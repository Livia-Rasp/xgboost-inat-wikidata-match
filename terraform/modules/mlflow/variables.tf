variable "name_prefix" {
  type = string
}

variable "network_name" {
  type = string
}

variable "mlflow_version" {
  description = "Tag suffix for the built image. Keep in step with docker/Dockerfile.mlflow's FROM and with pyproject.toml's tracking extra."
  type        = string
  default     = "3.15.2"
}

variable "build_context" {
  description = "Absolute path to the repo root — docker/Dockerfile.mlflow is resolved against it."
  type        = string
}

variable "dockerfile" {
  description = "Dockerfile path, relative to build_context."
  type        = string
  default     = "docker/Dockerfile.mlflow"
}

variable "backend_store_uri" {
  description = "Postgres URI. Must be a database: the Model Registry is unsupported on the file store."
  type        = string
  sensitive   = true
}

variable "artifacts_destination" {
  description = "Where the server puts artifacts it proxies, e.g. s3://mlflow/."
  type        = string
}

variable "s3_endpoint_url" {
  type = string
}

variable "s3_access_key" {
  type      = string
  sensitive = true
}

variable "s3_secret_key" {
  type      = string
  sensitive = true
}

variable "host_port" {
  description = "Host port for the tracking server and its UI."
  type        = number
  default     = 5000
}
