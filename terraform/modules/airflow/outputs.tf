output "url" {
  description = "Airflow UI and REST API. Log in as `admin` with the password from terraform.tfvars."
  value       = "http://localhost:${var.host_port}"
}

output "container_names" {
  description = "The four long-running components, for docker logs."
  value = [
    docker_container.apiserver.name,
    docker_container.scheduler.name,
    docker_container.dag_processor.name,
    docker_container.triggerer.name,
  ]
}

output "image_id" {
  value = docker_image.airflow.image_id
}
