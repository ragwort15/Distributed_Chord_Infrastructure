# =============================================================================
# Outputs — printed after `terraform apply`
# =============================================================================

output "instance_public_ip" {
  description = "Public IP of the EC2 instance (Elastic IP)"
  value       = aws_eip.chord_demo.public_ip
}

output "ssh_command" {
  description = "SSH command to log into the instance"
  value       = "ssh -i deploy/ec2-demo/chord-demo-key.pem ec2-user@${aws_eip.chord_demo.public_ip}"
}

output "chord_node_1_url" {
  description = "Chord Node 1 endpoint"
  value       = "http://${aws_eip.chord_demo.public_ip}:5001"
}

output "chord_node_2_url" {
  description = "Chord Node 2 endpoint"
  value       = "http://${aws_eip.chord_demo.public_ip}:5002"
}

output "chord_node_3_url" {
  description = "Chord Node 3 endpoint"
  value       = "http://${aws_eip.chord_demo.public_ip}:5003"
}

output "grafana_url" {
  description = "Grafana dashboard URL (admin / admin)"
  value       = "http://${aws_eip.chord_demo.public_ip}:3000"
}

output "prometheus_url" {
  description = "Prometheus metrics URL"
  value       = "http://${aws_eip.chord_demo.public_ip}:9090"
}

output "instance_id" {
  description = "EC2 instance ID (for AWS Console)"
  value       = aws_instance.chord_demo.id
}

output "demo_checklist" {
  description = "Quick demo verification checklist"
  value = <<-EOT

  ============================================================
    CHORD DHT DEMO IS DEPLOYING
  ============================================================
    Wait ~3 minutes for EC2 bootstrap to finish, then:

    1. Ring health:
       curl http://${aws_eip.chord_demo.public_ip}:5001/chord/ping

    2. Submit a job:
       curl -X POST http://${aws_eip.chord_demo.public_ip}:5001/jobs \
         -H "Content-Type: application/json" \
         -d '{"type":"echo","payload":{"message":"hello chord"}}'

    3. View Grafana:
       http://${aws_eip.chord_demo.public_ip}:3000  (admin/admin)

    4. SSH into instance:
       ssh -i deploy/ec2-demo/chord-demo-key.pem ec2-user@${aws_eip.chord_demo.public_ip}

  ============================================================
  EOT
}
