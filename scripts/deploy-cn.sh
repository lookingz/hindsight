#!/usr/bin/env bash
# Deploy the CN branch to the deployment host over SSH.
#
# The deployment host never carries local edits: its checkout is reset to
# origin/<branch> on every deploy, so the remote branch is the single source
# of truth. Gitignored files (.env, docker-compose.override.yaml) are the
# only host-local state.
#
# Usage:
#   scripts/deploy-cn.sh                # deploy latest origin/main-cn-llm-api
#   scripts/deploy-cn.sh rollback       # rebuild the previously deployed commit
#   scripts/deploy-cn.sh status         # show local / deployed / available commits
#
# Configuration: set in the environment, or in a gitignored .deploy-env.local
# file at the repo root (sourced automatically, KEY=VALUE per line):
#   DEPLOY_HOST    SSH host to deploy to          (required)
#   DEPLOY_DIR     repo path on the deploy host   (default: $HOME/hindsight)
#   DEPLOY_BRANCH  branch to deploy               (default: main-cn-llm-api)
#   API_PORT       API port for health checks     (default: 8888)
#
# Rollback safety: the container is only replaced after a successful build,
# so a failed build leaves the running version untouched. A failed health
# check after `up -d` is reported with a rollback hint.

set -euo pipefail

if [ -f .deploy-env.local ]; then
    # shellcheck disable=SC1091
    . ./.deploy-env.local
fi

DEPLOY_BRANCH="${DEPLOY_BRANCH:-main-cn-llm-api}"
DEPLOY_DIR="${DEPLOY_DIR:-$HOME/hindsight}"
API_PORT="${API_PORT:-8888}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-300}"
STATE_FILE=".deploy-last"

if [ -z "${DEPLOY_HOST:-}" ]; then
    echo "error: DEPLOY_HOST is not set." >&2
    echo "Set it in the environment or in .deploy-env.local, e.g.:" >&2
    echo "  echo 'DEPLOY_HOST=myserver' >> .deploy-env.local" >&2
    echo "  echo 'DEPLOY_DIR=/srv/hindsight' >> .deploy-env.local" >&2
    exit 2
fi

log() { printf '\033[1;34m[deploy]\033[0m %s\n' "$*"; }
err() { printf '\033[1;31m[deploy]\033[0m %s\n' "$*" >&2; }

remote() {
    ssh -o ConnectTimeout=15 "$DEPLOY_HOST" "cd '$DEPLOY_DIR' && $*"
}

health_check() {
    log "waiting for API on port $API_PORT (timeout ${HEALTH_TIMEOUT}s)..."
    if remote "for i in \$(seq 1 $HEALTH_TIMEOUT); do
        curl -sf -o /dev/null http://localhost:$API_PORT/health/live && exit 0
        sleep 1
    done; exit 1"; then
        log "API is live."
    else
        err "API did not become live within ${HEALTH_TIMEOUT}s."
        err "Last 30 log lines follow:"
        remote "docker compose logs --tail 30 hindsight" || true
        err "Roll back with: scripts/deploy-cn.sh rollback"
        exit 1
    fi
    remote "curl -sf http://localhost:$API_PORT/health >/dev/null" \
        && log "Readiness check (DB) passed." \
        || err "Readiness check (DB) still failing - API is up, check 'docker compose logs hindsight'."
}

deploy() {
    log "host=$DEPLOY_HOST dir=$DEPLOY_DIR branch=$DEPLOY_BRANCH"

    log "fetching origin/$DEPLOY_BRANCH..."
    remote "git fetch origin '$DEPLOY_BRANCH'"

    if ! remote "git diff --quiet HEAD -- && [ -z \"\$(git status --porcelain --untracked-files=no)\" ]"; then
        err "deploy host has local modifications to tracked files - resolve manually before deploying."
        remote "git status --short --untracked-files=no" || true
        exit 1
    fi

    local new_commit old_commit
    new_commit=$(remote "git rev-parse origin/$DEPLOY_BRANCH")
    old_commit=$(remote "git rev-parse HEAD")
    log "current:  $old_commit"
    log "deploying: $new_commit"

    if [ "$old_commit" != "$new_commit" ]; then
        remote "git checkout '$DEPLOY_BRANCH' >/dev/null 2>&1 || git checkout -B '$DEPLOY_BRANCH' 'origin/$DEPLOY_BRANCH'"
        remote "git reset --hard 'origin/$DEPLOY_BRANCH'" >/dev/null
        remote "git rev-parse HEAD > '$STATE_FILE'"
    else
        log "already at origin/$DEPLOY_BRANCH - rebuilding only."
    fi

    log "building image (this can take a few minutes)..."
    remote "docker compose build hindsight"

    log "starting services..."
    remote "docker compose up -d"

    health_check

    log "deployed: $(remote "git log --oneline -1")"
}

rollback() {
    local target
    target=$(remote "cat '$STATE_FILE' 2>/dev/null || true")
    if [ -z "$target" ]; then
        err "no previous deployment recorded in $STATE_FILE."
        exit 1
    fi
    log "rolling back to $target"
    remote "git reset --hard '$target'" >/dev/null
    remote "docker compose build hindsight"
    remote "docker compose up -d"
    health_check
    log "rolled back to: $(remote "git log --oneline -1")"
}

status() {
    log "local:      $(git rev-parse --short HEAD) ($(git rev-parse --abbrev-ref HEAD))"
    log "on host:    $(remote "git log --oneline -1")"
    log "last-good:  $(remote "git rev-parse --short \$(cat '$STATE_FILE' 2>/dev/null) 2>/dev/null" || echo "n/a")"
    log "available:  $(remote "git log --oneline -1 'origin/$DEPLOY_BRANCH'")"
}

case "${1:-deploy}" in
    deploy)   deploy ;;
    rollback) rollback ;;
    status)   status ;;
    *)
        echo "usage: scripts/deploy-cn.sh [deploy|rollback|status]" >&2
        exit 2
        ;;
esac
