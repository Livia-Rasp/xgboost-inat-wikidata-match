# Airflow 3.3 on LocalExecutor: four long-running containers plus two one-shot jobs.
#
# LocalExecutor, not Celery (platform-design §5.4): the executor is a property of the scheduler
# rather than a separate service, so there is no Redis and no worker container — four containers
# instead of six, which is the right size for a laptop. The trade is that task parallelism is
# bounded by the scheduler's own process; the dbt tasks are serialised by a pool anyway, and
# training is one long task.
#
# The repository is bind-mounted rather than baked in (§5.4 amendment 2), so an edit to src/ or
# dags/ is measurable on the next DAG run with no image rebuild, and MLflow still records which
# commit — and whether the tree was dirty — through tracking.git_sha().

terraform {
  required_providers {
    docker = {
      source  = "kreuzwerker/docker"
      version = "~> 4.5"
    }
  }
}

locals {
  data_path = var.data_path != "" ? var.data_path : "${var.repo_path}/data"

  # AIRFLOW_HOME in the base image. Holds the metadata of nothing (that is in Postgres) but does
  # hold task logs and the simple auth manager's password file, so it needs to survive a restart.
  airflow_home = "/opt/airflow"
  passwords_file = "/opt/airflow/simple_auth_manager_passwords.json"

  # The repo read-only, with data/ mounted read-write *inside* it (Docker applies the deeper path
  # second, so the nesting works), and the sibling repo's taxa.db read-only when it exists.
  # Every component gets the same set: the dag processor parses the DAGs, the scheduler runs the
  # tasks, and the api server serves the source view.
  project_mounts = concat(
    [
      { host_path = var.repo_path, container_path = "/opt/project", read_only = true },
      { host_path = local.data_path, container_path = "/opt/project/data", read_only = false },
    ],
    var.inat_cache_path != "" ? [
      { host_path = var.inat_cache_path, container_path = "/opt/inat-cache", read_only = true },
    ] : [],
  )

  # Every component must agree on all of this, so it is built once.
  common_env = [
    # Airflow is pip-installed into /home/airflow/.local (a --user install in the base image), and
    # Python finds that through HOME. These containers run as the *host* uid so that what they
    # write into data/ belongs to Livia, which changes HOME — and the one-shot jobs override the
    # entrypoint that would otherwise paper over it, so `airflow` there dies with
    # "ModuleNotFoundError: No module named 'airflow'". Setting it explicitly fixes both.
    "HOME=/home/airflow",
    # Same root cause, one layer on: that uid has no /etc/passwd entry, so every CLI command dies
    # in its own metrics collection with "OSError: No username set in the environment" —
    # getpass.getuser() falls back to the password database when no USER/LOGNAME is set.
    "USER=airflow",
    "AIRFLOW__CORE__EXECUTOR=LocalExecutor",
    "AIRFLOW__DATABASE__SQL_ALCHEMY_CONN=${var.metadata_db_uri}",
    # Tasks do not reach the database in Airflow 3; they call the execution API, and every
    # component needs to know where that is and share the secret its tokens are signed with.
    "AIRFLOW__CORE__EXECUTION_API_SERVER_URL=http://airflow-apiserver:8080/execution/",
    "AIRFLOW__API_AUTH__JWT_SECRET=${var.jwt_secret}",
    "AIRFLOW__CORE__FERNET_KEY=${var.fernet_key}",
    # The default bundle, "dags-folder", reads core.dags_folder — which is how a bind-mounted
    # dags/ becomes the DAG source without configuring a git bundle.
    "AIRFLOW__CORE__DAGS_FOLDER=/opt/project/dags",
    "AIRFLOW__CORE__LOAD_EXAMPLES=False",
    "AIRFLOW__CORE__DAGS_ARE_PAUSED_AT_CREATION=False",
    # The simple auth manager is the default with no FAB provider installed. Its password file is
    # normally generated and printed into the logs; seeding it from tfvars instead means the UI
    # password is the one in your tfvars and is not in any log.
    "AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_USERS=admin:admin",
    "AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_PASSWORDS_FILE=${local.passwords_file}",
    "AIRFLOW__SCHEDULER__ENABLE_HEALTH_CHECK=True",

    # -- this project ---------------------------------------------------------------------------
    # dags/ imports dags._common, which needs the repo root itself importable — Airflow puts the
    # dags folder on sys.path, not its parent — and `src` comes from the same place.
    "PYTHONPATH=/opt/project",
    "MATCHER_DATA_DIR=/opt/project/data",
    # Independently of DATA_DIR on purpose: the frozen models are the one committed thing inside
    # data/, and a mount over data/ must not hide them.
    "MATCHER_MODEL_DIR=/opt/project/data/models",
    "MATCHER_TAXA_DB=/opt/inat-cache/taxa.db",
    "MATCHER_WORKERS=${var.workers}",
    "MLFLOW_TRACKING_URI=${var.mlflow_tracking_uri}",
    # dbt writes logs/ and target/ inside the project directory by default, which the read-only
    # repo mount forbids — `dbt ls` then fails with PermissionError before doing anything.
    "DBT_LOG_PATH=/opt/project/data/dbt/logs",
    "DBT_TARGET_PATH=/opt/project/data/dbt/target",
    "OMP_NUM_THREADS=4",
    "PYTHONHASHSEED=0",
  ]
}

resource "docker_image" "airflow" {
  name         = "${var.name_prefix}-airflow:${var.airflow_version}"
  keep_locally = true
  force_remove = false

  build {
    context    = var.build_context
    dockerfile = var.dockerfile
    tag        = ["${var.name_prefix}-airflow:${var.airflow_version}"]
  }

  # Keyed on the Dockerfile's hash, like the mlflow module: without it the provider re-evaluates
  # the build context every run and reports a diff forever, which is how an IaC demo stops being
  # trustworthy. The repo is mounted, not copied, so its contents are not part of the image.
  triggers = {
    dockerfile_sha256 = filesha256("${var.build_context}/${var.dockerfile}")
  }
}

resource "docker_volume" "home" {
  name = "${var.name_prefix}-airflow-home"
}

# Airflow's metadata database, next to MLflow's on the same server. A one-shot job because the
# docker provider has no database resource and the postgres image only creates POSTGRES_DB;
# `psql` is in the Airflow image already, so this needs no second image. Idempotent: it checks
# pg_database first, so a re-apply that recreates it does not fail.
resource "docker_container" "create_db" {
  name       = "${var.name_prefix}-airflow-createdb"
  image      = docker_image.airflow.image_id
  must_run   = false
  attach     = true
  logs       = true
  user       = "${var.uid}:0"
  entrypoint = ["/bin/bash", "-c"]
  env        = ["HOME=/home/airflow", "USER=airflow"]

  command = [
    <<-EOT
      set -euo pipefail
      psql "${var.postgres_admin_uri}" -tAc "SELECT 1 FROM pg_database WHERE datname='${var.metadata_db_name}'" \
        | grep -q 1 \
        || psql "${var.postgres_admin_uri}" -c 'CREATE DATABASE ${var.metadata_db_name}'
      echo "database ${var.metadata_db_name} present"
    EOT
  ]

  networks_advanced {
    name = var.network_name
  }
}

# Schema migration, the admin password, and the one-slot DuckDB pool. Everything that must happen
# once before any component starts, in the order it must happen in.
resource "docker_container" "init" {
  name       = "${var.name_prefix}-airflow-init"
  image      = docker_image.airflow.image_id
  must_run   = false
  attach     = true
  logs       = true
  user       = "${var.uid}:0"
  entrypoint = ["/bin/bash", "-c"]
  # The password and its destination go through the environment rather than into the command
  # string, so neither is interpolated into a shell line that ends up in `docker inspect`.
  env = concat(local.common_env, [
    "ADMIN_PASSWORD=${var.admin_password}",
    "PASSWORDS_FILE=${local.passwords_file}",
  ])
  depends_on = [docker_container.create_db]

  command = [
    <<-EOT
      set -euo pipefail
      airflow db migrate
      python -c 'import json, os; open(os.environ["PASSWORDS_FILE"], "w").write(json.dumps({"admin": os.environ["ADMIN_PASSWORD"]}))'
      airflow pools set ${var.duckdb_pool} ${var.duckdb_pool_slots} 'DuckDB permits one writing process per database file; Cosmos runs one dbt process per model'
      echo "airflow initialised"
    EOT
  ]

  # `airflow db migrate` talks to Postgres by its network alias, so this job has to be on the
  # platform network like everything else — without it the migration dies on
  # "could not translate host name 'postgres'".
  networks_advanced {
    name = var.network_name
  }

  volumes {
    volume_name    = docker_volume.home.name
    container_path = local.airflow_home
  }
}

# -- the four components -----------------------------------------------------------------------
#
# One resource each rather than a for_each over a map: the healthchecks genuinely differ (the api
# server has an HTTP endpoint, the other three are checked with `airflow jobs check`), and only
# the scheduler runs tasks and so needs /dev/shm and the CPU-bound mounts. A for_each would spend
# more conditionals hiding those differences than it would save in lines.

resource "docker_container" "apiserver" {
  name       = "${var.name_prefix}-airflow-apiserver"
  image      = docker_image.airflow.image_id
  restart    = "unless-stopped"
  user       = "${var.uid}:0"
  command    = ["api-server"]
  env        = local.common_env
  depends_on = [docker_container.init]

  networks_advanced {
    name    = var.network_name
    aliases = ["airflow-apiserver"]
  }

  ports {
    internal = 8080
    external = var.host_port
  }

  volumes {
    volume_name    = docker_volume.home.name
    container_path = local.airflow_home
  }

  dynamic "volumes" {
    for_each = local.project_mounts
    content {
      host_path      = volumes.value.host_path
      container_path = volumes.value.container_path
      read_only      = volumes.value.read_only
    }
  }

  wait = true
  healthcheck {
    test         = ["CMD", "curl", "--fail", "http://localhost:8080/api/v2/monitor/health"]
    interval     = "10s"
    timeout      = "10s"
    retries      = 18
    start_period = "20s"
  }
}

resource "docker_container" "scheduler" {
  name       = "${var.name_prefix}-airflow-scheduler"
  image      = docker_image.airflow.image_id
  restart    = "unless-stopped"
  user       = "${var.uid}:0"
  command    = ["scheduler"]
  env        = local.common_env
  depends_on = [docker_container.apiserver]

  # This is where tasks actually run under LocalExecutor, so this is the container that needs
  # room for candidate generation's process pool: mp.Pool against Docker's default 64 MB
  # /dev/shm is a documented way to hang a worker pool.
  shm_size = 1024

  networks_advanced {
    name    = var.network_name
    aliases = ["airflow-scheduler"]
  }

  volumes {
    volume_name    = docker_volume.home.name
    container_path = local.airflow_home
  }

  dynamic "volumes" {
    for_each = local.project_mounts
    content {
      host_path      = volumes.value.host_path
      container_path = volumes.value.container_path
      read_only      = volumes.value.read_only
    }
  }

  wait = true
  healthcheck {
    test     = ["CMD-SHELL", "airflow jobs check --job-type SchedulerJob --hostname \"$${HOSTNAME}\""]
    interval = "10s"
    timeout  = "10s"
    retries  = 18
  }
}

resource "docker_container" "dag_processor" {
  name       = "${var.name_prefix}-airflow-dag-processor"
  image      = docker_image.airflow.image_id
  restart    = "unless-stopped"
  user       = "${var.uid}:0"
  command    = ["dag-processor"]
  env        = local.common_env
  depends_on = [docker_container.apiserver]

  networks_advanced {
    name    = var.network_name
    aliases = ["airflow-dag-processor"]
  }

  volumes {
    volume_name    = docker_volume.home.name
    container_path = local.airflow_home
  }

  dynamic "volumes" {
    for_each = local.project_mounts
    content {
      host_path      = volumes.value.host_path
      container_path = volumes.value.container_path
      read_only      = volumes.value.read_only
    }
  }

  wait = true
  healthcheck {
    test     = ["CMD-SHELL", "airflow jobs check --job-type DagProcessorJob --hostname \"$${HOSTNAME}\""]
    interval = "10s"
    timeout  = "10s"
    retries  = 18
  }
}

# No deferrable operator in this project uses it today. It is here because the api server reports
# the triggerer's health as part of the stack's own health, and because an AssetWatcher — the
# obvious next step for reading the checker's findings — needs one.
resource "docker_container" "triggerer" {
  name       = "${var.name_prefix}-airflow-triggerer"
  image      = docker_image.airflow.image_id
  restart    = "unless-stopped"
  user       = "${var.uid}:0"
  command    = ["triggerer"]
  env        = local.common_env
  depends_on = [docker_container.apiserver]

  networks_advanced {
    name    = var.network_name
    aliases = ["airflow-triggerer"]
  }

  volumes {
    volume_name    = docker_volume.home.name
    container_path = local.airflow_home
  }

  dynamic "volumes" {
    for_each = local.project_mounts
    content {
      host_path      = volumes.value.host_path
      container_path = volumes.value.container_path
      read_only      = volumes.value.read_only
    }
  }

  wait = true
  healthcheck {
    test     = ["CMD-SHELL", "airflow jobs check --job-type TriggererJob --hostname \"$${HOSTNAME}\""]
    interval = "10s"
    timeout  = "10s"
    retries  = 18
  }
}
