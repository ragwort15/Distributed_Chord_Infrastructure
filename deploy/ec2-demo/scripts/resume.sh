#!/bin/bash
# =============================================================================
# resume.sh — Start a paused EC2 instance and verify the Chord ring is up
#
# Companion to pause.sh. The instance already has Docker + the app image —
# containers auto-restart because restart: unless-stopped is set in Compose.
# This script just starts the instance, waits for SSH, and verifies health.
#
# Usage:
#   cd Distributed_Chord_Infrastructure
#   bash deploy/ec2-demo/scripts/resume.sh
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
TERRAFORM_DIR="$SCRIPT_DIR/../terraform"
KEY_FILE="$SCRIPT_DIR/../chord-demo-key.pem"

ssh_run() {
  ssh -o StrictHostKeyChecking=no \
      -o ConnectTimeout=10 \
      -o BatchMode=yes \
      -i "$KEY_FILE" \
      "ec2-user@$PUBLIC_IP" "$@"
}

# ── Get instance details from Terraform state ─────────────────────────────────
step "Reading instance details from Terraform state"

cd "$TERRAFORM_DIR"
INSTANCE_ID=$(terraform output -raw instance_id 2>/dev/null) || \
  error "No Terraform state found. Run deploy.sh to create a new instance."

info "Instance : $INSTANCE_ID"

# ── Check current state ───────────────────────────────────────────────────────
CURRENT_STATE=$(aws ec2 describe-instances \
  --instance-ids "$INSTANCE_ID" \
  --query 'Reservations[0].Instances[0].State.Name' \
  --output text 2>/dev/null) || error "Could not query instance state. Check AWS credentials."

if [[ "$CURRENT_STATE" == "running" ]]; then
  PUBLIC_IP=$(terraform output -raw instance_public_ip)
  warn "Instance is already running at $PUBLIC_IP"
  echo ""
  echo -e "  Dashboard: ${CYAN}http://$PUBLIC_IP:5001${NC}"
  echo -e "  Grafana:   ${CYAN}http://$PUBLIC_IP:3000${NC}"
  exit 0
fi

if [[ "$CURRENT_STATE" == "terminated" ]]; then
  error "Instance has been terminated. Run deploy.sh to provision a new one."
fi

if [[ "$CURRENT_STATE" != "stopped" ]]; then
  error "Instance is in state '$CURRENT_STATE' — expected 'stopped'. Wait and retry."
fi

# ── Start the instance ────────────────────────────────────────────────────────
step "Starting instance"

aws ec2 start-instances --instance-ids "$INSTANCE_ID" --output text >/dev/null
info "Start signal sent — waiting for instance to reach 'running' state..."

aws ec2 wait instance-running --instance-ids "$INSTANCE_ID"
success "Instance is running"

# The Elastic IP is persistent — it stays associated across stop/start
PUBLIC_IP=$(terraform output -raw instance_public_ip)
info "Public IP: $PUBLIC_IP (Elastic IP — unchanged)"

# ── Wait for SSH ──────────────────────────────────────────────────────────────
step "Waiting for SSH"

for i in $(seq 1 18); do   # 18 × 10s = 3 min max
  if ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 \
         -o BatchMode=yes \
         -i "$KEY_FILE" \
         "ec2-user@$PUBLIC_IP" "true" 2>/dev/null; then
    success "SSH is up (attempt $i/18)"
    break
  fi
  echo "  Attempt $i/18 — waiting for SSH..."
  sleep 10
done

# ── Check Docker and containers ───────────────────────────────────────────────
step "Checking container status"

# Docker starts automatically (systemctl enabled). Containers auto-restart
# because restart: unless-stopped is set in the Compose file.
info "Waiting 20 s for containers to come up..."
sleep 20

ssh_run bash << 'REMOTE'
echo "[remote] Container status:"
sudo docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
REMOTE

# ── Health check ──────────────────────────────────────────────────────────────
step "Verifying node health"

sleep 10
if curl -sf "http://$PUBLIC_IP:5001/chord/ping" >/dev/null 2>&1; then
  success "Node 1 is healthy ✓"
else
  warn "Node 1 not responding yet — containers may still be starting."
  warn "Try again in ~30 seconds: curl http://$PUBLIC_IP:5001/chord/ping"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}Instance resumed.${NC}"
echo ""
echo -e "  ${CYAN}Dashboard:${NC}  http://$PUBLIC_IP:5001"
echo -e "  ${CYAN}Grafana:${NC}    http://$PUBLIC_IP:3000  (admin/admin)"
echo -e "  ${CYAN}Prometheus:${NC} http://$PUBLIC_IP:9090"
echo ""
echo -e "  ${CYAN}nip.io DNS:${NC} http://$PUBLIC_IP.nip.io:5001  (no setup needed)"
echo ""
echo -e "  ${YELLOW}SSH in:${NC}"
echo -e "    ssh -i deploy/ec2-demo/chord-demo-key.pem ec2-user@$PUBLIC_IP"
echo ""
echo -e "  ${YELLOW}Pause again when done:${NC}"
echo -e "    bash deploy/ec2-demo/scripts/pause.sh"
echo ""
