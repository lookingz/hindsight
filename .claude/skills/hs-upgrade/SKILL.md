---
name: hs-upgrade
description: Sync upstream vectorize-io/hindsight into this fork, merge into main-cn-llm-api, resolve conflicts, deploy to the CN host, and smoke test. Use when asked to "升级Hindsight", "同步上游", "sync/merge upstream", "update from upstream", or deploy an upstream update.
user_invocable: true
---

# Hindsight Upgrade (upstream sync -> CN merge -> deploy)

Run the full upgrade pipeline: fast-forward `main` to `upstream/main`, merge into
`main-cn-llm-api`, verify the CN adaptations survived, push, deploy to the CN
host, and smoke test. Work happens locally; the deploy host only runs pushed code
(configured via `DEPLOY_HOST`/`DEPLOY_DIR`, see `.deploy-env.local`).

## Branch model

- `main` - pure mirror of `upstream/main` (vectorize-io/hindsight). Never
  carries fork commits; never merges `origin/main` or CN work into it.
- `main-cn-llm-api` - deployment branch. CN adaptations (China mirrors,
  deploy Dockerfile, litellm-sdk batch size) + merges of `main`.
- Deployment docs: `docker/standalone/DEPLOYMENT_CN.md`.
- Fork Actions are DISABLED at the repo level: the inherited "Update star
  history" workflow would otherwise auto-commit to `origin/main` and break
  the mirror. If drift ever reappears, check
  `gh api repos/<owner>/<repo>/actions/permissions` first.

## Step 0 - Pre-flight

1. Clean working tree on `main-cn-llm-api`. Nothing uncommitted.
2. Create a backup branch:
   `git branch backup/main-cn-llm-api-before-upstream-sync-$(date +%Y%m%d)`.
3. **Network pitfalls** (this network is flaky, commands may need 1-2 retries):
   - `upstream` remote is `https://github.com/vectorize-io/hindsight.git`;
     HTTPS may time out - retry, or use
     `git@github.com:vectorize-io/hindsight.git` (SSH works intermittently).
   - Consider routing origin over `ssh://git@ssh.github.com:443/<owner>/<repo>.git`
     (port 443) if direct `git@github.com:` URLs time out on your network.
   - Failures like "Repository not found" / "Operation timed out" from GitHub
     are usually transient - retest with `ssh -T git@github.com` before
     concluding anything is broken.

## Step 1 - Sync `main`

```bash
git fetch upstream
git rev-list --left-right --count main...upstream/main   # expect "0 N"
```

- If the left count is 0, `git checkout main && git merge --ff-only upstream/main`.
- If not 0, STOP: `main` has fork drift. Back it up first
  (`git branch backup/origin-main-fork-commits-$(date +%Y%m%d) origin/main`),
  report to the user, and ask before diverging from the mirror rule.
- Push `main` to origin (normal push if it is an ancestor of `origin/main`;
  otherwise back up `origin/main` first, then `--force-with-lease`).

## Step 2 - Merge into `main-cn-llm-api`

```bash
git checkout main-cn-llm-api
git merge --no-ff main -m "merge: sync upstream main into main-cn-llm-api"
```

Expect few or no textual conflicts - CN adaptations live mostly in files
upstream never touches. Resolve any conflicts keeping BOTH sides' intent:
CN deployment config stays, upstream feature code comes in.

## Step 3 - Verify (ALWAYS, even after a clean auto-merge)

1. **CN adaptation files intact** (diff merge-result vs pre-merge CN branch -
   must be empty for each):
   `docker-compose.yaml`, `docker-compose.override.yaml.example`,
   `docker/standalone/Dockerfile.deploy`, `docker/standalone/DEPLOYMENT_CN.md`,
   `.gitignore`, `hindsight-api-slim/tests/test_db_utils.py`,
   `hindsight-api-slim/tests/test_litellm_sdk_embeddings.py`.
2. **Key semantic check** - `hindsight-api-slim/hindsight_api/engine/embeddings.py`,
   the `provider == "litellm-sdk"` factory branch must still pass
   `batch_size=config.embeddings_openai_batch_size`. Upstream features
   (e.g. `query_prefix`/`passage_prefix`) may add sibling params - both must
   coexist. The CN deployment sets
   `HINDSIGHT_API_EMBEDDINGS_OPENAI_BATCH_SIZE: 10` in the host override.
3. **No delete/modify conflicts**: files upstream deleted must not appear in
   the CN-side changed set since the merge-base.
4. **Tests**:
   ```bash
   uv sync --package hindsight-api-slim
   cd hindsight-api-slim && uv run --package hindsight-api-slim python -m pytest \
     tests/test_litellm_sdk_embeddings.py tests/test_embeddings_asymmetric_prefixes.py -q
   ```
   (Cohere-key skips are expected; 0 failures required.)
5. **Deployment-impacting upstream changes**: skim the new commits for
   `worker/poller.py`, tenant extensions, or docker changes. The deployment is
   single-schema (`public`, `DefaultTenantExtension`); `public.schemas_with_pending_work()`
   must NOT be installed there (see DEPLOYMENT_CN.md caveat). Upstream poller
   refactors need a read-through before deploying.

## Step 4 - Push

```bash
git push origin main-cn-llm-api   # retry on transient SSH failure
git push origin main
```

## Step 5 - Deploy

From the local checkout (the script SSHes to `DEPLOY_HOST` itself):

```bash
scripts/deploy-cn.sh            # fetch+reset+build+up+health check on host
```

- Build failure leaves the running version untouched (build happens before
  `up -d`). Health-check failure suggests `scripts/deploy-cn.sh rollback`.
- `DEPLOY_HOST`/`DEPLOY_DIR`/`DEPLOY_BRANCH` env vars override defaults
  (a gitignored `.deploy-env.local` at the repo root can hold them).
- The db container keeps running across deploys; only `hindsight` is rebuilt.

## Step 6 - Smoke test

On the host:

```bash
ssh "$DEPLOY_HOST" "cd $DEPLOY_DIR && ./scripts/smoke-test-slim.sh http://localhost:8888"
```

Pass criteria: retain `success: true` with token usage (LLM provider works),
recall returns >= 1 result with non-zero `semantic` AND `reranker` scores
(embeddings + local reranker both work). Then delete the `smoke-test-*` bank:
list with `GET /v1/default/banks` (field name is `bank_id`, not `id`), then
`DELETE /v1/default/banks/<bank_id>`. Known benign log noise: asyncio
"Unclosed client session".

## Boundaries

- Only deploy code that passed Step 3; never deploy around a failed check.
- Do not edit code on the deploy host - the deploy script resets its checkout to
  `origin/main-cn-llm-api`, host-local edits would be lost anyway.
- Do not commit `.env` or `docker-compose.override.yaml` (gitignored,
  host-local only).
- Rollback is `scripts/deploy-cn.sh rollback` (rebuilds the last deployed
  commit).
