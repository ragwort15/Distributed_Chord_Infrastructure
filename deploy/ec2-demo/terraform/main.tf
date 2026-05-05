# =============================================================================
# Chord DHT - Demo Deployment on AWS EC2 (Budget-Friendly)
# Cost: ~$0/month on Free Tier (t2.micro) or ~$8-12/month (t3.small)
# =============================================================================

terraform {
  required_version = ">= 1.3.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
    local = {
      source  = "hashicorp/local"
      version = "~> 2.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

# =============================================================================
# Data Sources
# =============================================================================

# Use the default VPC to avoid creating a new one (saves time & money)
data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

# Latest Amazon Linux 2023 AMI (free, well-maintained)
data "aws_ami" "amazon_linux_2023" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-2023.*-x86_64"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

# =============================================================================
# SSH Key Pair (generated locally, private key saved to file)
# =============================================================================

resource "tls_private_key" "chord_demo" {
  algorithm = "RSA"
  rsa_bits  = 4096
}

resource "aws_key_pair" "chord_demo" {
  key_name   = "${var.project_name}-demo-key"
  public_key = tls_private_key.chord_demo.public_key_openssh

  tags = local.common_tags
}

# Save private key locally so deploy.sh can use it
resource "local_file" "private_key" {
  content         = tls_private_key.chord_demo.private_key_pem
  filename        = "${path.module}/../chord-demo-key.pem"
  file_permission = "0600"
}

# =============================================================================
# Security Group — open only what the demo needs
# =============================================================================

resource "aws_security_group" "chord_demo" {
  name        = "${var.project_name}-demo-sg"
  description = "Chord DHT demo - allow SSH, app ports, Prometheus, Grafana"
  vpc_id      = data.aws_vpc.default.id

  # SSH access from your IP only (filled in by variable)
  ingress {
    description = "SSH"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.allowed_ssh_cidr]
  }

  # Chord node HTTP ports
  ingress {
    description = "Chord Node 1"
    from_port   = 5001
    to_port     = 5001
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "Chord Node 2"
    from_port   = 5002
    to_port     = 5002
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "Chord Node 3"
    from_port   = 5003
    to_port     = 5003
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # Grafana dashboard
  ingress {
    description = "Grafana"
    from_port   = 3000
    to_port     = 3000
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # Prometheus metrics
  ingress {
    description = "Prometheus"
    from_port   = 9090
    to_port     = 9090
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # All outbound allowed (for Docker pulls, apt installs, etc.)
  egress {
    description = "All outbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.common_tags, { Name = "${var.project_name}-demo-sg" })
}

# =============================================================================
# EC2 Instance
# =============================================================================

resource "aws_instance" "chord_demo" {
  ami                    = data.aws_ami.amazon_linux_2023.id
  instance_type          = var.instance_type
  key_name               = aws_key_pair.chord_demo.key_name
  vpc_security_group_ids = [aws_security_group.chord_demo.id]
  subnet_id              = tolist(data.aws_subnets.default.ids)[0]

  # Enough disk for Docker images + logs
  root_block_device {
    volume_type           = "gp3"
    volume_size           = 20
    delete_on_termination = true
  }

  # Bootstrap: install Docker + Docker Compose on first boot
  user_data = base64encode(templatefile("${path.module}/../scripts/ec2-bootstrap.sh", {}))

  # Prevent accidental termination during the demo
  disable_api_termination = false

  tags = merge(local.common_tags, { Name = "${var.project_name}-demo" })
}

# =============================================================================
# Elastic IP — stable public IP for the demo
# =============================================================================

resource "aws_eip" "chord_demo" {
  instance = aws_instance.chord_demo.id
  domain   = "vpc"

  tags = merge(local.common_tags, { Name = "${var.project_name}-demo-eip" })

  depends_on = [aws_instance.chord_demo]
}

# =============================================================================
# Locals
# =============================================================================

locals {
  common_tags = {
    Project     = var.project_name
    Environment = "demo"
    ManagedBy   = "Terraform"
    Class       = "CMPE-273"
  }
}
