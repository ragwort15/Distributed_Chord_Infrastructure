# Chord DHT — Deployment Guide

Complete step-by-step instructions for running the Chord DHT system locally and on AWS EC2.

---

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Local Development — Docker Compose](#local-development--docker-compose)
3. [Local Development — Bare Metal](#local-development--bare-metal)
4. [AWS EC2 Demo Deployment](#aws-ec2-demo-deployment)
5. [Verifying the Stack](#verifying-the-stack)
6. [Environment Variables Reference](#environment-variables-reference)
7. [Teardown](#teardown)
8. [Troubleshooting](#troubleshooting)

---

## Prerequisites

### Local (Mac / Linux)

| Tool | Version | Install |
|------|---------|---------|
| Docker Desktop | ≥ 4.x | https://www.docker.com/products/docker-desktop |
| Docker Compose | ≥ 2.x (bundled with Docker Desktop) | — |
| Python | 3.11+ | `brew install python@3.11` |
| Git | any | `brew install git` |

### EC2 Deployment (additional)

| Tool | Version | Install |
|------|---------|---------|
| AWS CLI | ≥ 2.x | `brew install awscli` |
| Terraform | ≥ 1.6 | `brew install terraform` |
| An AWS account with EC2 + VPC permissions | — | — |

---

## Local Development — Docker Compose

This is the **recommended** way to run the full stack locally. It starts 3 Chord nodes, Prometheus, and Grafana all in isolated containers on the same Docker network.

### Step 1 — Clone and enter the repo

```bash
git clone https://github.com/ragwort15/Distributed_Chord_Infrastructure.git
cd Distributed_Chord_Infrastructure
```

### Step 2 — (Optional) Set Anthropic API key for AI routing

The system works without a key using heuristic routing. To enable LLM-powered placement:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

Or create a `.env` file in the repo root:

```
ANTHROPIC_API_KEY=sk-ant-...
```

### Step 3 — Build and start all services

```bash
docker compose up -d --build
```

This builds the `chord-dht` image and starts:

| Container | Port | Purpose |
|-----------|------|---------|
| `chord-node-1` | 5001 | Bootstrap node (ID=10) |
| `chord-node-2` | 5002 | Ring node (ID=80) |
| `chord-node-3` | 5003 | Ring node (ID=150) |
| `chord-prometheus` | 9090 | Metrics collection |
| `chord-grafana` | 3000 | Dashboards |

### Step 4 — Wait for nodes to be healthy

```bash
docker compose ps
```

All three chord nodes should show `healthy` within ~30 seconds. If any show `starting`, wait a bit and re-run.

### Step 5 — Open the Dashboard

Open your browser to: **http://localhost:5001**

The dashboard shows:
- **Ring Topology** — live SVG of the Chord ring, finger tables, submit job
- **Observability** — embedded Grafana dashboard
- **Jobs** — all submitted jobs across the ring
- **Task Registry** — register and look up tasks
- **DHT Store** — browse key-value pairs stored in the ring
- **Agent Log** — AI placement decisions
- **Fault Lab** — inject node failures and observe recovery

### Step 6 — Open Grafana directly (optional)

Grafana is at **http://localhost:3000** (admin / admin). The Chord DHT dashboard auto-provisions and shows:
- Request throughput, hop counts, node queue depths, agent strategy mix

---

## Local Development — Bare Metal

Run nodes directly with Python (no Docker). Useful for debugging.

### Step 1 — Create and activate a virtual environment

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Step 2 — Start node 1 (bootstrap)

```bash
python run_node.py --host 127.0.0.1 --port 5001 --id 10 \
  --worker --workers 4 --log INFO
```

### Step 3 — Start nodes 2 and 3 (in separate terminals)

```bash
# Terminal 2
python run_node.py --host 127.0.0.1 --port 5002 --id 80 \
  --join 127.0.0.1:5001 --worker --workers 4

# Terminal 3
python run_node.py --host 127.0.0.1 --port 5003 --id 150 \
  --join 127.0.0.1:5001 --worker --workers 4
```

### Step 4 — (Optional) Start Prometheus + Grafana for bare-metal metrics

Edit `observability/prometheus.yml` — comment out the `chord-nodes` job and uncomment the `chord-nodes-local` job (which uses `host.docker.internal`). Then:

```bash
docker compose up -d prometheus grafana
```

### Step 5 — Open the dashboard

**http://localhost:5001**

---

## AWS EC2 Demo Deployment

Deploys the full stack on a single `t3.small` EC2 instance using Terraform + Docker Compose.

### Step 1 — Configure AWS credentials

```bash
aws configure
# Enter: AWS Access Key ID, Secret, region (e.g. us-west-2), output format (json)
```

Verify:
```bash
aws sts get-caller-identity
```

### Step 2 — Create an EC2 key pair (if you don't have one)

```bash
aws ec2 create-key-pair --key-name chord-demo-key \
  --query 'KeyMaterial' --output text > ~/.ssh/chord-demo-key.pem
chmod 400 ~/.ssh/chord-demo-key.pem
```

### Step 3 — Deploy with the deploy script

```bash
cd deploy/ec2-demo
chmod +x scripts/deploy.sh scripts/teardown.sh
./scripts/deploy.sh
```

The script will:
1. Run `terraform init` and `terraform apply` to provision the EC2 instance
2. Output the public IP address
3. Wait for EC2 to boot and install Docker
4. SSH in and start the stack with `docker-compose.demo.yml`

The full deployment takes ~3–5 minutes.

### Step 4 — Access the running stack

After the script completes, it prints the EC2 public IP. Open:

| Service | URL |
|---------|-----|
| Dashboard | `http://<EC2_IP>:5001` |
| Grafana | `http://<EC2_IP>:3000` (admin / admin) |
| Prometheus | `http://<EC2_IP>:9090` |

### Step 5 — Manual deploy (if script fails)

```bash
cd deploy/ec2-demo

# 1. Provision EC2
terraform init
terraform apply -auto-approve

# 2. Get the public IP
EC2_IP=$(terraform output -raw public_ip)
echo "EC2 IP: $EC2_IP"

# 3. Copy code to the instance
scp -i ~/.ssh/chord-demo-key.pem -r ../../ ec2-user@${EC2_IP}:~/chord/

# 4. SSH in and start
ssh -i ~/.ssh/chord-demo-key.pem ec2-user@${EC2_IP}
cd ~/chord
docker compose -f deploy/ec2-demo/docker-compose.demo.yml up -d --build
```

---

## Verifying the Stack

### Quick health check for all nodes

```bash
# Local
curl -s http://localhost:5001/chord/ping | python3 -m json.tool
curl -s http://localhost:5002/chord/ping | python3 -m json.tool
curl -s http://localhost:5003/chord/ping | python3 -m json.tool
```

Expected response:
```json
{"node_id": 10, "status": "alive", "successor": 80, ...}
```

### Submit a test job via API

```bash
curl -s -X POST http://localhost:5001/jobs \
  -H "Content-Type: application/json" \
  -d '{"type": "echo", "payload": {"message": "hello chord"}}' \
  | python3 -m json.tool
```

Expected:
```json
{
  "ok": true,
  "job_id": "...",
  "stored_at_node": 80,
  "placement_reasoning": "..."
}
```

### Check Prometheus targets

Open **http://localhost:9090/targets** — all three `chord-node-*` targets should show state `UP`.

### Check Docker container logs

```bash
# View all logs
docker compose logs -f

# View a specific node
docker compose logs -f chord-node-1
```

---

## Environment Variables Reference

These are set in `docker-compose.yml` and passed to each container via `entrypoint.sh`:

| Variable | Default | Description |
|----------|---------|-------------|
| `CHORD_PORT` | `5000` | Port the node listens on inside the container |
| `CHORD_ID` | _(auto)_ | Integer node ID (SHA-1 hash of host:port if unset) |
| `CHORD_JOIN` | _(empty)_ | `host:port` of existing ring node to join (empty = bootstrap) |
| `CHORD_WORKERS` | `4` | Worker thread pool size for job execution |
| `ANTHROPIC_API_KEY` | _(empty)_ | Enables LLM-powered AI routing (optional) |
| `LOG_LEVEL` | `INFO` | Logging verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `JOB_TTL_S` | `3600` | Seconds before completed/failed jobs are evicted from memory |
| `AGENT_LOG_MAX_MB` | `10` | Max size of `agent_decisions.jsonl` before rotation |

---

## Teardown

### Local Docker Compose

```bash
# Stop and remove containers, network
docker compose down

# Also remove volumes (Prometheus data, Grafana storage)
docker compose down -v

# Also remove the built image
docker compose down -v --rmi local
```

### EC2 / Terraform

```bash
cd deploy/ec2-demo
./scripts/teardown.sh

# Or manually
terraform destroy -auto-approve
```

---

## Troubleshooting

### Nodes stuck in `starting` / health check failing

```bash
docker compose logs chord-node-1
```

Common causes:
- Port conflict: another process on 5001/5002/5003 — run `lsof -i :5001`
- Build failed: run `docker compose build --no-cache`

### Grafana shows "No data" or blank dashboard

1. Check Prometheus targets at http://localhost:9090/targets — must show `UP`
2. Check `observability/prometheus.yml` uses container names (`chord-node-1:5000`), not `host.docker.internal`
3. Verify Grafana datasource: Settings → Data Sources → Prometheus → URL should be `http://chord-prometheus:9090`

### `--port` missing error on startup

This means the `entrypoint.sh` is missing or not copied into the image. Run:

```bash
docker compose build --no-cache
docker compose up -d
```

### Docker build is very slow

Make sure `.dockerignore` is present in the repo root. Without it, the 240 MB `.venv/` and `venv/` directories are sent to the build daemon on every build.

### EC2 — cannot SSH in

```bash
# Check the security group allows port 22 from your IP
aws ec2 describe-security-groups --group-names chord-demo-sg

# Find the correct key
ls ~/.ssh/*.pem
```

### Reset everything and start fresh

```bash
docker compose down -v --rmi local
docker system prune -f
docker compose up -d --build
```
