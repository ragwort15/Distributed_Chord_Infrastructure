#!/bin/bash
# =============================================================================
# deploy.sh — One-command demo deployment for Chord DHT on AWS EC2
#
# Usage:
#   cd Distributed_Chord_Infrastructure
#   bash deploy/ec2-demo/scripts/deploy.sh
#
# Prerequisites (see DEPLOYMENT_GUIDE.md for install instructions):
#   - AWS CLI configured  (aws configure)
#   - Terraform >= 1.3    (brew install terraform)
#   - rsync               (pre-installed on macOS/Linux)
# =============================================================================

set -euo pipefail

# ── Colours ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; NC='\033[0m'

info()    { echo -e "${BLUE}[INFO]${NC} $*"; }
success() { echo -e "${GREEN}[OK]${NC}   $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }
error()   { echo -e "${RED}[ERR]${NC}  $*"; exit 1; }
step()    { echo -e "\n${CYAN}══ $* ══${NC}"; }

# ── Repo root detection ───────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
TERRAFORM_DIR="$SCRIPT_DIR/../terraform"
KEY_FILE="$SCRIPT_DIR/../chord-demo-key.pem"

info "Repo root : $REPO_ROOT"
info "Terraform : $TERRAFORM_DIR"

# ── Prerequisites check ───────────────────────────────────────────────────────
step "Checking prerequisites"

command -v aws       >/dev/null 2>&1 || error "aws CLI not found. Install: https://aws.amazon.com/cli/"
command -v terraform >/dev/null 2>&1 || error "terraform not found. Install: https://developer.hashicorp.com/terraform/install"
command -v rsync     >/dev/null 2>&1 || error "rsync not found. Install with: brew install rsync"
command -v ssh       >/dev/null 2>&1 || error "ssh not found."

aws sts get-caller-identity >/dev/null 2>&1 || \
  error "AWS credentials not configured. Run: aws configure"

success "All prerequisites satisfied"

# ── Step 1: Terraform ─────────────────────────────────────────────────────────
step "Step 1/5 — Provisioning EC2 with Terraform"

cd "$TERRAFORM_DIR"
terraform init -upgrade -input=false
terraform apply -auto-approve -input=false

PUBLIC_IP=$(terraform output -raw instance_public_ip)
success "EC2 provisioned — Public IP: $PUBLIC_IP"

# ── Step 2: Wait for bootstrap ────────────────────────────────────────────────
step "Step 2/5 — Waiting for EC2 bootstrap to complete (~2 min)"

info "Waiting for SSH to become available..."
for i in $(seq 1 30); do
  if ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 \
         -o BatchMode=yes \
         -i "$KEY_FILE" \
         "ec2-user@$PUBLIC_IP" "true" 2>/dev/null; then
    success "SSH is up"
    break
  fi
  echo -n "."
  sleep 10
done

info "Waiting for Docker bootstrap to finish..."
for i in $(seq 1 24); do
  if ssh -o StrictHostKeyChecking=no -i "$KEY_FILE" \
         "ec2-user@$PUBLIC_IP" \
         "test -f /tmp/bootstrap-complete" 2>/dev/null; then
    success "Bootstrap complete"
    break
  fi
  echo -n "."
  sleep 10
done

# ── Step 3: Sync code ────────────────────────────────────────────────────────
step "Step 3/5 — Uploading project files to EC2"

cd "$REPO_ROOT"

rsync -az \
  --exclude='.venv/' \
  --exclude='venv/' \
  --exclude='.git/' \
  --exclude='__pycache__/' \
  --exclude='*.pyc' \
  --exclude='.pytest_cache/' \
  --exclude='*.log' \
  --exclude='node_modules/' \
  -e "ssh -o StrictHostKeyChecking=no -i $KEY_FILE" \
  --progress \
  . \
  "ec2-user@$PUBLIC_IP:/home/ec2-user/chord-app/"

success "Code synced to /home/ec2-user/chord-app/"

# ── Step 4: Start services ────────────────────────────────────────────────────
step "Step 4/5 — Building images and starting Chord ring"

ssh -o StrictHostKeyChecking=no -i "$KEY_FILE" "ec2-user@$PUBLIC_IP" << 'REMOTE'
  set -e
  cd /home/ec2-user/chord-app

  echo "[remote] Building Docker image (first build ~3-5 min)..."
  docker build -f Dockerfile.demo -t chord-dht:demo . 2>&1 | tail -20

  echo "[remote] Starting all services..."
  docker compose \
    -f deploy/ec2-demo/docker-compose.demo.yml \
    up -d --remove-orphans

  echo "[remote] Waiting for nodes to become healthy (up to 60s)..."
  sleep 30

  echo "[remote] Container status:"
  docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
REMOTE

success "Services started"

# ── Step 5: Verify ───────────────────────────────────────────────────────────
step "Step 5/5 — Verifying deployment"

sleep 10

info "Checking Chord Node 1..."
if curl -sf "http://$PUBLIC_IP:5001/chord/ping" >/dev/null 2>&1; then
  success "Node 1 is healthy ✓"
else
  warn "Node 1 not responding yet — may still be starting"
fi

# ── Summary ──────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║         CHORD DHT DEMO DEPLOYED SUCCESSFULLY!           ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  ${CYAN}Chord Node 1:${NC}  http://$PUBLIC_IP:5001"
echo -e "  ${CYAN}Chord Node 2:${NC}  http://$PUBLIC_IP:5002"
echo -e "  ${CYAN}Chord Node 3:${NC}  http://$PUBLIC_IP:5003"
echo -e "  ${CYAN}Grafana:${NC}       http://$PUBLIC_IP:3000  (admin/admin)"
echo -e "  ${CYAN}Prometheus:${NC}    http://$PUBLIC_IP:9090"
echo ""
echo -e "  ${YELLOW}SSH:${NC} ssh -i deploy/ec2-demo/chord-demo-key.pem ec2-user@$PUBLIC_IP"
echo ""
echo -e "  ${YELLOW}Quick test:${NC}"
echo -e "    curl http://$PUBLIC_IP:5001/chord/ping"
echo -e "    curl http://$PUBLIC_IP:5001/chord/info"
echo ""
echo -e "  ${RED}IMPORTANT — Teardown when done:${NC}"
echo -e "    bash deploy/ec2-demo/scripts/teardown.sh"
echo ""
