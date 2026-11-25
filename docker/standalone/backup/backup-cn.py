#!/usr/bin/env python3
"""Backup for a Docker-based Hindsight deployment.

Same shape as the Supabase -> Jianguoyun backup scheme: one timestamped ZIP per
run, kept locally with a day-based retention and copied off-site over WebDAV to
a project directory. Two things that scheme's notes call out are handled
differently here:

* an off-site copy that is configured but fails makes the run fail (non-zero),
  instead of leaving a successful-looking job behind;
* every upload is downloaded again and compared by SHA-256 before it counts.

The archive holds a plain pg_dump custom-format dump, so it restores into a
fresh PostgreSQL with the same major version and extensions - no running
Hindsight and no matching application version needed:

    docker exec -i <pg container> pg_restore -U hindsight -d hindsight < hindsight.dump

`drill` proves exactly that on a throwaway container without a network.

The dump and the per-table row counts in the manifest are taken from one
exported snapshot, so a drill can compare them exactly. `roles.sql` comes from
pg_dumpall --no-role-passwords and carries no password hashes.

Settings are KEY=VALUE files next to this script: `config` for everything
non-secret, `webdav.env` (mode 600) for WEBDAV_USERNAME and WEBDAV_PASSWORD.
Standard library only; needs docker, curl and a PostgreSQL container to exec
into.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Callable

STAMP_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})\.zip$")
SNAPSHOT_RE = re.compile(r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{8}-[0-9A-Fa-f]+$")
TIMERS = ("hindsight-backup.timer", "hindsight-restore-drill.timer")

DEFAULTS = {
    "BACKUP_DIR": "/srv/build/artifacts/hindsight/backups",
    "DB_CONTAINER": "hindsight-db",
    "APP_CONTAINER": "hindsight-app",
    "DB_USER": "hindsight",
    "DB_NAME": "hindsight",
    "KEEP_LOCAL_DAYS": "14",
    "KEEP_REMOTE_DAYS": "30",
    # The off-site copy stays off until this is "webdav" and webdav.env holds credentials.
    "UPLOAD_PROVIDER": "",
    # Days between uploads. Local archives are still made daily; raise this when the
    # WebDAV account's monthly upload traffic is capped.
    "UPLOAD_EVERY_N_DAYS": "1",
    "WEBDAV_URL": "",
    "WEBDAV_BASE_DIR": "hindsight",
}

PROPFIND_BODY = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<d:propfind xmlns:d="DAV:"><d:prop>'
    "<d:resourcetype/><d:getcontentlength/><d:getlastmodified/>"
    "</d:prop></d:propfind>"
)

ALEMBIC_SQL = (
    "select case when to_regclass('public.alembic_version') is null then '' "
    "else (select string_agg(version_num, ',') from public.alembic_version) end;"
)


class BackupError(Exception):
    """A failed step. The message is meant for the journal, so it names the cause."""


def log(level: str, message: str) -> None:
    print(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {level:<5} {message}", flush=True)


def run(cmd: list[str], stdin: bytes | None = None, timeout: int = 3600) -> bytes:
    """Run a command, return its stdout, raise with the stderr tail when it fails.

    A child never inherits this script's stdin. `docker exec -i` forwards whatever
    it reads on stdin into the container, so a caller feeding this script over
    `ssh host bash -s` would otherwise have the rest of that script swallowed.
    """
    try:
        p = subprocess.run(cmd, input=stdin if stdin is not None else b"", stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise BackupError(f"命令超时（{timeout}s）：{' '.join(cmd[:6])}")
    except FileNotFoundError:
        raise BackupError(f"找不到命令：{cmd[0]}")
    if p.returncode != 0:
        tail = p.stderr.decode("utf-8", "replace").strip()[-600:]
        raise BackupError(f"命令失败（退出码 {p.returncode}）：{' '.join(cmd[:7])}\n{tail}")
    return p.stdout


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def stamp_of(name: str) -> dt.datetime | None:
    """The timestamp encoded in one of our own file names, else None."""
    m = STAMP_RE.match(name) or STAMP_RE.match(name.removesuffix(".sha256"))
    return dt.datetime.strptime(m.group(1), "%Y-%m-%d_%H-%M-%S") if m else None


# --- configuration -----------------------------------------------------------------


def load_config(config_dir: Path) -> dict[str, str]:
    cfg = dict(DEFAULTS)
    for name in ("config", "webdav.env"):
        path = config_dir / name
        if not path.exists():
            continue
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            cfg[key.strip()] = value.strip().strip('"').strip("'")
    for key in ("KEEP_LOCAL_DAYS", "KEEP_REMOTE_DAYS"):
        if not cfg[key].isdigit():
            raise BackupError(f"config: {key} 必须是非负整数，当前为 {cfg[key]!r}")
    if not cfg["UPLOAD_EVERY_N_DAYS"].isdigit() or int(cfg["UPLOAD_EVERY_N_DAYS"]) < 1:
        raise BackupError(f"config: UPLOAD_EVERY_N_DAYS 必须是 >= 1 的整数，当前为 {cfg['UPLOAD_EVERY_N_DAYS']!r}")
    if cfg["UPLOAD_PROVIDER"] not in ("", "webdav"):
        raise BackupError(f"config: UPLOAD_PROVIDER 只支持留空或 webdav，当前为 {cfg['UPLOAD_PROVIDER']!r}")
    return cfg


def offsite_ready(cfg: dict[str, str]) -> tuple[bool, str]:
    """Whether the off-site copy is configured, and why not when it is not."""
    if cfg["UPLOAD_PROVIDER"] != "webdav":
        return False, "UPLOAD_PROVIDER 未设为 webdav"
    missing = [k for k in ("WEBDAV_URL", "WEBDAV_USERNAME", "WEBDAV_PASSWORD") if not cfg.get(k)]
    if missing:
        return False, "webdav.env 缺少 " + "、".join(missing)
    return True, ""


# --- database access ---------------------------------------------------------------


def db(cfg: dict[str, str], args: list[str], stdin: bytes | None = None, container: str | None = None) -> bytes:
    return run(["docker", "exec", "-i", container or cfg["DB_CONTAINER"], *args], stdin)


def psql(cfg: dict[str, str], sql: str, container: str | None = None) -> str:
    args = ["psql", "-U", cfg["DB_USER"], "-d", cfg["DB_NAME"], "-At", "-q", "-v", "ON_ERROR_STOP=1"]
    return db(cfg, args, sql.encode("utf-8"), container).decode("utf-8", "replace").strip()


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def table_names(cfg: dict[str, str], container: str | None = None) -> list[str]:
    sql = (
        "select table_name from information_schema.tables "
        "where table_schema='public' and table_type='BASE TABLE' order by table_name"
    )
    return [t for t in psql(cfg, sql, container).splitlines() if t]


def counts_sql(tables: list[str]) -> str:
    return " union all ".join(f"select '{t}', count(*) from public.{quote_ident(t)}" for t in tables)


def parse_counts(out: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in out.splitlines():
        name, _, n = row.partition("|")
        if name and n.isdigit():
            counts[name] = int(n)
    return counts


def count_rows(cfg: dict[str, str], container: str | None = None) -> dict[str, int]:
    return parse_counts(psql(cfg, counts_sql(table_names(cfg, container)) + ";", container))


class Snapshot:
    """A REPEATABLE READ transaction whose snapshot pg_dump and the row counts share.

    The exporting psql session has to stay open until both are done, so a count
    taken while the database is being written is the count *inside the dump*,
    not a nearby one.
    """

    def __init__(self, cfg: dict[str, str]) -> None:
        self.cfg = cfg
        self.proc: subprocess.Popen | None = None
        self.id = ""
        self.err = tempfile.TemporaryFile()

    def __enter__(self) -> Snapshot:
        cfg = self.cfg
        args = ["docker", "exec", "-i", cfg["DB_CONTAINER"], "psql", "-U", cfg["DB_USER"], "-d", cfg["DB_NAME"],
                "-At", "-q", "-v", "ON_ERROR_STOP=1"]
        self.proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.err, text=True)
        try:
            assert self.proc.stdin is not None and self.proc.stdout is not None
            self.proc.stdin.write("BEGIN ISOLATION LEVEL REPEATABLE READ;\nSELECT pg_export_snapshot();\n")
            self.proc.stdin.flush()
            line = self.proc.stdout.readline().strip()
        except (BrokenPipeError, AssertionError):
            line = ""
        if not SNAPSHOT_RE.match(line):
            self.close()
            raise BackupError("无法导出数据库快照：" + self._stderr())
        self.id = line
        return self

    def counts(self) -> dict[str, int]:
        # SET TRANSACTION SNAPSHOT has to be the first statement after BEGIN.
        sql = (
            "BEGIN ISOLATION LEVEL REPEATABLE READ;\n"
            f"SET TRANSACTION SNAPSHOT '{self.id}';\n"
            f"{counts_sql(table_names(self.cfg))};\n"
            "COMMIT;\n"
        )
        return parse_counts(psql(self.cfg, sql))

    def _stderr(self) -> str:
        self.err.seek(0)
        return self.err.read().decode("utf-8", "replace").strip()[-400:]

    def close(self) -> None:
        if self.proc is None:
            return
        try:
            if self.proc.stdin:
                self.proc.stdin.write("ROLLBACK;\n\\q\n")
                self.proc.stdin.close()
            self.proc.wait(timeout=30)
        except Exception:
            self.proc.kill()
        finally:
            self.err.close()
            self.proc = None

    def __exit__(self, *exc: object) -> None:
        self.close()


# --- backup ------------------------------------------------------------------------


def verify_dump(cfg: dict[str, str], dump: Path, tables: list[str]) -> int:
    """pg_restore --list reads the archive's table of contents: a truncated or corrupt dump fails here."""
    listing = db(cfg, ["pg_restore", "--list"], dump.read_bytes()).decode("utf-8", "replace")
    data_entries = {m.group(1) for m in re.finditer(r" TABLE DATA public (\S+) ", listing)}
    missing = [t for t in tables if t not in data_entries]
    if not tables or missing:
        raise BackupError(f"备份归档缺少这些表的数据：{', '.join(missing) or '（库里没有表）'}")
    return len(data_entries)


def make_backup(cfg: dict[str, str]) -> tuple[Path, dict]:
    backup_dir = Path(cfg["BACKUP_DIR"])
    backup_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(backup_dir, 0o700)
    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    work = backup_dir / ".tmp" / stamp
    work.mkdir(parents=True, mode=0o700)
    os.chmod(backup_dir / ".tmp", 0o700)
    try:
        dump = work / "hindsight.dump"
        started = time.monotonic()
        with Snapshot(cfg) as snap:
            dump.write_bytes(
                db(cfg, ["pg_dump", "-U", cfg["DB_USER"], "-d", cfg["DB_NAME"], "-Fc", "--no-password",
                         "--snapshot", snap.id])
            )
            counts = snap.counts()
        tables = sorted(counts)
        dump_seconds = time.monotonic() - started
        (work / "roles.sql").write_bytes(
            db(cfg, ["pg_dumpall", "-U", cfg["DB_USER"], "--globals-only", "--no-role-passwords"])
        )
        verify_dump(cfg, dump, tables)

        info = psql(cfg, "select version(); select pg_database_size(current_database());" + ALEMBIC_SQL).splitlines()
        app_image = ""
        if cfg["APP_CONTAINER"]:
            try:
                app_image = run(["docker", "inspect", "-f", "{{.Image}} {{.Config.Image}}",
                                 cfg["APP_CONTAINER"]]).decode().strip()
            except BackupError:
                log("WARN", f"未读取到应用容器 {cfg['APP_CONTAINER']} 的镜像信息（不影响备份）")

        members = {p.name: {"size_bytes": p.stat().st_size, "sha256": sha256_file(p)} for p in sorted(work.iterdir())}
        manifest = {
            "format": "hindsight-cn-backup/1",
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "stamp": stamp,
            "database": {
                "user": cfg["DB_USER"], "name": cfg["DB_NAME"],
                "version": info[0] if info else "",
                "size_bytes": int(info[1]) if len(info) > 1 and info[1].isdigit() else None,
            },
            "hindsight": {"app_image": app_image, "alembic_version": info[2] if len(info) > 2 else ""},
            "table_rows": dict(sorted(counts.items())),
            "members": members,
        }
        manifest_text = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"

        # The dump is already compressed; compressing it again would only cost time.
        zip_tmp = backup_dir / f".{stamp}.zip.part"
        with zipfile.ZipFile(zip_tmp, "w") as zf:
            zf.write(dump, "hindsight.dump", compress_type=zipfile.ZIP_STORED)
            zf.write(work / "roles.sql", "roles.sql", compress_type=zipfile.ZIP_DEFLATED)
            zf.writestr("manifest.json", manifest_text, compress_type=zipfile.ZIP_DEFLATED)
        os.chmod(zip_tmp, 0o600)
        with zip_tmp.open("rb") as f:
            os.fsync(f.fileno())

        # Trust what was written only after reading the finished ZIP back.
        with zipfile.ZipFile(zip_tmp) as zf:
            if zf.testzip() is not None:
                raise BackupError("备份 ZIP 自检失败（成员 CRC 不匹配）")
            for name, expected in members.items():
                h = hashlib.sha256()
                with zf.open(name) as f:
                    while chunk := f.read(1 << 20):
                        h.update(chunk)
                if h.hexdigest() != expected["sha256"]:
                    raise BackupError(f"备份 ZIP 回读校验失败：{name}")
            if json.loads(zf.read("manifest.json"))["table_rows"] != manifest["table_rows"]:
                raise BackupError("备份 ZIP 回读校验失败：manifest.json")

        final = backup_dir / f"{stamp}.zip"
        os.replace(zip_tmp, final)
        sidecar = backup_dir / f"{stamp}.zip.sha256"
        sidecar.write_text(f"{sha256_file(final)}  {final.name}\n")
        os.chmod(sidecar, 0o600)
        log("INFO", f"备份成功（本地）：{final}  {final.stat().st_size / 1e6:.1f}MB  {len(tables)} 张表 "
                    f"{sum(counts.values())} 行  pg_dump {dump_seconds:.1f}s")
        return final, manifest
    finally:
        shutil.rmtree(work, ignore_errors=True)


def prune_local(cfg: dict[str, str]) -> int:
    """Delete our own timestamped archives and sidecars past the local retention."""
    keep = int(cfg["KEEP_LOCAL_DAYS"])
    cutoff = time.time() - keep * 86400
    removed = 0
    for path in Path(cfg["BACKUP_DIR"]).iterdir():
        if stamp_of(path.name) and path.is_file() and path.stat().st_mtime < cutoff:
            path.unlink()
            removed += 1
    if removed:
        log("INFO", f"本地保留 {keep} 天：删除 {removed} 个过期文件")
    return removed


# --- WebDAV -------------------------------------------------------------------------


class WebDav:
    """curl-based WebDAV client. Credentials reach curl through a private netrc file, never argv."""

    def __init__(self, cfg: dict[str, str]) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.netrc = Path(self.tmp.name) / "netrc"
        host = urllib.parse.urlsplit(cfg["WEBDAV_URL"]).hostname or ""
        fd = os.open(self.netrc, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(f'machine {host} login "{cfg["WEBDAV_USERNAME"]}" password "{cfg["WEBDAV_PASSWORD"]}"\n')
        self.root = cfg["WEBDAV_URL"].rstrip("/") + "/"
        parts = [urllib.parse.quote(p) for p in cfg["WEBDAV_BASE_DIR"].strip("/").split("/") if p]
        self.base = self.root + "/".join(parts) + ("/" if parts else "")

    def close(self) -> None:
        self.tmp.cleanup()

    def _curl(self, url: str, target: Path, *extra: str, timeout: int = 300) -> int:
        cmd = ["curl", "-sS", "--connect-timeout", "15", "--max-time", str(timeout), "--netrc-file", str(self.netrc),
               "-o", str(target), "-w", "%{http_code}", "-A", "hindsight-backup", *extra, url]
        try:
            code = run(cmd, timeout=timeout + 30).decode().strip()
        except BackupError as e:
            # run() prefixes "命令失败…": keep only the curl error itself.
            raise BackupError(f"WebDAV 请求失败（{url}）：{str(e).partition(chr(10))[2].strip() or e}")
        if not code.isdigit():
            raise BackupError(f"WebDAV 请求异常（{url}）：{code[:200]}")
        if code == "401":
            raise BackupError("WebDAV 认证失败（HTTP 401）：检查 webdav.env 里的账号与应用密码")
        return int(code)

    def request(self, method: str, url: str, *, headers: list[str] = (), body: str | None = None,
                upload: Path | None = None, timeout: int = 300) -> tuple[int, bytes]:
        out = Path(self.tmp.name) / "response"
        extra: list[str] = []
        for header in headers:
            extra += ["-H", header]
        if upload is not None:
            extra += ["-T", str(upload)]  # implies PUT
        else:
            extra += ["-X", method]
            if body is not None:
                extra += ["-H", "Content-Type: application/xml", "--data-binary", body]
        code = self._curl(url, out, *extra, timeout=timeout)
        data = out.read_bytes() if out.exists() else b""
        out.unlink(missing_ok=True)
        return code, data

    def download(self, url: str, target: Path) -> int:
        return self._curl(url, target, timeout=1800)

    def ensure_dir(self) -> None:
        """Check the project directory, creating missing levels. 405 on MKCOL means it already exists."""
        prefix = self.root
        for part in self.base[len(self.root):].split("/"):
            if not part:
                continue
            prefix += part + "/"
            code, _ = self.request("PROPFIND", prefix, headers=["Depth: 0"], body=PROPFIND_BODY)
            if code in (200, 207):
                continue
            if code != 404:
                raise BackupError(f"WebDAV 无法访问目录 {prefix}（HTTP {code}）")
            code, _ = self.request("MKCOL", prefix)
            if code not in (200, 201, 204, 405):
                raise BackupError(f"WebDAV 创建目录失败 {prefix}（HTTP {code}）")

    def list_files(self) -> list[str]:
        code, data = self.request("PROPFIND", self.base, headers=["Depth: 1"], body=PROPFIND_BODY)
        if code not in (200, 207):
            raise BackupError(f"WebDAV 列目录失败 {self.base}（HTTP {code}）")
        try:
            root = ET.fromstring(data)
        except ET.ParseError:
            raise BackupError("WebDAV 列目录返回了无法解析的内容")
        names = []
        for response in root.iter():
            if response.tag.rsplit("}", 1)[-1] != "response":
                continue
            href, is_dir = None, False
            for node in response.iter():
                tag = node.tag.rsplit("}", 1)[-1]
                if tag == "href" and href is None:
                    href = node.text or ""
                elif tag == "collection":
                    is_dir = True
            if href is None or is_dir:
                continue
            name = urllib.parse.unquote(href.rstrip("/")).rsplit("/", 1)[-1]
            if name:
                names.append(name)
        return names

    def delete(self, name: str) -> None:
        code, _ = self.request("DELETE", self.base + urllib.parse.quote(name))
        if code not in (200, 204, 404):
            raise BackupError(f"WebDAV 删除远端文件失败 {name}（HTTP {code}）")


def offsite_copy(cfg: dict[str, str], archive: Path) -> None:
    """Upload one archive under a temporary name, move it into place, read it back, then prune the remote."""
    dav = WebDav(cfg)
    try:
        dav.ensure_dir()
        name = urllib.parse.quote(archive.name)
        final, temporary = dav.base + name, dav.base + name + ".uploading"
        code, _ = dav.request("PUT", temporary, upload=archive, timeout=1800)
        if code not in (200, 201, 204):
            raise BackupError(f"WebDAV 上传失败 {archive.name}（HTTP {code}）")
        code, _ = dav.request("MOVE", temporary, headers=[f"Destination: {final}", "Overwrite: T"])
        if code not in (201, 204):
            raise BackupError(f"WebDAV 上传后改名失败 {archive.name}（HTTP {code}）")
        readback = Path(dav.tmp.name) / "readback.zip"
        code = dav.download(final, readback)
        if code != 200:
            raise BackupError(f"WebDAV 回读失败 {archive.name}（HTTP {code}）")
        if sha256_file(readback) != sha256_file(archive):
            raise BackupError(f"WebDAV 回读内容与本地不一致 {archive.name}")
        readback.unlink(missing_ok=True)
        log("INFO", f"WebDAV 上传成功：{final}（已回读，SHA-256 一致）")

        keep = int(cfg["KEEP_REMOTE_DAYS"])
        cutoff = dt.datetime.now() - dt.timedelta(days=keep)
        removed = stale = 0
        for remote in dav.list_files():
            # A .uploading file is an interrupted upload. The lock serialises runs, so none is ever in progress here.
            if remote.endswith(".zip.uploading"):
                dav.delete(remote)
                stale += 1
                continue
            when = stamp_of(remote)
            if when and when < cutoff:
                dav.delete(remote)
                removed += 1
        if removed:
            log("INFO", f"远端保留 {keep} 天：删除 {removed} 个过期文件")
        if stale:
            log("INFO", f"远端清理 {stale} 个未完成的上传残留")
        marker = Path(cfg["BACKUP_DIR"]) / ".last-upload"
        marker.write_text(dt.datetime.now().isoformat(timespec="seconds") + "\n")
        os.chmod(marker, 0o600)
    finally:
        dav.close()


# --- restore drill -------------------------------------------------------------------


def wait_until_ready(name: str, user: str, dbname: str) -> None:
    """The image runs a temporary server during initdb, so one successful probe proves little: require three in a row."""
    ok = 0
    for _ in range(90):
        probe = subprocess.run(["docker", "exec", name, "psql", "-U", user, "-d", dbname, "-At", "-c", "select 1"],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ok = ok + 1 if probe.returncode == 0 else 0
        if ok >= 3:
            return
        time.sleep(2)
    raise BackupError("演练用数据库容器未能就绪")


def restore_drill(cfg: dict[str, str], archive: Path | None) -> None:
    """Restore the newest (or the given) archive into a throwaway container and compare row counts."""
    if archive is None:
        candidates = sorted(p for p in Path(cfg["BACKUP_DIR"]).iterdir() if STAMP_RE.match(p.name))
        if not candidates:
            raise BackupError("本地没有可演练的备份")
        archive = candidates[-1]
    with zipfile.ZipFile(archive) as zf:
        manifest = json.loads(zf.read("manifest.json"))
        dump = zf.read("hindsight.dump")
    started = time.monotonic()
    image = run(["docker", "inspect", "-f", "{{.Image}}", cfg["DB_CONTAINER"]]).decode().strip()
    name = f"{cfg['DB_CONTAINER']}-restore-drill"
    subprocess.run(["docker", "rm", "-f", name], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL)
    try:
        run(["docker", "run", "-d", "--rm", "--name", name, "--network", "none",
             "-e", f"POSTGRES_USER={cfg['DB_USER']}", "-e", "POSTGRES_PASSWORD=restore-drill",
             "-e", f"POSTGRES_DB={cfg['DB_NAME']}", image])
        wait_until_ready(name, cfg["DB_USER"], cfg["DB_NAME"])
        # --exit-on-error: a drill that skips broken objects proves nothing.
        db(cfg, ["pg_restore", "-U", cfg["DB_USER"], "-d", cfg["DB_NAME"], "--exit-on-error"], dump, container=name)
        restored = count_rows(cfg, container=name)
        restored_alembic = psql(cfg, ALEMBIC_SQL, container=name)
    finally:
        subprocess.run(["docker", "stop", "-t", "30", name], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        # --rm removes the container after it exits, but not before this returns: wait, or
        # its anonymous data volume outlives the drill.
        for _ in range(30):
            left = subprocess.run(["docker", "ps", "-aq", "--filter", f"name=^{name}$"],
                                  stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  text=True).stdout.strip()
            if not left:
                break
            time.sleep(2)

    expected = manifest["table_rows"]
    problems = [
        f"{t}: 备份 {expected.get(t, 0)} 行，恢复后 {restored.get(t, 0)} 行"
        for t in sorted(set(expected) | set(restored))
        if expected.get(t) != restored.get(t)
    ]
    if restored_alembic != manifest["hindsight"]["alembic_version"]:
        problems.append(f"alembic_version: 备份 {manifest['hindsight']['alembic_version']}，恢复后 {restored_alembic}")
    if problems:
        raise BackupError("恢复演练不一致：\n" + "\n".join(problems))
    log("INFO", f"恢复演练通过：{archive.name} 已还原到无网络的临时容器，{len(restored)} 张表 "
                f"{sum(restored.values())} 行与备份时完全一致，耗时 {time.monotonic() - started:.0f}s")


# --- commands ------------------------------------------------------------------------


def last_upload(cfg: dict[str, str]) -> dt.datetime | None:
    """When the last off-site copy succeeded, per the marker a successful upload leaves behind."""
    marker = Path(cfg["BACKUP_DIR"]) / ".last-upload"
    try:
        return dt.datetime.fromisoformat(marker.read_text().strip())
    except (OSError, ValueError):
        return None


def due_for_upload(cfg: dict[str, str]) -> tuple[bool, str]:
    every = int(cfg["UPLOAD_EVERY_N_DAYS"])
    previous = last_upload(cfg)
    if previous is None:
        return True, "从未上传过"
    age = dt.datetime.now() - previous
    if age >= dt.timedelta(days=every):
        return True, f"距上次成功上传 {age.days} 天"
    return False, f"距上次成功上传不足 {every} 天（{age.days} 天 {age.seconds // 3600} 小时）"


def run_backup(cfg: dict[str, str]) -> None:
    started = time.monotonic()
    summary: dict = {"result": "failed", "archive": None, "offsite": "skipped"}
    try:
        archive, manifest = make_backup(cfg)
        summary.update(archive=str(archive), rows=sum(manifest["table_rows"].values()))
        prune_local(cfg)
        ready, why = offsite_ready(cfg)
        if not ready:
            summary["offsite"] = "not_configured"
            log("WARN", f"未配置异地上传（{why}）：本轮仅有本地备份")
        else:
            due, why = due_for_upload(cfg)
            if not due:
                summary["offsite"] = "skipped_interval"
                log("INFO", f"按 UPLOAD_EVERY_N_DAYS 跳过本轮上传：{why}")
            else:
                summary["offsite"] = "failed"
                offsite_copy(cfg, archive)
                summary["offsite"] = "uploaded"
        summary["result"] = "ok"
    finally:
        summary["seconds"] = round(time.monotonic() - started, 1)
        print(f"SUMMARY {json.dumps(summary, ensure_ascii=False)}", flush=True)


def with_lock(cfg: dict[str, str], fn: Callable[[], None]) -> None:
    backup_dir = Path(cfg["BACKUP_DIR"])
    backup_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(backup_dir, 0o700)
    with (backup_dir / ".lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BackupError("另一个备份或演练正在运行，本次退出")
        fn()


def show_status(cfg: dict[str, str]) -> None:
    if not Path(cfg["BACKUP_DIR"]).exists():
        print(f"备份目录尚不存在: {cfg['BACKUP_DIR']}")
        archives = []
    else:
        archives = sorted(p for p in Path(cfg["BACKUP_DIR"]).iterdir() if STAMP_RE.match(p.name))
    ready, why = offsite_ready(cfg)
    print(f"备份目录: {cfg['BACKUP_DIR']}")
    print(f"本地保留: {cfg['KEEP_LOCAL_DAYS']} 天    远端保留: {cfg['KEEP_REMOTE_DAYS']} 天")
    print(f"异地上传: {'已配置（webdav）' if ready else '未配置（' + why + '）'}")
    if ready:
        previous = last_upload(cfg)
        print(f"上传间隔: 每 {cfg['UPLOAD_EVERY_N_DAYS']} 天    上次成功上传: "
              f"{previous.strftime('%Y-%m-%d %H:%M:%S') if previous else '从未'}")
    print(f"本地备份: {len(archives)} 个")
    for archive in archives[-5:]:
        print(f"  {archive.name}  {archive.stat().st_size / 1e6:.1f}MB")
    for unit in TIMERS:
        state = subprocess.run(["systemctl", "--user", "show", unit, "-p", "ActiveState", "-p", "NextElapseUSecRealtime"],
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               text=True).stdout
        fields = dict(line.split("=", 1) for line in state.splitlines() if "=" in line)
        print(f"{unit}: {fields.get('ActiveState', 'not-found')}  下次: {fields.get('NextElapseUSecRealtime', 'n/a')}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Hindsight 数据库备份：pg_dump 归档、本地保留、WebDAV 异地副本、恢复演练")
    parser.add_argument("--config-dir", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--backup-dir", type=Path, help="覆盖配置里的 BACKUP_DIR（测试用）")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="备份一次：本地归档，以及已配置时的异地上传")
    drill = sub.add_parser("drill", help="把最新备份恢复到无网络的临时容器并核对行数")
    drill.add_argument("--zip", type=Path, help="演练指定的归档，默认最新一份")
    again = sub.add_parser("upload", help="重新上传一份已有的本地归档（例如首次配置凭据后）")
    again.add_argument("--zip", type=Path, required=True)
    sub.add_parser("status", help="查看配置、已有备份与定时器状态")
    args = parser.parse_args()

    try:
        cfg = load_config(args.config_dir)
        if args.backup_dir:
            cfg["BACKUP_DIR"] = str(args.backup_dir)
        if args.command == "run":
            with_lock(cfg, lambda: run_backup(cfg))
        elif args.command == "drill":
            with_lock(cfg, lambda: restore_drill(cfg, args.zip))
        elif args.command == "upload":
            with_lock(cfg, lambda: offsite_copy(cfg, args.zip))
        elif args.command == "status":
            show_status(cfg)
    except BackupError as e:
        log("ERROR", str(e))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
