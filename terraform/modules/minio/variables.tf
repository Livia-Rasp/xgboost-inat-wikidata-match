variable "name_prefix" {
  type = string
}

variable "network_name" {
  type = string
}

variable "image" {
  description = "MinIO server image. Pinned to a release tag, not `latest` — this is infrastructure."
  type        = string
  default     = "minio/minio:RELEASE.2025-09-07T16-13-09Z"
}

variable "mc_image" {
  description = "MinIO client, used once to create the bucket."
  type        = string
  default     = "minio/mc:RELEASE.2025-08-13T08-35-41Z"
}

variable "access_key" {
  description = "MINIO_ROOT_USER. From a gitignored terraform.tfvars."
  type        = string
  sensitive   = true
}

variable "secret_key" {
  description = "MINIO_ROOT_PASSWORD. From a gitignored terraform.tfvars."
  type        = string
  sensitive   = true
}

variable "bucket" {
  description = "Bucket MLflow writes artifacts into."
  type        = string
  default     = "mlflow"
}

variable "api_port" {
  description = "Host port for the S3 API."
  type        = number
  default     = 9000
}

variable "console_port" {
  description = "Host port for the MinIO web console."
  type        = number
  default     = 9001
}
