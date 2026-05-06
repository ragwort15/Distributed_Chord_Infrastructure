#!/bin/bash
# =============================================================================
# EC2 Bootstrap Script (runs as user-data on first boot)
# Installs Docker + Docker Compose, prepares the environment.
# The actual app files are pushed by deploy.sh after this runs.
# =============================================================================

set -euo pipefail
exec > >(tee /var/log/chord-bootstrap.log | logger -t chord-bootstrap) 2>&1

echo "[bootstrap] Starting Chord DHT demo bootstrap at $(date)"

# ---------------------------------------------------------------------------
# 1. System updates
# ---------------------------------------------------------------------------
# Amazon Linux 2023 ships with curl-minimal. The full 'curl' package conflicts
# with it, so we use --allowerasing to let dnf swap the minimal variant out.
# We only install what's actually needed: docker, git, htop.
# curl-minimal is already present and sufficient for the bootstrap curl calls.
# ---------------------------------------------------------------------------
dnf update -y
dnf install -y --allowerasing \
    docker \
    git \
    htop

echo "[bootstrap] Packages installed"

# ---------------------------------------------------------------------------
# 2. Docker setup
# ---------------------------------------------------------------------------
systemctl enable docker
systemctl start docker

# Add ec2-user to docker group (takes effect on next login; deploy.sh uses sudo)
usermod -aG docker ec2-user

echo "[bootstrap] Docker started"

# ---------------------------------------------------------------------------
# 3. Docker Compose v2 (plugin style)
# ---------------------------------------------------------------------------
COMPOSE_VERSION="v2.27.0"
mkdir -p /usr/local/lib/docker/cli-plugins

curl -fsSL \
  "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-x86_64" \
  -o /usr/local/lib/docker/cli-plugins/docker-compose

chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

echo "[bootstrap] Docker Compose installed: $(docker compose version)"

# ---------------------------------------------------------------------------
# 4. Create app directory with right ownership
# ---------------------------------------------------------------------------
mkdir -p /home/ec2-user/chord-app
chown -R ec2-user:ec2-user /home/ec2-user/chord-app

# ---------------------------------------------------------------------------
# 5. Signal that bootstrap is complete
# ---------------------------------------------------------------------------
touch /tmp/bootstrap-complete
echo "[bootstrap] Bootstrap complete at $(date)"
echo "[bootstrap] Docker  : $(docker --version)"
echo "[bootstrap] Compose : $(docker compose version)"
