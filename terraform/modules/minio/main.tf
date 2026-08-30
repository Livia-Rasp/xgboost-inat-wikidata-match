# MLflow's artifact store, S3-compatible.
#
# Nothing in this repo ever talks to it directly. The tracking server is run with
# --serve-artifacts, so clients address artifacts as mlflow-artifacts:/ over HTTP and the server
# is the only thing holding these credentials — which is what keeps boto3 and a secret out of the
# pipeline image entirely. Verified: a client with no boto3 installed round-trips a model.

terraform {
  required_providers {
    docker = {
      source  = "kreuzwerker/docker"
      version = "~> 4.5"
    }
  }
}

resource "docker_image" "minio" {
  name         = var.image
  keep_locally = true
}

resource "docker_image" "mc" {
  name         = var.mc_image
  keep_locally = true
}

resource "docker_volume" "data" {
  name = "${var.name_prefix}-minio-data"
}

resource "docker_container" "minio" {
  name    = "${var.name_prefix}-minio"
  image   = docker_image.minio.image_id
  restart = "unless-stopped"
  command = ["server", "/data", "--console-address", ":9001"]

  env = [
    "MINIO_ROOT_USER=${var.access_key}",
    "MINIO_ROOT_PASSWORD=${var.secret_key}",
  ]

  networks_advanced {
    name    = var.network_name
    aliases = ["minio"]
  }

  volumes {
    volume_name    = docker_volume.data.name
    container_path = "/data"
  }

  ports {
    internal = 9000
    external = var.api_port
  }

  ports {
    internal = 9001
    external = var.console_port
  }

  wait = true
  healthcheck {
    test     = ["CMD", "mc", "ready", "local"]
    interval = "5s"
    timeout  = "5s"
    retries  = 12
  }
}

# MLflow does not create its own bucket, and the docker provider has no bucket resource, so this
# is a one-shot job container: `must_run = false` because it is expected to exit, `attach = true`
# so apply actually waits for it and a failure surfaces as a failed apply rather than a bucket
# that quietly never appeared.
resource "docker_container" "create_bucket" {
  name       = "${var.name_prefix}-minio-mkbucket"
  image      = docker_image.mc.image_id
  must_run   = false
  attach     = true
  depends_on = [docker_container.minio]

  networks_advanced {
    name = var.network_name
  }

  entrypoint = ["/bin/sh", "-c"]
  command = [
    join(" && ", [
      "mc alias set local http://minio:9000 ${var.access_key} ${var.secret_key}",
      "mc mb --ignore-existing local/${var.bucket}",
    ])
  ]
}
