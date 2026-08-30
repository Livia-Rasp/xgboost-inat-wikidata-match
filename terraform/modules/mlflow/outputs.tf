output "tracking_uri" {
  description = "What MLFLOW_TRACKING_URI is set to. The only thing a client needs — no credentials, no boto3."
  value       = "http://localhost:${var.host_port}"
}

output "container_name" {
  value = docker_container.mlflow.name
}

output "image_id" {
  value = docker_image.mlflow.image_id
}
