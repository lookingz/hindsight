# CN Deployment Notes

This branch uses a China-friendly Docker deployment flow centered on `docker/standalone/Dockerfile.deploy`.

## Branch

- Deploy from `main-cn-llm-api`.
- Keep `main` clean for upstream sync only.

## Files

- Base compose entry: `docker-compose.yaml`
- Deployment override template: `docker-compose.override.yaml.example`
- Local server-only override: `docker-compose.override.yaml`
- Deployment Dockerfile: `docker/standalone/Dockerfile.deploy`
- Runtime secrets/config: `.env`
- Deploy / rollback / status script: `scripts/deploy-cn.sh`

## What `Dockerfile.deploy` is

A derived copy of upstream's `docker/standalone/Dockerfile` plus three CN deltas,
nothing else:

- Package mirrors: Debian apt, Alpine apk, the npm registry and the PyPI bootstrap
  of `uv` resolve through Aliyun / npmmirror.
- `BUILD_PROXY` (optional): HTTP proxy for build-time wheel, npm tarball and
  Hugging Face downloads. apt always talks to the mirror directly. Never baked
  into the image. `BUILD_NO_PROXY` lists hosts that must stay direct.
- `HF_ENDPOINT` / `HF_TOKEN` (optional): Hugging Face mirror and token for the
  build-time model pre-download.

Upstream reworks its Dockerfile regularly (uv workspace lockfile, Node base
version, control-plane build path). When a sync changes
`docker/standalone/Dockerfile`, **re-derive `Dockerfile.deploy` from it** instead
of patching forward, then check the drift:

```bash
git diff --no-index docker/standalone/Dockerfile docker/standalone/Dockerfile.deploy
```

Only the deltas above (and the header) may show. An actual build is the only
real test: between 2026-08 and 2026-11 this file fell three months behind
upstream without any signal, and no longer built the current source.

The image keeps local model dependencies enabled, so `HINDSIGHT_API_RERANKER_PROVIDER=local`
can run inside the container.

## Recommended `.env` items

Runtime:

- `HINDSIGHT_API_LLM_PROVIDER`
- `HINDSIGHT_API_LLM_API_KEY`
- `HINDSIGHT_API_LLM_MODEL`
- `HINDSIGHT_API_LLM_BASE_URL`
- `HINDSIGHT_API_EMBEDDINGS_PROVIDER`
- `HINDSIGHT_API_EMBEDDINGS_LITELLM_SDK_MODEL`
- `HINDSIGHT_API_EMBEDDINGS_LITELLM_SDK_API_BASE`
- `HINDSIGHT_API_EMBEDDINGS_LITELLM_SDK_API_KEY`
- `HINDSIGHT_API_RERANKER_PROVIDER=local`
- `HINDSIGHT_API_RERANKER_LOCAL_MODEL`
- `HINDSIGHT_API_RERANKER_LOCAL_BUCKET_BATCHING` - opt-in length-sorted batching
  for the local reranker; same scores, less padding. On when the model is
  `hindsight-ai/hs-rerank-1`.
- `HINDSIGHT_API_WORKER_ID=hindsight-app`

Deployment:

- `HINDSIGHT_DB_PASSWORD` - the base compose falls back to `hindsight_pass`;
  set a real password on any server.
- `COMPOSE_PROJECT_NAME` - optional; the base compose names the project `hindsight-local`.
- `HINDSIGHT_SOURCE_DIR` - split layout only, see below.
- `BUILD_PROXY`, `BUILD_NO_PROXY` - build-time only. The pytorch wheel host
  (`download-r2.pytorch.org`) is often throttled to ~100 KB/s from China while
  PyPI and the mirrors are fast, so proxy only what needs it:

  ```
  BUILD_PROXY=http://127.0.0.1:7890
  BUILD_NO_PROXY=mirrors.aliyun.com,files.pythonhosted.org,pypi.org,registry.npmmirror.com,hf-mirror.com
  ```

  It is used from inside the build container, so `127.0.0.1` is the container,
  not the host. If the proxy only listens on the host loopback, uncomment
  `network: host` in the override so build steps share the host network.
- `HF_ENDPOINT`, `HF_TOKEN`

## First-time deploy

Single checkout that holds everything (source, compose files, `.env`, build):

```bash
git checkout main-cn-llm-api
cp docker-compose.override.yaml.example docker-compose.override.yaml
# fill in .env
docker compose build hindsight
docker compose up -d
docker compose ps
docker compose logs -f hindsight
```

### Split layout

For hosts that keep Git working trees and Docker build contexts in a dedicated
build workspace, with application entries under a services directory. The
checkout is only the build context; the deployment entry holds compose + config.

```bash
# 1. checkout = build context (shallow is enough: every deploy fetches on top,
#    and rollback targets are commits that were already deployed)
git clone --depth 1 --branch main-cn-llm-api \
  https://github.com/<fork>/hindsight.git <source-dir>

# 2. deployment entry
mkdir -p <deploy-dir> && cd <deploy-dir>
cp <source-dir>/docker-compose.yaml .
cp <source-dir>/docker-compose.override.yaml.example docker-compose.override.yaml
# .env: the usual settings, plus
#   HINDSIGHT_SOURCE_DIR=<source-dir>
```

Locally, `.deploy-env.local` then carries three paths:

```
DEPLOY_HOST=<host>
DEPLOY_DIR=<deploy-dir>
DEPLOY_SOURCE_DIR=<source-dir>
```

`scripts/deploy-cn.sh` resets the checkout, copies `docker-compose.yaml` into
the entry dir, and builds / restarts from there.

## Re-deploy after updates

Development happens locally; the deploy host only runs code that was pushed to
`origin/main-cn-llm-api`. From a local checkout:

```bash
git push origin main-cn-llm-api
scripts/deploy-cn.sh
```

The script SSHes into the deploy host (set `DEPLOY_HOST`, `DEPLOY_DIR` and
optionally `DEPLOY_SOURCE_DIR` in the environment or a gitignored
`.deploy-env.local`), resets its checkout to `origin/main-cn-llm-api`, rebuilds,
restarts, and waits for the API health check. Gitignored host-local files
(`.env`, `docker-compose.override.yaml`) are left untouched.

Equivalent manual steps on the host (single-dir layout):

```bash
git checkout main-cn-llm-api
git fetch origin
git reset --hard origin/main-cn-llm-api
docker compose build hindsight
docker compose up -d
```

## Rollback

```bash
scripts/deploy-cn.sh rollback
```

Rebuilds the commit recorded in `<deploy-dir>/.deploy-last`: the one that was
running before the current deployment. The script writes it before anything is
switched, so it is also the right target after a failed health check.

## Database port

The base compose publishes 5432 on every host interface. The override template
drops that with `ports: !reset []` (Compose >= 2.24): the database is reachable
only on the compose network, and `docker exec hindsight-db psql ...` still
works. Publish `127.0.0.1:5432:5432` only if you need psql from the host itself.

## Single-user authentication (before any public tunnel)

The API and control plane are open by default. For a personal deployment exposed
through a tunnel, enable both layers before publishing anything:

```bash
# Independent, randomly generated secrets; do not reuse the LLM provider keys.
HINDSIGHT_CP_ACCESS_KEY=<control-plane login key>
HINDSIGHT_API_TENANT_EXTENSION=hindsight_api.extensions.builtin.tenant:ApiKeyTenantExtension
HINDSIGHT_API_TENANT_API_KEY=<dataplane Bearer key>
HINDSIGHT_CP_DATAPLANE_API_KEY=<same dataplane Bearer key>
```

Then run `docker compose up -d hindsight`.

- The control plane shows a login page and issues a 24-hour HttpOnly session
  cookie after the shared key is entered. HTTPS adds `Secure` automatically.
- The dataplane requires `Authorization: Bearer ...`; health endpoints remain
  public for deployment checks.
- This is a single shared-key gate, not user accounts, roles or per-user memory
  isolation. The built-in API-key tenant still uses the `public` schema.
- By default, expose only control-plane port 9999 through the tunnel. Keep
  dataplane 8888 on loopback; the control plane calls it internally with the
  Bearer key.
- If external agents need direct API access, a separate authenticated tunnel to
  8888 is also supported. Do not point agents at the control-plane hostname:
  its middleware redirects `/v1/...` and `/health/...` to the UI login page.

A cpolar tunnel entry looks like this (use your own reserved subdomain and
follow the host's shared-tunnel procedures; do not start a second cpolar):

```yaml
  hindsight:
    proto: http
    addr: 127.0.0.1:9999
    region: cn_vip
    subdomain: <reserved-subdomain>
    bind_tls: true
    inspect: false
```

Optional second tunnel for external agents (requires the API Bearer key above):

```yaml
  hindsight-api:
    proto: http
    addr: 127.0.0.1:8888
    region: cn_vip
    subdomain: <reserved-api-subdomain>
    bind_tls: true
    inspect: false
```

Agents then use `https://<reserved-api-subdomain>.vip.cpolar.cn/v1/default` and
`Authorization: Bearer <HINDSIGHT_API_TENANT_API_KEY>`. `/health/live` and
`/health` stay public; data endpoints reject missing or wrong keys with 401.

## Backups

`docker/standalone/backup/` is a host-side backup job with the same shape as the
Supabase -> Jianguoyun scheme: one timestamped ZIP per run, day-based local
retention and an optional WebDAV off-site copy. It needs only python3, curl and
docker.

The ZIP holds a plain `pg_dump` custom-format dump, `roles.sql` (no password
hashes) and a manifest, so it restores into a fresh PostgreSQL with the same
major version and extensions - no Hindsight needs to be running:

```bash
docker exec -i <pg container> pg_restore -U hindsight -d hindsight < hindsight.dump
```

Install it next to the compose entry:

```bash
mkdir -m 700 <deploy-dir>/backup
cp docker/standalone/backup/backup-cn.py <deploy-dir>/backup/
cp docker/standalone/backup/config.example <deploy-dir>/backup/config
chmod 600 <deploy-dir>/backup/config
cp docker/standalone/backup/*.service docker/standalone/backup/*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now hindsight-backup.timer hindsight-restore-drill.timer
```

- `hindsight-backup.timer` runs daily at 02:00 host time (`Persistent=true`):
  a local archive, then the off-site copy once that is configured.
- `hindsight-restore-drill.timer` runs weekly. It restores the newest archive
  into a throwaway container with no network and compares every table's row
  count with the manifest, which is taken from the same snapshot as the dump.
  A backup is only proven by a passing drill, not by the unit's exit code.

| Command (run `python3 <deploy-dir>/backup/backup-cn.py ...`) | Does |
| --- | --- |
| `status` | Settings, archives, timer state, last successful upload |
| `run` | Back up now |
| `drill` | Restore the newest archive and verify it |
| `upload --zip <archive>` | Upload (again) one local archive |

**Off-site copy.** Copy `webdav.env.example` to `webdav.env` (mode 600) with
`WEBDAV_USERNAME` and `WEBDAV_PASSWORD` - for Jianguoyun, the login email and
an app password - then set `UPLOAD_PROVIDER=webdav` in `config`. Until then each
run logs `未配置异地上传` and keeps a local archive only. Once it is set, a
failed upload fails the run, so `systemctl --user --failed` shows a broken
credential instead of a successful-looking job. The archive goes up under a
temporary name, is moved into place, then is downloaded again and compared by
SHA-256. Verified against Jianguoyun's WebDAV on 2026-11-25, including a
restore drill from the downloaded copy.

`WEBDAV_BASE_DIR` is relative to `WEBDAV_URL` and missing levels are created,
so the archives can sit inside a folder that is already there. On macpro they
moved from `/dav/hindsight/` to `/dav/ns_ServerBackups/hindsight/` on
2026-11-27 with a WebDAV `MOVE` per file (nothing re-uploaded), then the newest
was read back from its new place and compared by SHA-256. Two Jianguoyun
quirks showed up: it answers 403 to `DELETE` on a folder, and it lists only
folders that are not empty, so the emptied folder is invisible over WebDAV
until someone removes it in the Jianguoyun client.

`UPLOAD_EVERY_N_DAYS` spaces uploads out for a WebDAV account that caps monthly
upload traffic: a 30 MB database is about 0.9 GB a month at one upload a day,
and the read-back counts as download traffic. The interval is counted by
calendar date, not as 24-hour periods. The timer fires at a randomly delayed
minute, so against a strict 24 hours the next night's run was skipped whenever
it started a few minutes earlier than the previous upload had finished, and so
was the night after a manual upload during the day. The macpro archive of
2026-11-26 stayed local that way and was uploaded by hand after the fix on
2026-11-27. `python3 -m unittest discover -s docker/standalone/backup -p
'test_*.py'` covers the rule and needs neither docker nor a network.

In the journal, success is the `SUMMARY {"result": "ok", ... "offsite":
"uploaded"}` line. `offsite` is `not_configured` while credentials are missing
and `skipped_interval` inside the upload interval; both still mean a local
archive was written.

## Verification

- API: `http://localhost:8888` (liveness: `/health/live`, readiness: `/health`)
- Control plane: `http://localhost:9999`
- Database container should be `healthy`

## Notes

- Do not commit `.env` or `docker-compose.override.yaml`.
- The control-plane `npm install` stage can still be slow on first build.
- Local reranker works better through Docker than host-source startup because the image includes the needed local ML dependencies.
- The image pre-downloads upstream's default local models (bge-small-en-v1.5,
  hs-rerank-1, embeddinggemma-2). Any other `HINDSIGHT_API_RERANKER_LOCAL_MODEL`
  is fetched at runtime through `HF_ENDPOINT`, once per container. On a slow
  mirror that download can exceed `HINDSIGHT_API_MODEL_INIT_TIMEOUT` (default
  300s); the API then exits with "Model/connection initialization did not
  complete", and every container recreation starts over. Either keep the
  preloaded default, raise the timeout, or mount a volume at that model's own
  cache directory so the download happens once (other models still come from
  the image):

  ```yaml
      volumes:
        - reranker_cache:/home/hindsight/.cache/huggingface/hub/models--cross-encoder--ms-marco-MiniLM-L-6-v2
  volumes:
    reranker_cache:
  ```

  Seed it once as root, since a fresh volume is not writable by the container
  user:

  ```bash
  docker compose run --rm --no-deps --user 0 \
    -e HF_HUB_CACHE=/home/hindsight/.cache/huggingface/hub --entrypoint bash hindsight -c \
    'python -c "from huggingface_hub import snapshot_download; snapshot_download(\"cross-encoder/ms-marco-MiniLM-L-6-v2\")" \
     && chown -R 1000:1000 /home/hindsight/.cache/huggingface/hub/models--cross-encoder--ms-marco-MiniLM-L-6-v2'
  ```

  A deployment on the preloaded `hindsight-ai/hs-rerank-1` needs none of this:
  drop the mount and the volume. This one kept both, unused, after switching.
- The base file gives only the database a restart policy; the override adds
  `restart: always` for the app so it comes back after a crash or a reboot.
- Set a stable `HINDSIGHT_API_WORKER_ID` for Docker deployments. Otherwise the worker defaults to the container hostname, and recreated containers can leave async tasks stuck in `processing`.
- The API needs working LLM and embeddings keys to start at all: it verifies both connections during startup and exits otherwise (`scripts/deploy-cn.sh` then fails its health check). To initialize a fresh database before the keys are in place, run `docker compose run --rm --no-deps --entrypoint hindsight-admin hindsight run-db-migration`.
- `LITELLM_LOCAL_MODEL_COST_MAP=True` (set by the override) skips LiteLLM's startup fetch of its price table from `raw.githubusercontent.com`, which otherwise stalls every start where that host is unreachable.
- `Unknown retain strategy 'conversation'` in the logs means a client is naming, per item, a strategy the bank does not have. The `coding-agents` integration does this (`git`, `gitlog`, `conversation`, `document`, `survey`) and normally seeds them into the bank at session start; a bank it auto-created on first write can miss that. The server does not reject the request - it extracts with the bank's own rules, so a long conversation is split into 3000-character chunks and each one costs an extraction call. Give the bank the strategies. The plugin only ever adds what is missing, so seeding them by hand is safe; `GET .../config` on a bank it did seed returns them under `overrides.retain_strategies`:

  ```bash
  curl -X PATCH -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
    http://localhost:8888/v1/default/banks/<bank>/config \
    -d "{\"updates\": {\"retain_strategies\": <that map>}}"
  ```

  The `updates` wrapper is required; without it the endpoint answers 422.

  Check what a bank holds before giving it the plugin's `conversation` strategy
  by hand. That strategy is written for developer conversations: its mission asks
  for the fewest facts that capture the outcome (settled decisions, changes to
  code entities), and it sets `retain_chunk_size` to 12000. In a bank that holds
  other kinds of conversation, two things follow:

  - the next session write-back re-chunks the whole document, every chunk counts
    as changed, and the entire conversation is extracted again under that
    mission, replacing its facts (on macpro the whole store went from 3958 to
    about 1350 memory units the same day, almost all of it in banks that hold
    personal conversations);
  - facts that are not decisions or outcomes are mostly dropped.

  For such a bank, register `conversation` with the bank's own defaults instead,
  `{"retain_chunk_size": 3000, "retain_extraction_mode": "concise"}` and no
  mission. That silences the warning and keeps the server's general extraction
  rules, which already cover personal facts, preferences, events and plans; the
  seven macpro banks carry this since 2026-11-25. Checked on three real
  conversation samples: the plugin's strategy left memories stating 21 of the
  115 things the labeller found the user saying in them, the defaults 80. A
  short invented transcript showed no such difference, so test on real
  conversations. The document text is kept (`store_document_text`), so a later
  change can extract it again, and a change to the chunk size makes the next
  write-back do so by itself. The other four strategies are only used for git
  commits, documents and codebase surveys, and do nothing until a client names
  them.
- `HINDSIGHT_API_RERANKER_LOCAL_MODEL` names the local reranker. This deployment
  moved from `cross-encoder/ms-marco-MiniLM-L-6-v2` (English) to the service's
  default, `hindsight-ai/hs-rerank-1` (multilingual, trained for Hindsight
  memory reranking), on 2026-11-25.

  - The choice matters most through recall's combined scoring: `sigmoid(rerank score) * recency boost * proof-count boost`. A model that scores many candidates between 5 and 8 saturates under the sigmoid, so the boosts decide the order and relevance stops mattering.
  - Evidence on real memories: a 123-question comparison scored judged nDCG@5 at 0.17 for ms-marco and 0.91 for hs-rerank-1, with the answer in the top 5 for 11% versus 93% of questions. After the switch, 28 fresh questions asked of the live API put the source memory in the top 5 for 27 of them (0 before the switch, on the same candidates); its median rank went from 148 to 2.
  - Measure speed on the text the service really scores. It is not the memory's text but `[Date: <readable> (<iso>)] <document context>: <text>` (`engine/search/reranking.py`), and a client's context can be longer than the memory itself. In an idle container from the deployed image, default torch threads (12 on an E5-2697 v2), same candidates: ms-marco 101 ms per candidate; hs-rerank-1 80 ms with `HINDSIGHT_API_RERANKER_LOCAL_BUCKET_BATCHING=true` and 145 ms without. On bare text the three are 82, 53 and 107. A probe that scores bare text, or pins a thread count the service does not use, does not predict production.
  - Length-sorted batching leaves every score as it was (largest difference 4e-6 over 284 candidates).
  - Switching or rolling back is an env change plus `docker compose up -d --no-build hindsight`; the app reported healthy 41 s after the recreate. With hs-rerank-1 the ms-marco cache mount described above is unused.
- For `qwen3.8-flash` through DashScope, set `HINDSIGHT_API_LLM_EXTRA_BODY={"enable_thinking":false}` when reflect latency matters. The default thinking mode emits reasoning content; in one deployment it made complex reflect calls repeatedly hit the 30-second per-request timeout and the whole operation return 504. Disabling it also speeds retain/consolidation. Keep `HINDSIGHT_API_REFLECT_LLM_TIMEOUT` at its 30-second default unless measurements show healthy calls need more.

## Async worker caveat for single-schema deployments

- This deployment currently uses a single schema (`public`) for async operations.
- In this mode, do not install `public.schemas_with_pending_work()`.
- That optional PostgreSQL routine is intended for deployments that scan multiple schemas. If it is present but only returns `tenant_%` schemas, the worker will never service `public.async_operations`.
- Symptom: worker logs keep showing `global: pending=0`, while the database still has real `pending` rows in `public.async_operations`.

## Recovery if async tasks stop moving

If tasks remain pending for a long time, check whether `public.schemas_with_pending_work()` is installed incorrectly:

```bash
docker exec hindsight-db psql -U hindsight -d hindsight -c "\df+ public.schemas_with_pending_work"
```

If this is a single-schema deployment and the function exists, remove it and restart the app container:

```bash
docker exec hindsight-db psql -U hindsight -d hindsight -c "DROP FUNCTION IF EXISTS public.schemas_with_pending_work();"
docker compose restart hindsight
docker logs --tail 120 hindsight-app
```

Expected recovery signs:

- Worker logs show claimed tasks again, for example `Worker hindsight-app claimed ...`.
- Previously stuck rows in `public.async_operations` move from `pending` to `completed`.
