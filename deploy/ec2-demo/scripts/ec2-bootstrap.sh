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
dnf update -y
dnf install -y \
    docker \
    git \
    curl \
    wget \
    unzip \
    htop

# ---------------------------------------------------------------------------
# 2. Docker setup
# ---------------------------------------------------------------------------
systemctl enable docker
systemctl start docker

# Add ec2-user to docker group so we can run docker without sudo
usermod -aG docker ec2-user

# ---------------------------------------------------------------------------
# 3. Docker Compose v2 (plugin style)
# ---------------------------------------------------------------------------
DOCKER_COMPOSE_VERSION="v2.27.0"
mkdir -p /usr/local/lib/docker/cli-plugins
curl -SL \
  "https://github.com/docker/compose/releases/download/${DOCKER_COMPOSE_VERSION}/docker-compose-linux-x86_64" \
  -o /usr/local/lib/docker/cli-plugins/docker-compose
chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

# Verify
docker compose version

# ---------------------------------------------------------------------------
# 4. Create app directory with right ownership
# ---------------------------------------------------------------------------
mkdir -p /home/ec2-user/chord-app
chown -R ec2-user:ec2-user /home/ec2-user/chord-app

# ---------------------------------------------------------------------------
# 5. Signal that bootstrap is done
# ---------------------------------------------------------------------------
touch /tmp/bootstrap-complete
echo "[bootstrap] Bootstrap complete at $(date)"
echo "[bootstrap] Docker version: $(docker --version)"
echo "[bootstrap] Compose version: $(docker compose version)"
