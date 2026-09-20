# The MLflow tracking server, and the model registry that replaces the prose freeze in CLAUDE.md.

terraform {
  required_providers {
    docker = {
      source  = "kreuzwerker/docker"
      version = "~> 4.5"
    }
  }
}

# Built, not pulled. The official ghcr.io/mlflow/mlflow image is a bare `pip install mlflow` with
# neither psycopg2 nor boto3, so it cannot reach the Postgres backend store or the MinIO artifact
# store as shipped. docker/Dockerfile.mlflow adds exactly those two.
#
# `triggers` keyed on the Dockerfile's own hash, so a second `plan` after an unchanged apply is
# clean: without it the provider re-evaluates the build context every run and reports a diff
# forever, which is the classic way an IaC demo stops being trustworthy.
resource "docker_image" "mlflow" {
  name         = "${var.name_prefix}-mlflow:${var.mlflow_version}"
  keep_locally = true
  force_remove = false

  build {
    context    = var.build_context
    dockerfile = var.dockerfile
    tag        = ["${var.name_prefix}-mlflow:${var.mlflow_version}"]
  }

  triggers = {
    dockerfile_sha256 = filesha256("${var.build_context}/${var.dockerfile}")
  }
}

resource "docker_container" "mlflow" {
  name    = "${var.name_prefix}-mlflow"
  image   = docker_image.mlflow.image_id
  restart = "unless-stopped"

  # --serve-artifacts is the load-bearing flag: the server proxies artifact upload and download,
  # so clients resolve mlflow-artifacts:/ over HTTP and never need S3 credentials or boto3. The
  # alternative (--default-artifact-root) hands clients a raw s3:// URI and pushes both into the
  # pipeline image.
  command = [
    "mlflow", "server",
    "--host", "0.0.0.0",
    "--port", "5000",
    "--backend-store-uri", var.backend_store_uri,
    "--artifacts-destination", var.artifacts_destination,
    "--serve-artifacts",
    # Without this, anything calling the server by its docker-network name gets a 403 from
    # MLflow's DNS-rebinding protection — which is every Airflow task (milestone 16).
    "--allowed-hosts", var.allowed_hosts,
  ]

  env = [
    "MLFLOW_S3_ENDPOINT_URL=${var.s3_endpoint_url}",
    "AWS_ACCESS_KEY_ID=${var.s3_access_key}",
    "AWS_SECRET_ACCESS_KEY=${var.s3_secret_key}",
  ]

  networks_advanced {
    name    = var.network_name
    aliases = ["mlflow"]
  }

  ports {
    internal = 5000
    external = var.host_port
  }

  wait = true
  healthcheck {
    # The endpoint the plan's acceptance check curls. Python rather than curl: the base image is
    # python:slim-derived and carries no curl or wget.
    test         = ["CMD", "python3", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:5000/health')"]
    interval     = "5s"
    timeout      = "5s"
    retries      = 24
    start_period = "10s"
  }
}
