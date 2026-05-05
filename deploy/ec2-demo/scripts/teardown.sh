#!/bin/bash
# =============================================================================
# teardown.sh — Destroy all AWS resources created for the Chord DHT demo
#
# Usage (from repo root):
#   bash deploy/ec2-demo/scripts/teardown.sh
# =============================================================================

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TERRAFORM_DIR="$SCRIPT_DIR/../terraform"
KEY_FILE="$SCRIPT_DIR/../chord-demo-key.pem"

echo -e "${RED}"
echo "╔══════════════════════════════════════════════════════════╗"
echo "║              CHORD DHT DEMO TEARDOWN                    ║"
echo "║  This will DESTROY all AWS resources and stop billing.  ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo -e "${NC}"

read -p "Are you sure you want to destroy everything? (yes/no): " CONFIRM
if [[ "$CONFIRM" != "yes" ]]; then
  echo "Teardown cancelled."
  exit 0
fi

echo -e "\n${CYAN}Stopping Docker services on EC2 (if reachable)...${NC}"
if [[ -f "$KEY_FILE" ]]; then
  PUBLIC_IP=$(cd "$TERRAFORM_DIR" && terraform output -raw instance_public_ip 2>/dev/null || echo "")
  if [[ -n "$PUBLIC_IP" ]]; then
    ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 -i "$KEY_FILE" \
        "ec2-user@$PUBLIC_IP" \
        "cd /home/ec2-user/chord-app && docker compose -f deploy/ec2-demo/docker-compose.demo.yml down 2>/dev/null || true" \
        2>/dev/null || echo "(EC2 not reachable — skipping graceful shutdown)"
  fi
fi

echo -e "\n${CYAN}Running terraform destroy...${NC}"
cd "$TERRAFORM_DIR"
terraform destroy -auto-approve -input=false

# Clean up local key file
if [[ -f "$KEY_FILE" ]]; then
  rm -f "$KEY_FILE"
  echo -e "${GREEN}Removed local key file: $KEY_FILE${NC}"
fi

echo ""
echo -e "${GREEN}✓ All AWS resources destroyed. No further charges will accrue.${NC}"
echo ""
