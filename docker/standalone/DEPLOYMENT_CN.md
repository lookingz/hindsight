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

## What `Dockerfile.deploy` does

- Switches Debian and Python package downloads to China mirrors.
- Uses `BUILD_PROXY` for build-time `apt`, `npm`, and model download traffic.
- Supports `HF_ENDPOINT` and `HF_TOKEN` for Hugging Face model downloads.
- Builds the standalone image with API + control plane.
- Keeps local model dependencies enabled so `HINDSIGHT_API_RERANKER_PROVIDER=local` can run inside the container.

## Recommended `.env` items

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
- `HF_ENDPOINT`
- `HF_TOKEN`
- `BUILD_PROXY`

## First-time deploy

```bash
git checkout main-cn-llm-api
cp docker-compose.override.yaml.example docker-compose.override.yaml
docker compose build hindsight
docker compose up -d
docker compose ps
docker compose logs -f hindsight
```

## Re-deploy after updates

Development happens locally; the deploy host only runs code that was pushed to
`origin/main-cn-llm-api`. From a local checkout:

```bash
git push origin main-cn-llm-api
scripts/deploy-cn.sh
```

The script SSHes into the deploy host (set `DEPLOY_HOST` and optionally
`DEPLOY_DIR` in the environment or a gitignored `.deploy-env.local`), resets
its checkout to `origin/main-cn-llm-api`, rebuilds, restarts, and waits for the
API health check. Gitignored host-local files (`.env`,
`docker-compose.override.yaml`) are left untouched.

Equivalent manual steps on the host:

```bash
git checkout main-cn-llm-api
git fetch origin
git reset --hard origin/main-cn-llm-api
docker compose build hindsight
docker compose up -d
```

Rollback rebuilds the previously deployed commit:

```bash
scripts/deploy-cn.sh rollback
```

## Verification

- API: `http://localhost:8888` (liveness: `/health/live`, readiness: `/health`)
- Control plane: `http://localhost:9999`
- Database container should be `healthy`

## Notes

- Do not commit `.env` or `docker-compose.override.yaml`.
- The control-plane `npm install` stage can still be slow on first build.
- Local reranker works better through Docker than host-source startup because the image includes the needed local ML dependencies.
- Set a stable `HINDSIGHT_API_WORKER_ID` for Docker deployments. Otherwise the worker defaults to the container hostname, and recreated containers can leave async tasks stuck in `processing`.

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
