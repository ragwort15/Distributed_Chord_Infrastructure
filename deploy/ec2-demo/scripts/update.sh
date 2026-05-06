#!/bin/bash
# =============================================================================
# update.sh — Push code changes to a running EC2 instance and restart services
#
# Use this for day-to-day code changes (Python files, Dockerfile tweaks, etc.)
# NO teardown needed — the EC2 instance stays running.
#
# Usage:
#   cd Distributed_Chord_Infrastructure
#   bash deploy/ec2-demo/scripts/update.sh
# =============================================================================

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; NC='\033[0m'

info()    { echo -e "${BLUE}[INFO]${NC} $*"; }
success() { echo -e "${GREEN}[OK]${NC}   $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }
error()   { echo -e "${RED}[ERR]${NC}  $*"; exit 1; }
step()    { echo -e "\n${CYAN}══ $* ══${NC}"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
TERRAFORM_DIR="$SCRIPT_DIR/../terraform"
KEY_FILE="$SCRIPT_DIR/../chord-demo-key.pem"

ssh_run() {
  ssh -o StrictHostKeyChecking=no \
      -o ConnectTimeout=10 \
      -o BatchMode=yes \
      -i "$KEY_FILE" \
      "ec2-user@$PUBLIC_IP" "$@"
}

# ── Get the existing EC2 IP from Terraform state ──────────────────────────────
step "Reading EC2 IP from Terraform state"

cd "$TERRAFORM_DIR"
PUBLIC_IP=$(terraform output -raw instance_public_ip 2>/dev/null) || \
  error "No Terraform state found. Run deploy.sh first to provision the instance."

info "Target instance: $PUBLIC_IP"

# Verify it's reachable
ssh_run "true" 2>/dev/null || \
  error "Cannot SSH to $PUBLIC_IP. Is the instance running? Check AWS console."

success "Instance is reachable"

# ── Sync changed files ────────────────────────────────────────────────────────
step "Syncing code to EC2 (changed files only)"

cd "$REPO_ROOT"

rsync -az \
  --exclude='.venv/' \
  --exclude='venv/' \
  --exclude='.git/' \
  --exclude='__pycache__/' \
  --exclude='*.pyc' \
  --exclude='.pytest_cache/' \
  --exclude='*.log' \
  --exclude='*.jsonl' \
  --exclude='node_modules/' \
  -e "ssh -o StrictHostKeyChecking=no -i $KEY_FILE" \
  --progress \
  . \
  "ec2-user@$PUBLIC_IP:/home/ec2-user/chord-app/"

success "Code synced"

# ── Rebuild image and restart containers ──────────────────────────────────────
step "Rebuilding Docker image and restarting services"

ssh_run bash << 'REMOTE'
set -e
cd /home/ec2-user/chord-app

echo "[remote] Rebuilding image..."
sudo docker build -f Dockerfile.demo -t chord-dht:demo . 2>&1 | tail -15

echo "[remote] Restarting services (zero-downtime rolling restart)..."
sudo docker compose \
  -f deploy/ec2-demo/docker-compose.demo.yml \
  up -d --remove-orphans --build

echo "[remote] Waiting 20 s for nodes to stabilise..."
sleep 20

echo "[remote] Container status:"
sudo docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
REMOTE

success "Services restarted"

# ── Quick health check ────────────────────────────────────────────────────────
step "Verifying node health"

sleep 5
if curl -sf "http://$PUBLIC_IP:5001/chord/ping" >/dev/null 2>&1; then
  success "Node 1 is healthy ✓"
else
  warn "Node 1 not responding yet — may still be restarting, check in ~30 s"
fi

echo ""
echo -e "${GREEN}Update complete!${NC}"
echo ""
echo -e "  ${CYAN}Dashboard:${NC}  http://$PUBLIC_IP:5001"
echo -e "  ${CYAN}Grafana:${NC}    http://$PUBLIC_IP:3000"
echo ""
echo -e "  ${YELLOW}Live logs:${NC}"
echo -e "    ssh -i deploy/ec2-demo/chord-demo-key.pem ec2-user@$PUBLIC_IP"
echo -e "    sudo docker compose -f chord-app/deploy/ec2-demo/docker-compose.demo.yml logs -f"
echo ""
