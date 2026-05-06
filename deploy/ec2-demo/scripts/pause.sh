#!/bin/bash
# =============================================================================
# pause.sh — Stop the EC2 instance to save compute costs
#
# Stops billing for compute (~$0.023/hr) while keeping your EBS volume and
# Elastic IP intact. The EIP and EBS still cost ~$0.007/hr while stopped.
# Run resume.sh to bring everything back up (Docker auto-restarts containers).
#
# Usage:
#   cd Distributed_Chord_Infrastructure
#   bash deploy/ec2-demo/scripts/pause.sh
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

# ── Get instance ID from Terraform state ─────────────────────────────────────
step "Reading instance details from Terraform state"

cd "$TERRAFORM_DIR"
INSTANCE_ID=$(terraform output -raw instance_id 2>/dev/null) || \
  error "No Terraform state found. Nothing to pause."
PUBLIC_IP=$(terraform output -raw instance_public_ip 2>/dev/null) || true

info "Instance : $INSTANCE_ID"
info "IP       : ${PUBLIC_IP:-unknown}"

# ── Check current state ───────────────────────────────────────────────────────
CURRENT_STATE=$(aws ec2 describe-instances \
  --instance-ids "$INSTANCE_ID" \
  --query 'Reservations[0].Instances[0].State.Name' \
  --output text 2>/dev/null) || error "Could not query instance state. Check AWS credentials."

if [[ "$CURRENT_STATE" == "stopped" ]]; then
  warn "Instance is already stopped."
  echo ""
  echo -e "  Run ${CYAN}bash deploy/ec2-demo/scripts/resume.sh${NC} to bring it back up."
  exit 0
fi

if [[ "$CURRENT_STATE" != "running" ]]; then
  error "Instance is in state '$CURRENT_STATE' — can only pause a running instance."
fi

# ── Stop the instance ─────────────────────────────────────────────────────────
step "Stopping instance (this takes ~30 seconds)"

aws ec2 stop-instances --instance-ids "$INSTANCE_ID" --output text >/dev/null
info "Stop signal sent — waiting for instance to reach 'stopped' state..."

aws ec2 wait instance-stopped --instance-ids "$INSTANCE_ID"
success "Instance stopped ✓"

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}Instance paused.${NC}"
echo ""
echo -e "  ${CYAN}Compute billing stopped${NC}  (was ~\$0.023/hr)"
echo -e "  ${YELLOW}EBS + EIP still running${NC}  (~\$0.007/hr = ~\$5/month)"
echo ""
echo -e "  To resume:   ${CYAN}bash deploy/ec2-demo/scripts/resume.sh${NC}"
echo -e "  To destroy:  ${CYAN}bash deploy/ec2-demo/scripts/teardown.sh${NC}  ← stops all billing"
echo ""
