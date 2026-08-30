output "mlflow_url" {
  description = "Tracking server and UI."
  value       = module.mlflow.tracking_uri
}

output "minio_console_url" {
  description = "MinIO web console. Log in with the credentials from terraform.tfvars."
  value       = module.minio.console_url
}

output "postgres_port" {
  description = "Published Postgres port, for psql from the host."
  value       = module.postgres.host_port
}

output "tracking_env" {
  description = "Paste this to point the pipeline at the stack. It is the only thing a client needs — the server proxies artifacts, so no S3 credentials and no boto3."
  value       = "export MLFLOW_TRACKING_URI=${module.mlflow.tracking_uri}"
}

output "tracking_env_container" {
  description = "Same, for `docker compose run` — the container reaches the host's published port, not localhost."
  value       = "MLFLOW_TRACKING_URI=http://host.docker.internal:${var.mlflow_port}"
}
