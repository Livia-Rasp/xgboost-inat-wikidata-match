# MLflow's backend store.
#
# It has to be a database: the Model Registry is not supported on the file store, which is the
# whole reason this container exists rather than pointing --backend-store-uri at a directory.

terraform {
  required_providers {
    docker = {
      source  = "kreuzwerker/docker"
      version = "~> 4.5"
    }
  }
}

resource "docker_image" "postgres" {
  name         = var.image
  keep_locally = true
}

# Named, not a bind mount: the point of `terraform destroy` here is that it takes the data with
# it unless you deliberately keep the volume.
resource "docker_volume" "pgdata" {
  name = "${var.name_prefix}-pgdata"
}

resource "docker_container" "postgres" {
  name    = "${var.name_prefix}-postgres"
  image   = docker_image.postgres.image_id
  restart = "unless-stopped"

  env = [
    "POSTGRES_USER=${var.username}",
    "POSTGRES_PASSWORD=${var.password}",
    "POSTGRES_DB=${var.database}",
  ]

  networks_advanced {
    name    = var.network_name
    aliases = ["postgres"]
  }

  # /var/lib/postgresql, NOT /var/lib/postgresql/data. Postgres 18 changed this: the image now
  # stores data in a major-version-specific subdirectory so that `pg_upgrade --link` does not
  # have to cross a mount boundary, and it refuses to start — restart-looping, so the symptom is
  # a healthcheck timeout rather than an error — if it finds data at the old path.
  # See docker-library/postgres#1259.
  volumes {
    volume_name    = docker_volume.pgdata.name
    container_path = "/var/lib/postgresql"
  }

  # Published because this is a laptop and being able to psql into it is most of the point of
  # running it locally at all.
  ports {
    internal = 5432
    external = var.host_port
  }

  # `wait` blocks apply until this passes, which is what gives the mlflow module a real
  # dependency rather than a hopeful one: Postgres accepting TCP is not the same as Postgres
  # being ready to serve, and MLflow runs its Alembic migrations on startup.
  wait = true
  healthcheck {
    test     = ["CMD-SHELL", "pg_isready -U ${var.username} -d ${var.database}"]
    interval = "5s"
    timeout  = "5s"
    retries  = 12
  }
}
