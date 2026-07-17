#!/usr/bin/env bash
#
# On-box deploy for the AsterDEX tracker bot. Committed to the repo and executed
# on the VPS over SSH by the GitHub Actions `deploy` job (see
# .github/workflows/deploy.yml and docs/CICD.md).
#
# Build-on-box: updates the checkout to the target ref, rebuilds and restarts the
# container with `docker compose up -d --build`, waits for the Dockerfile
# HEALTHCHECK to report healthy, and AUTO-ROLLS-BACK to the previous commit if the
# deploy is unhealthy — exiting non-zero so the GitHub job goes red.
#
# The SQLite named volume is never touched, so tracked wallets and last-seen state
# survive every deploy and rollback.
#
# Inputs (environment):
#   DEPLOY_PATH     absolute path to the repo checkout on the VPS (required)
#   DEPLOY_REF      branch, tag, or commit SHA to deploy (default: main)
#   CONTAINER_NAME  compose container_name to health-check (default: aster-whale-monitor)
#   HEALTH_TIMEOUT  seconds to wait for `healthy` before rolling back (default: 180)
#
set -euo pipefail

cd "${DEPLOY_PATH:?DEPLOY_PATH must be set to the repo checkout on the VPS}"

DEPLOY_REF="${DEPLOY_REF:-main}"
CONTAINER="${CONTAINER_NAME:-aster-whale-monitor}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-180}"

log() { printf '\n\033[1;34m[deploy]\033[0m %s\n' "$*"; }

# The commit we can fall back to if the new one is bad.
PREV_SHA="$(git rev-parse HEAD)"
log "current commit: ${PREV_SHA}"

# Poll the container's health until it settles. Returns 0 if healthy, 1 otherwise
# (unhealthy or timed out). Relies on the Dockerfile HEALTHCHECK.
wait_for_health() {
    local deadline status
    deadline=$(( $(date +%s) + HEALTH_TIMEOUT ))
    while :; do
        status="$(docker inspect -f '{{.State.Health.Status}}' "$CONTAINER" 2>/dev/null || echo missing)"
        case "$status" in
            healthy)
                log "container is healthy"
                return 0
                ;;
            unhealthy)
                log "container reported UNHEALTHY"
                return 1
                ;;
        esac
        if [ "$(date +%s)" -ge "$deadline" ]; then
            log "timed out after ${HEALTH_TIMEOUT}s waiting for healthy (last: ${status})"
            return 1
        fi
        sleep 5
    done
}

# Reset to the last-good commit, rebuild, and fail the job. Best-effort: even if
# the rollback build hiccups we still exit non-zero so the failure is loud.
rollback() {
    log "ROLLING BACK to ${PREV_SHA}"
    git reset --hard "$PREV_SHA"
    if docker compose up -d --build; then
        wait_for_health && log "rollback is healthy" || log "WARNING: rollback did not report healthy"
    else
        log "WARNING: rollback build failed"
    fi
    docker image prune -f >/dev/null 2>&1 || true
    exit 1
}

# --- update the checkout to the requested ref -------------------------------
log "fetching and resetting to '${DEPLOY_REF}'"
git fetch --all --prune --tags
# A branch name resolves to its remote-tracking ref; a raw SHA or tag is used
# as-is. `git reset --hard main` would target a possibly-stale local branch.
if git rev-parse --verify --quiet "origin/${DEPLOY_REF}" >/dev/null; then
    TARGET="origin/${DEPLOY_REF}"
else
    TARGET="${DEPLOY_REF}"
fi
git reset --hard "$TARGET"
log "now at $(git rev-parse HEAD)"

# --- rebuild + restart (state volume preserved) -----------------------------
log "docker compose up -d --build"
docker compose up -d --build || rollback

# --- verify, or roll back ---------------------------------------------------
wait_for_health || rollback

# --- success: reclaim disk from dangling images -----------------------------
docker image prune -f >/dev/null 2>&1 || true
log "deploy succeeded: $(git rev-parse HEAD)"
