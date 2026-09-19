# The platform stack for spec §7 milestones 15-16: an MLflow tracking server with a Postgres
# backend store and a MinIO artifact store.
#
# Deliberately not in compose.yaml. That file runs the *pipeline* and states the invariant at its
# first line: no service appears in both. Milestone 16 added modules/airflow here and changed
# nothing about the other three.
#
# Milestone 15 needs a database-backed store because the MLflow Model Registry is not supported
# on the file store, and the registry is the whole point — it replaces a paragraph in CLAUDE.md
# as the thing that keeps the milestone 6/7 models frozen.

locals {
  # terraform/envs/local -> repo root. The mlflow image is built from docker/Dockerfile.mlflow.
  repo_root = abspath("${path.module}/../../..")
}

resource "docker_network" "platform" {
  name = "${var.name_prefix}-net"
}

module "postgres" {
  source = "../../modules/postgres"

  name_prefix  = var.name_prefix
  network_name = docker_network.platform.name
  password     = var.postgres_password
  host_port    = var.postgres_port
}

module "minio" {
  source = "../../modules/minio"

  name_prefix  = var.name_prefix
  network_name = docker_network.platform.name
  access_key   = var.minio_access_key
  secret_key   = var.minio_secret_key
  api_port     = var.minio_api_port
  console_port = var.minio_console_port
}

module "mlflow" {
  source = "../../modules/mlflow"

  name_prefix   = var.name_prefix
  network_name  = docker_network.platform.name
  build_context = local.repo_root
  host_port     = var.mlflow_port

  backend_store_uri     = module.postgres.backend_store_uri
  artifacts_destination = module.minio.artifacts_destination
  s3_endpoint_url       = module.minio.endpoint_url
  s3_access_key         = var.minio_access_key
  s3_secret_key         = var.minio_secret_key

  # Ordering that matters and is easy to get wrong: MLflow runs Alembic migrations against
  # Postgres on startup, and writes to the bucket on the first logged artifact. depends_on a
  # whole module covers every resource in it, so this waits for the mc bucket job too, not just
  # for the MinIO container — the difference between "MinIO is up" and "the bucket MLflow was
  # told to write to exists". Both modules also gate on `wait = true` healthchecks, so this is
  # ordering against readiness rather than against container creation.
  depends_on = [module.postgres, module.minio]
}

module "airflow" {
  source = "../../modules/airflow"

  name_prefix   = var.name_prefix
  network_name  = docker_network.platform.name
  build_context = local.repo_root
  host_port     = var.airflow_port

  # The checkout itself, mounted read-only, with data/ read-write inside it. The containers carry
  # dependencies, not code — see docs/platform-design.md §5.4 amendment 2.
  repo_path       = local.repo_root
  inat_cache_path = var.inat_cache_path
  uid             = var.airflow_uid
  workers         = var.matcher_workers

  admin_password = var.airflow_admin_password
  jwt_secret     = var.airflow_jwt_secret
  fernet_key     = var.airflow_fernet_key

  # Airflow's metadata database lives on the same Postgres server as MLflow's backend store, in
  # its own database. The URI is assembled from the module's outputs rather than repeated, so the
  # username cannot drift between the two consumers.
  metadata_db_uri = join("", [
    "postgresql+psycopg2://${module.postgres.username}:${var.postgres_password}",
    "@${module.postgres.host}:5432/airflow",
  ])
  postgres_admin_uri = module.postgres.backend_store_uri

  # Over the docker network: a task logs to the tracking server without depending on a published
  # port, and the gate resolves the champion from the registry the same way a host run does.
  mlflow_tracking_uri = "http://mlflow:5000"

  # The gate needs a registry to compare against, so the tracking server has to exist first. As
  # with the mlflow module, depending on the whole module waits for its healthcheck, not merely
  # for a created container.
  depends_on = [module.postgres, module.mlflow]
}
