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
- Expose only control-plane port 9999 through the tunnel. Keep dataplane 8888 on
  loopback; the control plane calls it internally with the Bearer key.

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
- The base file gives only the database a restart policy; the override adds
  `restart: always` for the app so it comes back after a crash or a reboot.
- Set a stable `HINDSIGHT_API_WORKER_ID` for Docker deployments. Otherwise the worker defaults to the container hostname, and recreated containers can leave async tasks stuck in `processing`.
- The API needs working LLM and embeddings keys to start at all: it verifies both connections during startup and exits otherwise (`scripts/deploy-cn.sh` then fails its health check). To initialize a fresh database before the keys are in place, run `docker compose run --rm --no-deps --entrypoint hindsight-admin hindsight run-db-migration`.
- `LITELLM_LOCAL_MODEL_COST_MAP=True` (set by the override) skips LiteLLM's startup fetch of its price table from `raw.githubusercontent.com`, which otherwise stalls every start where that host is unreachable.

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
