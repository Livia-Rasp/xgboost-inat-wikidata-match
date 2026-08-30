output "backend_store_uri" {
  description = "SQLAlchemy URI for MLflow's --backend-store-uri, addressed on the docker network."
  # The network alias, not a computed attribute: the container is reachable at "postgres" from
  # anything on the same docker network, and depending on the resource keeps the ordering.
  value      = "postgresql://${var.username}:${var.password}@postgres:5432/${var.database}"
  depends_on = [docker_container.postgres]
  sensitive  = true
}

output "host" {
  description = "Network alias other containers reach Postgres on."
  value       = "postgres"
}

output "host_port" {
  description = "Published port, for psql from the host."
  value       = var.host_port
}

output "container_name" {
  value = docker_container.postgres.name
}
