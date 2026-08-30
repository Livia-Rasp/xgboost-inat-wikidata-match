output "endpoint_url" {
  description = "S3 endpoint as reached from other containers on the docker network."
  value       = "http://minio:9000"
}

output "artifacts_destination" {
  description = "What MLflow's --artifacts-destination points at."
  value       = "s3://${var.bucket}/"
}

output "console_url" {
  description = "MinIO web console, from the host."
  value       = "http://localhost:${var.console_port}"
}

output "api_url" {
  description = "S3 API, from the host."
  value       = "http://localhost:${var.api_port}"
}

output "bucket_ready" {
  description = "Depend on this to order against bucket creation rather than merely against MinIO starting."
  value       = docker_container.create_bucket.id
}
