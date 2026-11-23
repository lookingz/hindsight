---
name: hs-upgrade
description: Sync upstream vectorize-io/hindsight into this fork, merge into main-cn-llm-api, resolve conflicts, and verify the CN adaptations locally. Use when asked to "升级Hindsight", "同步上游", "sync/merge upstream", or "update from upstream".
user_invocable: true
---

# Hindsight Upgrade (upstream sync -> CN merge -> verify)

Run the local-only upgrade pipeline: fast-forward `main` to `upstream/main`,
merge into `main-cn-llm-api`, and verify the CN adaptations survived. Do not
deploy, do not SSH anywhere, and do not perform host-side smoke tests.

## Branch model

- `main` - pure mirror of `upstream/main` (vectorize-io/hindsight). Never
  carries fork commits; never merges `origin/main` or CN work into it.
- `main-cn-llm-api` - deployment branch. CN adaptations (China mirrors,
  deploy Dockerfile, litellm-sdk batch size) + merges of `main`.
- Deployment docs: `docker/standalone/DEPLOYMENT_CN.md`.
- Fork Actions are DISABLED at the repo level (2026-08-20): the inherited
  "Update star history" workflow used to auto-commit to `origin/main` and
  break the mirror. If drift ever reappears, check
  `gh api repos/lookingz/hindsight/actions/permissions` first.

## Step 0 - Pre-flight

1. Clean working tree on `main-cn-llm-api`. Nothing uncommitted.
   - Known-safe exception: a pending `.gitignore` edit that adds/updates the
     ignore entry for this skill's dir (`.agents/skills/hs-upgrade/`, local
     only, never published) is expected after the skill relocated from
     `.claude/skills/` - commit it as a small chore and continue.
   - Any OTHER uncommitted change: STOP and ask the user.
2. Create a backup branch (date + time, so same-day reruns don't collide):
   `git branch backup/main-cn-llm-api-before-upstream-sync-$(date +%Y%m%d-%H%M%S)`.
3. `git remote -v` may list a URL-named "remote" like
   `git@github.com:vectorize-io/hindsight.git` with no short name. That is a
   partial-clone promisor artifact, not drift - ignore it; the real remotes are
   `origin` and `upstream` only.
4. **Network pitfalls** (this network is flaky, commands may need 1-2 retries):
   - `upstream` remote is `https://github.com/vectorize-io/hindsight.git`;
     HTTPS may time out - retry, or use
     `git@github.com:vectorize-io/hindsight.git` (SSH works intermittently).
   - `origin` uses `ssh://git@ssh.github.com:443/lookingz/hindsight.git`
     (port 443). Direct `git@github.com:` URLs time out more often.
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
- Do not push during Step 1. Keep the sync local; any push belongs to Step 4
  and only happens if the user explicitly asks for it.

## Step 2 - Merge into `main-cn-llm-api`

```bash
git checkout main-cn-llm-api
git merge --no-ff main -m "merge: sync upstream main into main-cn-llm-api"
```

Expect few or no textual conflicts - CN adaptations live mostly in files
upstream never touches. Resolve any conflicts keeping BOTH sides' intent:
CN deployment config stays, upstream feature code comes in.

## Step 3 - Verify (ALWAYS, even after a clean auto-merge)

Set once for this section (BACKUP = the Step 0 branch):

```bash
BACKUP=backup/main-cn-llm-api-before-upstream-sync-<stamp>
MB=$(git merge-base "$BACKUP" main)
```

1. **CN adaptation files intact** (diff merge-result vs pre-merge CN branch).
   Two kinds of files, two different invariants:

   a. **CN-exclusive files** (upstream never touches them) - diff must be EMPTY.
      One `git diff` call with all paths; empty output = pass:
      ```bash
      git diff --name-only "$BACKUP" HEAD -- \
        docker-compose.yaml docker-compose.override.yaml.example \
        docker/standalone/Dockerfile.deploy docker/standalone/DEPLOYMENT_CN.md \
        .gitignore hindsight-api-slim/tests/test_db_utils.py
      ```

   b. **Upstream-owned files carrying CN additions** - the file MAY
      legitimately change when upstream evolves it, so an empty diff is NOT the
      invariant; every CN-added line surviving is. Known file:
      `hindsight-api-slim/tests/test_litellm_sdk_embeddings.py`
      (upstream actively develops it; e.g. #6380 legitimately added lines in
      the 2026-11-23 sync). Check per file:
      ```bash
      git diff "$MB" "$BACKUP" -- hindsight-api-slim/tests/test_litellm_sdk_embeddings.py \
        | grep '^+' | grep -v '^+++' | sed 's/^+//' \
        | while IFS= read -r line; do grep -qF -- "$line" \
            hindsight-api-slim/tests/test_litellm_sdk_embeddings.py \
            || echo "MISSING: $line"; done
      ```
      No `MISSING` output = pass.

   **Shell trap**: pass the paths as multiple arguments to ONE `git diff`.
   Never `for f in $FILES` - zsh does not word-split `$var`, so the loop runs
   ONCE with the whole string as a bogus pathspec, `git diff --quiet` exits 0,
   and every file reports a false "OK". (`$(cmd)` does split; only `$var` is
   affected. This false-pass actually happened on 2026-11-23.)
2. **Key semantic check** - `hindsight-api-slim/hindsight_api/engine/embeddings.py`,
   the `provider == "litellm-sdk"` factory branch must still pass
   `batch_size=config.embeddings_openai_batch_size`. Upstream features
   (e.g. `query_prefix`/`passage_prefix`) may add sibling params - both must
   coexist. The CN deployment sets
   `HINDSIGHT_API_EMBEDDINGS_OPENAI_BATCH_SIZE: 10` in the host override.
3. **No delete/modify conflicts**: files upstream deleted must not appear in
   the CN-side changed set since the merge-base:
   ```bash
   comm -12 <(git diff --diff-filter=D --name-only "$MB" main | sort) \
            <(git diff --name-only "$MB" "$BACKUP" | sort)
   ```
   Empty output = pass.
4. **Tests**:
   ```bash
   uv sync --package hindsight-api-slim
   cd hindsight-api-slim && uv run --package hindsight-api-slim python -m pytest \
     tests/test_litellm_sdk_embeddings.py tests/test_embeddings_asymmetric_prefixes.py -q
   ```
   (Cohere-key skips are expected; 0 failures required.)

   If a CN test fails because upstream changed factory/config semantics, adapt
   the test minimally: copy upstream's own adaptation pattern from neighboring
   tests in the SAME file, keep the CN assertion intent, commit separately as
   `test(...)`. Precedent (2026-11-23): upstream #6380's split-embeddings made
   bare-`MagicMock()` configs enter the `query_embeddings`/`passage_embeddings`
   branch, so the CN batch-size test needed upstream's
   `mock_config.query_embeddings = mock_config.passage_embeddings = None  # no split`
   line added.
5. **Runtime-impacting upstream changes**: skim the new commits for
   `worker/poller.py`, tenant extensions, or docker changes. The CN runtime is
   single-schema (`public`, `DefaultTenantExtension`); `public.schemas_with_pending_work()`
   must NOT be installed there (see DEPLOYMENT_CN.md caveat). Upstream poller
   refactors still need a read-through before considering the upgrade complete.
   Baseline verified 2026-11-23: `OptionalRoutines` only probes `pg_proc` and
   caches - it has no install API, so the runtime can never auto-install the
   routine and the `is_installed` fallback IS the CN path. Re-confirm this
   still holds after any poller/optional-routines refactor.
6. **Deploy-image references**: diff upstream changes (`git diff "$MB" main --`)
   against every path `docker/standalone/Dockerfile.deploy` COPYs - notably
   `docker/standalone/start-all.sh` (the container entrypoint). Comment-only
   changes are fine (happened 2026-11-23); functional changes need a
   read-through before the upgrade is complete.

## Step 4 - Optional push

```bash
git push origin main-cn-llm-api   # retry on transient SSH failure
git push origin main
```

- Do not push unless the user explicitly asks for it. A successful local merge
  and verification does not imply permission to publish.

## Boundaries

- Stop after local verification unless the user explicitly asks for a push.
- Do not run deployment scripts, do not SSH to any host, and do not reference
  host-local state as part of the upgrade flow.
- Do not commit `.env` or `docker-compose.override.yaml` (gitignored,
  host-local only).
