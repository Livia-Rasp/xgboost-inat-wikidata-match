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

variable "airflow_admin_password" {
  description = "Password for Airflow's `admin` user. Seeded into the simple auth manager's password file, so it is never generated into a log."
  type        = string
  sensitive   = true
}

variable "airflow_jwt_secret" {
  description = "Signs the tokens tasks use to call the execution API. Any random string; every component must share it."
  type        = string
  sensitive   = true
}

variable "airflow_fernet_key" {
  description = "Encrypts connection secrets at rest. Must be a urlsafe base64-encoded 32-byte key — terraform.tfvars.example has the one-liner that prints one."
  type        = string
  sensitive   = true

  validation {
    # Airflow accepts a bad key at startup and fails only when something is encrypted, which is a
    # much later and stranger error than a plan-time complaint.
    #
    # The shape, not the decoded length: a Fernet key is 32 random bytes in *urlsafe* base64, so
    # it is always 43 characters from [A-Za-z0-9_-] plus one '='. Terraform cannot check the
    # bytes — base64decode returns a string and errors on anything that is not valid UTF-8, which
    # random bytes essentially never are, so `length(base64decode(...)) == 32` rejects every
    # valid key. Both wrong versions of this rule were written before the regex.
    condition     = can(regex("^[A-Za-z0-9_-]{43}=$", var.airflow_fernet_key))
    error_message = "airflow_fernet_key must be a urlsafe base64-encoded 32-byte key (43 chars then '='): python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"
  }
}

# -- what the Airflow containers can see -------------------------------------------------------

variable "airflow_uid" {
  description = "Host uid the Airflow containers run as, so files they write into data/ belong to you rather than to the image's airflow user. `id -u`."
  type        = number
  default     = 1000
}

variable "inat_cache_path" {
  description = <<-EOT
    Host directory holding the sibling repo's taxa.db, mounted read-only at /opt/inat-cache.
    Leave empty if you do not have that checkout: everything except the ingest DAG's first task
    works without it, and that task then fails naming the mount.
  EOT
  type        = string
  default     = ""
}

variable "findings_db_path" {
  description = <<-EOT
    The checker's findings.db, mounted read-only for the score_ambiguous DAG — usually
    <sibling repo>/data/findings.db. Leave empty if you do not have that checkout; only that one
    DAG needs it. Nothing here ever writes to it (platform-design §2.4).
  EOT
  type        = string
  default     = ""
}

variable "matcher_workers" {
  description = "MATCHER_WORKERS inside the containers — candidate generation's pool size. Neither cpu_count() nor process_cpu_count() can see a container CPU quota, so this is the only thing that works."
  type        = number
  default     = 4
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

variable "airflow_port" {
  type    = number
  default = 8080
}
