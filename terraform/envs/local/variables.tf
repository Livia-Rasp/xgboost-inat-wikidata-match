variable "name_prefix" {
  description = "Prefix for every container, volume and network name."
  type        = string
  default     = "inat-match"
}

# -- credentials -------------------------------------------------------------------------------
#
# No defaults, on purpose: `terraform apply` should fail asking for these rather than silently
# standing up a stack on a password that is in a public repo. They live in terraform.tfvars,
# which is gitignored; terraform.tfvars.example is the committed template.

variable "postgres_password" {
  description = "Password for MLflow's Postgres backend store."
  type        = string
  sensitive   = true
}

variable "minio_access_key" {
  description = "MinIO root user, which is also the S3 access key the tracking server uses."
  type        = string
  sensitive   = true
}

variable "minio_secret_key" {
  description = "MinIO root password, which is also the S3 secret key. MinIO requires >= 8 characters."
  type        = string
  sensitive   = true

  validation {
    condition     = length(var.minio_secret_key) >= 8
    error_message = "MinIO refuses to start with a root password shorter than 8 characters; it fails at runtime, not at apply, so it is checked here."
  }
}

# -- published ports ---------------------------------------------------------------------------

variable "mlflow_port" {
  type    = number
  default = 5000
}

variable "postgres_port" {
  type    = number
  default = 5432
}

variable "minio_api_port" {
  type    = number
  default = 9000
}

variable "minio_console_port" {
  type    = number
  default = 9001
}
