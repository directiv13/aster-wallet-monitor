#!/usr/bin/env bash
#
# One-time provisioning for a fresh Ubuntu 22.04/24.04 LTS box to host the
# AsterDEX tracker bot under Docker. Idempotent: safe to re-run.
#
# Run as a sudo-capable user (NOT inside a container):
#     curl -fsSL .../provision.sh | sudo bash        # or
#     sudo ./provision.sh
#
# Override the deploy user with DEPLOY_USER=name sudo ./provision.sh
#
set -euo pipefail

DEPLOY_USER="${DEPLOY_USER:-deploy}"

log() { printf '\n\033[1;32m==>\033[0m %s\n' "$*"; }

if [[ $EUID -ne 0 ]]; then
    echo "This script must run as root (use sudo)." >&2
    exit 1
fi

# --- base packages ----------------------------------------------------------
log "Updating apt and installing base packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get -y upgrade
apt-get install -y --no-install-recommends \
    ca-certificates curl git ufw unattended-upgrades

# --- Docker Engine + Compose plugin (official repo, not the distro package) --
if ! command -v docker >/dev/null 2>&1; then
    log "Installing Docker Engine from Docker's official apt repository"
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc

    # shellcheck source=/dev/null
    . /etc/os-release
    echo \
"deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
        > /etc/apt/sources.list.d/docker.list

    apt-get update
    apt-get install -y \
        docker-ce docker-ce-cli containerd.io \
        docker-buildx-plugin docker-compose-plugin
else
    log "Docker already installed; skipping engine install"
fi

# Docker's service is enabled + started on install, but be explicit so a reboot
# always brings the bot back up.
systemctl enable --now docker

# --- deploy user ------------------------------------------------------------
if ! id -u "$DEPLOY_USER" >/dev/null 2>&1; then
    log "Creating deploy user '$DEPLOY_USER'"
    adduser --disabled-password --gecos "" "$DEPLOY_USER"
else
    log "User '$DEPLOY_USER' already exists"
fi
log "Adding '$DEPLOY_USER' to the docker group"
usermod -aG docker "$DEPLOY_USER"

# --- unattended security upgrades -------------------------------------------
log "Enabling unattended security upgrades"
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
systemctl enable --now unattended-upgrades || true

# --- time sync (fills-window logic depends on an accurate clock) ------------
log "Ensuring NTP time sync is on"
timedatectl set-ntp true || true

# --- firewall: deny inbound except (rate-limited) SSH -----------------------
log "Configuring UFW (deny inbound, allow outbound, allow SSH)"
ufw default deny incoming
ufw default allow outgoing
ufw limit OpenSSH        # rate-limit SSH to blunt brute-force attempts
ufw --force enable
ufw status verbose || true

# --- advisory SSH hardening (NOT applied, to avoid locking you out) ---------
cat <<'EOF'

============================================================================
 Provisioning complete.

 RECOMMENDED SSH HARDENING (do this manually once key auth works):
   In /etc/ssh/sshd_config set:
       PasswordAuthentication no
       PermitRootLogin no
       PubkeyAuthentication yes
   Then: sudo systemctl restart ssh
   Confirm you can still log in with your key BEFORE closing this session.

 NEXT STEPS (as the deploy user):
   1. Log out and back in (or: `newgrp docker`) so docker-group membership
      takes effect, then verify:
          docker run --rm hello-world
   2. Clone the repo and deploy — see DEPLOY.md:
          git clone <repo-url> ~/aster-whale-monitor
          cd ~/aster-whale-monitor
          cp .env.example .env && chmod 600 .env   # fill in real values
          docker compose up -d --build
============================================================================
EOF
