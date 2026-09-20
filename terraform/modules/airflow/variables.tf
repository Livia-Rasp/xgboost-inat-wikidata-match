variable "name_prefix" {
  type = string
}

variable "network_name" {
  type = string
}

variable "build_context" {
  description = "Absolute path to the repo root — docker/Dockerfile.airflow is resolved against it."
  type        = string
}

variable "dockerfile" {
  type    = string
  default = "docker/Dockerfile.airflow"
}

variable "airflow_version" {
  description = "Tag suffix for the built image. Keep in step with Dockerfile.airflow's FROM and pyproject's airflow extra."
  type        = string
  default     = "3.3.1"
}

# -- what the containers see of the host -------------------------------------------------------

variable "repo_path" {
  description = <<-EOT
    Absolute host path to this checkout, bind-mounted read-only at /opt/project. The containers
    carry dependencies, not code (platform-design §5.4 amendment 2), so an edit is measurable on
    the next DAG run with no image rebuild.
  EOT
  type        = string
}

variable "data_path" {
  description = "Host path mounted read-write at /opt/project/data. Defaults to <repo_path>/data."
  type        = string
  default     = ""
}

variable "inat_cache_path" {
  description = <<-EOT
    Host directory holding the sibling repo's taxa.db, mounted read-only at /opt/inat-cache.
    Empty means "not available": the ingest DAG's first task then fails with the mount name, which
    is the same failure the pipeline image gives, rather than a missing-file traceback.
  EOT
  type        = string
  default     = ""
}

variable "findings_db_path" {
  description = <<-EOT
    The sibling checker's findings.db, mounted read-only for the score_ambiguous DAG. Empty means
    "not available", and that DAG's first task then fails naming the file — everything else is
    unaffected. This repo only ever reads it (platform-design §2.4).
  EOT
  type        = string
  default     = ""
}

variable "uid" {
  description = <<-EOT
    Host uid the containers run as, so files they write into data/ are owned by you rather than by
    the image's airflow user (50000). Group 0 because the base image's directories are root-group
    writable.
  EOT
  type        = number
}

variable "workers" {
  description = "MATCHER_WORKERS — candidate generation's process-pool size. Neither cpu_count() nor process_cpu_count() can see a container CPU quota, so this is the only thing that works here."
  type        = number
  default     = 4
}

# -- credentials -------------------------------------------------------------------------------

variable "admin_password" {
  description = "Password for the `admin` user of the simple auth manager. Seeded into its passwords file by the init job."
  type        = string
  sensitive   = true
}

variable "jwt_secret" {
  description = "AIRFLOW__API_AUTH__JWT_SECRET. Signs the tokens workers use to call the execution API; every component must agree on it."
  type        = string
  sensitive   = true
}

variable "fernet_key" {
  description = "AIRFLOW__CORE__FERNET_KEY, a urlsafe base64 32-byte key. Encrypts connection secrets at rest; this stack stores none, but a shared key keeps the components from warning and makes adding one later safe."
  type        = string
  sensitive   = true
}

variable "metadata_db_uri" {
  description = "SQLAlchemy URI for Airflow's own metadata database, on the docker network."
  type        = string
  sensitive   = true
}

variable "postgres_image" {
  description = "Used by the one-shot job that creates Airflow's database next to MLflow's; keep equal to the postgres module's."
  type        = string
  default     = "postgres:18-alpine"
}

variable "postgres_admin_uri" {
  description = "URI of an existing database on the same server, which the create-database job connects to in order to issue CREATE DATABASE."
  type        = string
  sensitive   = true
}

variable "metadata_db_name" {
  type    = string
  default = "airflow"
}

variable "mlflow_tracking_uri" {
  description = "Reached over the docker network, so tracking works from a task without publishing a port."
  type        = string
  default     = "http://mlflow:5000"
}

variable "host_port" {
  description = "Host port for the Airflow UI and REST API."
  type        = number
  default     = 8080
}

variable "duckdb_pool" {
  description = "Name of the pool the dbt tasks run in. Must equal dags/_common.py's DUCKDB_POOL."
  type        = string
  default     = "duckdb"
}

variable "duckdb_pool_slots" {
  description = "Slots in the `duckdb` pool. One, because DuckDB permits a single writing process per database file and Cosmos runs one dbt process per model."
  type        = number
  default     = 1
}
