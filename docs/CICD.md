# CI/CD: GitHub Actions → deploy to the VPS

How the AsterDEX tracker bot is tested and deployed. The pipeline lives in
[`.github/workflows/deploy.yml`](../.github/workflows/deploy.yml) and the on-box
deploy logic in [`scripts/deploy.sh`](../scripts/deploy.sh). This complements
[`DEPLOY.md`](../DEPLOY.md), which covers the manual/first-time VPS setup.

## Architecture

- **`ci` job** (every trigger — PRs, pushes, manual): `ruff` lint → `pytest` →
  a Docker **image-build validation** (buildx, no push, GitHub Actions layer
  cache). `smoke.py` is deliberately excluded — it calls the live Aster endpoint,
  which bans abusive IPs.
- **`deploy` job** (only on push to `main` and the manual **Run workflow**
  button): SSH into the VPS and run `scripts/deploy.sh`, which updates the
  checkout, rebuilds/restarts with `docker compose up -d --build` (**build on the
  box**, matching the existing setup), waits for the container `HEALTHCHECK` to
  report `healthy`, and **auto-rolls-back** to the previous commit on failure.

**Secrets model:** the app's `.env` (Telegram token, chat id, admin ids) lives
**only on the VPS** (chmod 600) and is never stored in GitHub or written by the
pipeline. GitHub holds **only what's needed to reach the box** (SSH).

**Deploys are serialized:** the `deploy` job uses
`concurrency: { group: production-deploy, cancel-in-progress: false }`, so two
deploys queue instead of overlapping. This is set at the **job** level (not
top-level) on purpose — top-level concurrency would also make unrelated PR CI
runs queue behind a deploy. Deploys still never overlap.

**Supply chain:** every third-party action is pinned to a full commit SHA (with
the version tag in a trailing comment).

## Triggers

| Event | `ci` | `deploy` |
|-------|------|----------|
| Pull request | ✅ | ❌ |
| Push to `main` | ✅ | ✅ (deploys the pushed commit) |
| **Run workflow** (manual) | ✅ | ✅ (deploys the `ref` input, default `main`) |

## Required GitHub secrets

Add these under **Settings → Environments → `production` → Environment
secrets** (scoping them to the environment lets you add a required-reviewer gate
later):

| Secret | What it is |
|--------|-----------|
| `SSH_HOST` | VPS hostname or IP |
| `SSH_USER` | the non-root **deploy user** (in the `docker` group) |
| `SSH_PORT` | SSH port (optional; defaults to `22`) |
| `SSH_PRIVATE_KEY` | private key of the **Actions deploy key** (see below) |
| `SSH_HOST_FINGERPRINT` | the server's SHA256 host-key fingerprint (host-key pinning) |
| `DEPLOY_PATH` | absolute path to the repo checkout on the VPS, e.g. `/home/deploy/aster-whale-monitor` |

> **No app secrets here.** There is intentionally no `TELEGRAM_BOT_TOKEN` etc. in
> GitHub — those stay in `.env` on the VPS.

> **Host-key pinning:** this pipeline uses `appleboy/ssh-action`, which verifies
> the server by **SHA256 fingerprint** (`SSH_HOST_FINGERPRINT`) rather than a
> `known_hosts` file. `StrictHostKeyChecking=no` is never used.

## One-time setup

### 1. On the VPS

Assumes the box was provisioned per [`DEPLOY.md`](../DEPLOY.md) (Docker installed,
deploy user in the `docker` group).

```bash
# As the deploy user:
git clone <repo-url> ~/aster-whale-monitor        # DEPLOY_PATH
cd ~/aster-whale-monitor
cp .env.example .env && nano .env && chmod 600 .env   # real values; stays on the box
```

The checkout must contain `scripts/deploy.sh` (it does, once this branch is
merged). The bot can be started once by hand (`docker compose up -d --build`) or
left for the first pipeline deploy to bring up.

### 2. (Private repos only) read-only git deploy key

So the VPS can `git fetch` non-interactively. **This is a separate key from the
Actions `SSH_PRIVATE_KEY`** — it authenticates the *box → GitHub* pull, not
*GitHub → box*.

```bash
# On the VPS, as the deploy user:
ssh-keygen -t ed25519 -f ~/.ssh/github_deploy -N "" -C "vps-git-deploy"
cat ~/.ssh/github_deploy.pub
```

Add that public key in GitHub under **Repo → Settings → Deploy keys → Add deploy
key** (read-only). Then point the repo's remote at SSH and pin the key:

```bash
git -C ~/aster-whale-monitor remote set-url origin git@github.com:<owner>/<repo>.git
cat >> ~/.ssh/config <<'EOF'
Host github.com
    IdentityFile ~/.ssh/github_deploy
    IdentitiesOnly yes
EOF
ssh -T git@github.com   # accept the host key once
```

### 3. Actions SSH deploy key (GitHub → VPS)

```bash
# On your workstation:
ssh-keygen -t ed25519 -f actions_deploy -N "" -C "github-actions-deploy"
# Public key -> the deploy user's authorized_keys on the VPS:
ssh-copy-id -i actions_deploy.pub deploy@<host>   # or append manually
# Private key -> the SSH_PRIVATE_KEY secret:
cat actions_deploy        # paste into the GitHub secret, then delete the local copy
```

Advisory hardening — lock the key down in the deploy user's
`~/.ssh/authorized_keys` by prefixing its line:

```
from="<runner-egress-or-*>",no-agent-forwarding,no-port-forwarding,no-x11-forwarding ssh-ed25519 AAAA... github-actions-deploy
```

(GitHub-hosted runners have no fixed egress IP; `from=` is only practical with a
self-hosted runner or a static NAT. The `no-*-forwarding` restrictions are always
worth applying.)

### 4. Host-key fingerprint

```bash
ssh-keyscan -p <port> <host> | ssh-keygen -lf -
# Use the SHA256:... value from the ed25519 line as SSH_HOST_FINGERPRINT.
```

### 5. `production` environment

**Settings → Environments → New environment → `production`.** Add the secrets
above. Optionally add **Required reviewers** so every deploy (including the manual
button) waits for approval.

## Running a deploy

- **Automatic:** merge/push to `main`. `ci` runs, then `deploy`.
- **Manual:** **Actions → CI / Deploy → Run workflow**, optionally set `ref` to a
  branch/tag/commit.

Watch it: the Actions run shows lint/test/build, then the SSH deploy streams
`git reset`, `docker compose up -d --build`, and the health-wait. A green run
means the container reported `healthy`.

## Rollback

- **Automatic:** if the new container never becomes `healthy` (or reports
  `unhealthy`) within the timeout, `deploy.sh` resets to the previous commit,
  rebuilds, and exits non-zero — the GitHub job goes **red** and the bot is back
  on the last-good version.
- **Manual (redeploy an old version):** **Run workflow** with `ref` set to a
  known-good commit SHA.
- **Manual (on the box):**
  ```bash
  cd "$DEPLOY_PATH"
  git reset --hard <good-sha>
  docker compose up -d --build
  ```

State is safe throughout: `deploy.sh` never touches the `aster-tracker-data`
volume, so tracked wallets and last-seen state survive deploys and rollbacks, and
no duplicate notifications are produced.

## Tuning `deploy.sh`

Environment knobs (all optional, defaults in-script):

| Var | Default | Meaning |
|-----|---------|---------|
| `DEPLOY_REF` | `main` | branch/tag/SHA to deploy (set by the workflow) |
| `CONTAINER_NAME` | `aster-whale-monitor` | container to health-check |
| `HEALTH_TIMEOUT` | `180` | seconds to wait for `healthy` before rolling back |

If a large watchlist widens the poll interval, raise `HEALTH_TIMEOUT` (and/or
`HEARTBEAT_MAX_AGE_SECONDS` in `.env`, per `DEPLOY.md`) so a slow first cycle
isn't misread as a failed deploy.

## Updating the pinned action SHAs

Each `uses:` line is pinned to a commit SHA with the version in a comment. To
bump, look up the new tag's commit SHA and replace both:

```bash
gh api repos/actions/checkout/git/ref/tags/v7.0.0 --jq .object.sha
```
