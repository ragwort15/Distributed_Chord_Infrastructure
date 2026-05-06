#!/bin/bash
# =============================================================================
# deploy.sh — One-command demo deployment for Chord DHT on AWS EC2
#
# Usage:
#   cd Distributed_Chord_Infrastructure
#   bash deploy/ec2-demo/scripts/deploy.sh
#
# Prerequisites (see DEPLOYMENT_GUIDE.md):
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

# ── SSH helper — consistent options everywhere ────────────────────────────────
ssh_run() {
  ssh -o StrictHostKeyChecking=no \
      -o ConnectTimeout=10 \
      -o ServerAliveInterval=15 \
      -o BatchMode=yes \
      -i "$KEY_FILE" \
      "ec2-user@$PUBLIC_IP" "$@"
}

# ── Prerequisites check ───────────────────────────────────────────────────────
step "Checking prerequisites"

command -v aws       >/dev/null 2>&1 || error "aws CLI not found. Install: https://aws.amazon.com/cli/"
command -v terraform >/dev/null 2>&1 || error "terraform not found. Install: https://developer.hashicorp.com/terraform/install"
command -v rsync     >/dev/null 2>&1 || error "rsync not found. Install: brew install rsync"
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

# ── Step 2: Wait for SSH ──────────────────────────────────────────────────────
step "Step 2/5 — Waiting for SSH to become available"

SSH_READY=false
for i in $(seq 1 30); do
  if ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 \
         -o BatchMode=yes \
         -i "$KEY_FILE" \
         "ec2-user@$PUBLIC_IP" "true" 2>/dev/null; then
    SSH_READY=true
    success "SSH is up (attempt $i/30)"
    break
  fi
  echo "  Attempt $i/30 — instance not ready yet, retrying in 10 s..."
  sleep 10
done

$SSH_READY || error "SSH never became available after 5 minutes. Check your security group and EC2 console."

# ── Step 3: Wait for bootstrap to finish ─────────────────────────────────────
step "Step 3/5 — Waiting for EC2 bootstrap to finish (installs Docker, ~3-7 min)"

info "Polling for /tmp/bootstrap-complete on the instance..."
BOOTSTRAP_DONE=false
for i in $(seq 1 42); do   # 42 x 10 s = 7 minutes max
  if ssh_run "test -f /tmp/bootstrap-complete" 2>/dev/null; then
    BOOTSTRAP_DONE=true
    success "Bootstrap finished (attempt $i/42)"
    break
  fi
  printf "  [%2d/42] Still bootstrapping — last log line: " "$i"
  ssh_run "tail -1 /var/log/chord-bootstrap.log 2>/dev/null || echo '(log not ready yet)'"
  sleep 10
done

if ! $BOOTSTRAP_DONE; then
  warn "Bootstrap did not finish within 7 minutes. Showing last 20 lines of log:"
  ssh_run "tail -20 /var/log/chord-bootstrap.log 2>/dev/null || echo '(log not found)'"
  error "Bootstrap timed out. The EC2 instance may need more time or the script failed."
fi

# Verify Docker daemon is actually responding before proceeding
info "Verifying Docker daemon is running..."
DOCKER_READY=false
for i in $(seq 1 6); do
  if ssh_run "sudo docker info >/dev/null 2>&1"; then
    DOCKER_READY=true
    success "Docker daemon is up"
    break
  fi
  echo "  Waiting for Docker daemon... ($i/6)"
  sleep 5
done

$DOCKER_READY || error "Docker daemon is not responding after bootstrap. Check /var/log/chord-bootstrap.log on the instance."

# ── Step 4: Sync code ────────────────────────────────────────────────────────
step "Step 4/5 — Uploading project files to EC2"

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

success "Code synced to /home/ec2-user/chord-app/"

# ── Step 5: Build + start services ────────────────────────────────────────────
step "Step 5/5 — Building images and starting Chord ring"

# Use 'sudo docker' to avoid needing an active docker group session in SSH.
# (usermod -aG docker requires a new login shell to take effect; sudo bypasses that.)
ssh_run bash << 'REMOTE'
set -e
cd /home/ec2-user/chord-app

echo "[remote] Building Docker image (first build ~3-5 min)..."
sudo docker build -f Dockerfile.demo -t chord-dht:demo . 2>&1 | tail -25

echo "[remote] Starting all services..."
sudo docker compose \
  -f deploy/ec2-demo/docker-compose.demo.yml \
  up -d --remove-orphans

echo "[remote] Waiting 30 s for nodes to initialise..."
sleep 30

echo "[remote] Container status:"
sudo docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
REMOTE

success "Services started"

# ── Verify ────────────────────────────────────────────────────────────────────
info "Waiting 10 s then checking node health..."
sleep 10

if curl -sf "http://$PUBLIC_IP:5001/chord/ping" >/dev/null 2>&1; then
  success "Node 1 is healthy ✓"
else
  warn "Node 1 not responding yet — it may still be starting (wait ~30 s and retry the curl)"
fi

# ── Summary ──────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║         CHORD DHT DEMO DEPLOYED SUCCESSFULLY!           ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  ${CYAN}Dashboard:${NC}     http://$PUBLIC_IP:5001"
echo -e "  ${CYAN}Chord Node 2:${NC}  http://$PUBLIC_IP:5002"
echo -e "  ${CYAN}Chord Node 3:${NC}  http://$PUBLIC_IP:5003"
echo -e "  ${CYAN}Grafana:${NC}       http://$PUBLIC_IP:3000  (admin/admin)"
echo -e "  ${CYAN}Prometheus:${NC}    http://$PUBLIC_IP:9090"
echo ""
echo -e "  ${YELLOW}SSH in:${NC}"
echo -e "    ssh -i deploy/ec2-demo/chord-demo-key.pem ec2-user@$PUBLIC_IP"
echo ""
echo -e "  ${YELLOW}Quick health check:${NC}"
echo -e "    curl http://$PUBLIC_IP:5001/chord/ping"
echo ""
echo -e "  ${RED}IMPORTANT — Teardown when done (stops billing):${NC}"
echo -e "    bash deploy/ec2-demo/scripts/teardown.sh"
echo ""
