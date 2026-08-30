variable "name_prefix" {
  description = "Prefix for container and volume names, so one machine can run more than one environment."
  type        = string
}

variable "network_name" {
  description = "Docker network to attach to. Created by the environment, not by this module."
  type        = string
}

variable "image" {
  description = "Postgres image, pinned."
  type        = string
  default     = "postgres:18-alpine"
}

variable "username" {
  description = "Postgres superuser MLflow connects as."
  type        = string
  default     = "mlflow"
}

variable "password" {
  description = "Postgres password. Comes from a gitignored terraform.tfvars; never defaulted."
  type        = string
  sensitive   = true
}

variable "database" {
  description = "Database MLflow's backend store lives in."
  type        = string
  default     = "mlflow"
}

variable "host_port" {
  description = "Host port to publish Postgres on."
  type        = number
  default     = 5432
}
