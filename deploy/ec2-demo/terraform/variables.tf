# =============================================================================
# Variables — Chord DHT Demo Deployment
# =============================================================================

variable "aws_region" {
  description = "AWS region for the demo"
  type        = string
  default     = "us-west-2"
}

variable "project_name" {
  description = "Project name used for resource naming/tagging"
  type        = string
  default     = "chord-dht"
}

variable "instance_type" {
  description = <<-EOT
    EC2 instance type.
    - t2.micro  → Free Tier eligible (1 vCPU, 1 GB RAM) — tight but works
    - t3.small  → ~$0.0208/hr ($15/mo) — recommended for a smooth demo
    - t3.medium → ~$0.0416/hr ($30/mo) — comfortable with Grafana + 3 nodes
  EOT
  type        = string
  default     = "t3.small"
}

variable "allowed_ssh_cidr" {
  description = <<-EOT
    CIDR block allowed to SSH into the instance.
    Set to your current public IP: curl -s https://checkip.amazonaws.com/
    Example: "203.0.113.42/32"
    Use "0.0.0.0/0" only if you don't mind open SSH (NOT recommended).
  EOT
  type        = string
  default     = "0.0.0.0/0"
}
