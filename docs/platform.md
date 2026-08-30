# The platform

The stack the pipeline reports to: an MLflow tracking server, a Postgres backend store and a
MinIO artifact store, provisioned by Terraform through the Docker provider.

**What this is, honestly.** A local stack defined as code. What it demonstrates is that the
infrastructure is reproducible and tear-down-able and that the HCL has actually been applied —
not that it is multi-region, highly available, or production-shaped. The alternatives that were
considered and rejected, including production-shaped AWS HCL that would never have been run, are
in [`platform-design.md`](platform-design.md) §2.1.

It is deliberately **not** in `compose.yaml`. That file runs the pipeline, and states the
invariant on its first line: no service appears in both. Spec §7 milestone 16 adds an Airflow
module to the same environment and changes nothing about these three.

## Running it

```sh
cd terraform/envs/local
cp terraform.tfvars.example terraform.tfvars   # then change every value
cd -
make platform-up          # terraform init + apply, prints the export line
make platform-plan        # a clean plan after apply is milestone 15's acceptance check
make platform-down        # destroy; takes the volumes with it
```

`terraform.tfvars` is gitignored and has no defaults in `variables.tf`, so `apply` fails asking
for credentials rather than standing a stack up on a password that is in a public repo.

Then point the pipeline at it:

```sh
export MLFLOW_TRACKING_URI=http://localhost:5000     # `make platform-url` prints this
```

With that unset, everything still works: training runs untracked and scoring loads the models
from `data/models/`. The five-minute path, the container and CI never need the stack.

| Service | Host port | What it is |
|---|---|---|
| MLflow | 5000 | Tracking server, UI, and the model registry |
| MinIO console | 9001 | Artifact store, browsable |
| MinIO S3 API | 9000 | What the tracking server writes artifacts to |
| Postgres | 5432 | Backend store |

## Why each piece is the way it is

**Postgres, not the file store.** The MLflow Model Registry is not supported on a file backend,
and the registry is the whole point of milestone 15 — it is what replaces a paragraph in
`CLAUDE.md` as the mechanism keeping the milestone 6/7 models frozen.

**The server image is built, not pulled.** `ghcr.io/mlflow/mlflow:v3.15.2` is a bare
`pip install --no-cache mlflow==$VERSION`: verified to ship `sqlalchemy` and `alembic` but
**neither `psycopg2` nor `boto3`**, so as published it cannot reach either store.
`docker/Dockerfile.mlflow` adds exactly those two.

**The server runs Python 3.10; this project runs 3.14.** They speak HTTP, so the interpreters are
unrelated. That is also why the known `mlflow server` failure under 3.13/3.14
([mlflow#18868](https://github.com/mlflow/mlflow/issues/18868)) cannot affect this stack.

**Artifacts are proxied.** The server is run with `--serve-artifacts --artifacts-destination
s3://mlflow/`, so clients address artifacts as `mlflow-artifacts:/` over HTTP and the server is
the only thing holding S3 credentials. Verified: a client with `boto3` not installed at all
round-trips a model with bit-identical predictions. That keeps both a dependency and a secret out
of the pipeline image.

## Two things that will bite

**Postgres 18 moved the data directory.** The volume mounts at `/var/lib/postgresql`, *not*
`/var/lib/postgresql/data`. Since 18 the image keeps data in a major-version-specific
subdirectory so `pg_upgrade --link` does not cross a mount boundary, and it refuses to start if
it finds data at the old path — restart-looping, so the symptom is a healthcheck timeout rather
than a readable error (docker-library/postgres#1259).

**Proxied artifacts are not fully proxied on download.** With an S3-backed destination the server
advertises multipart downloads, and a client that has not been told otherwise then asks for a
*presigned* URL and fetches it directly — at `http://minio:9000`, which resolves on the docker
network but not from the host, so the download hangs rather than failing. Clients must set:

```sh
MLFLOW_ENABLE_PROXY_MULTIPART_DOWNLOAD=false
MLFLOW_ENABLE_PROXY_MULTIPART_UPLOAD=false
```

Anything going through the pipeline will have these set for it once `src/tracking.py` lands
(spec §7 milestone 15, next slice); until then, and for anyone using the UI's own download links
or writing a client by hand, set them yourself. The alternative fix — making
`MLFLOW_S3_ENDPOINT_URL` resolve identically inside and outside the docker network — ties the
configuration to a machine's IP address and was rejected for that.
