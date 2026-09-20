# The platform

The stack the pipeline reports to and runs on: an MLflow tracking server, a Postgres backend
store, a MinIO artifact store, and Airflow — all provisioned by Terraform through the Docker
provider.

**What this is, honestly.** A local stack defined as code. What it demonstrates is that the
infrastructure is reproducible and tear-down-able and that the HCL has actually been applied —
not that it is multi-region, highly available, or production-shaped. The alternatives that were
considered and rejected, including production-shaped AWS HCL that would never have been run, are
in [`platform-design.md`](platform-design.md) §2.1.

It is deliberately **not** in `compose.yaml`. That file runs the pipeline, and states the
invariant on its first line: no service appears in both. Milestone 16 added the Airflow module to
this same environment and changed nothing about the other three — its `plan` was `8 to add,
0 to change`.

## Running it

```sh
cd terraform/envs/local
cp terraform.tfvars.example terraform.tfvars   # then change every value
cd -
make platform-up          # terraform init + apply, prints the export line
make platform-plan        # a clean plan after apply is milestone 15's acceptance check
make platform-scratch     # stand the whole stack up from nothing, then tear it down
make platform-down        # destroy; takes the volumes with it
```

`make platform-scratch` is milestone 16's *other* acceptance check — apply from a torn-down state,
then a clean plan — run against a **throwaway copy**. `platform-down` would take the volumes with
it, and those volumes hold the registry every published number resolves to: the v1 backfill, the
ladder rungs, the champion. A Terraform workspace gives the copy its own state and the overrides
give it its own container names, network and ports, so the two cannot touch each other; the
workspace is restored even if the apply fails half way. Verified: 19 resources up from nothing, a
second plan reporting no changes, then all 19 destroyed, with the real stack untouched throughout.

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
| Airflow | 8080 | UI and REST API; log in as `admin` |
| MLflow | 5000 | Tracking server, UI, and the model registry |
| MinIO console | 9001 | Artifact store, browsable |
| MinIO S3 API | 9000 | What the tracking server writes artifacts to |
| Postgres | 5432 | Backend store for MLflow, and Airflow's metadata database |

## Airflow

Four containers on **LocalExecutor** — `api-server`, `scheduler` (which runs the tasks: the
executor is a property of the scheduler, not a separate service), `dag-processor` and `triggerer`
— plus two one-shot jobs that create the metadata database and migrate it. No Redis and no worker
container, which is six containers' worth of stack reduced to four.

**The repository is bind-mounted, read-only, with `data/` read-write inside it.** The image carries
dependencies; the code comes from your checkout, so an edit is measurable on the next DAG run with
no image rebuild. That is the working loop this whole milestone exists for: change a feature or a
hyperparameter, trigger, compare in MLflow. `tracking.git_sha()` still records the commit *and*
whether the tree was dirty, so a run from uncommitted code says so.

| DAG | Trigger | What it does |
|---|---|---|
| `taxonomy_ingest` | manual, `force_refresh` param | lookup index, Wikidata pull, ancestor chains, candidates |
| `feature_build` | `ingest_complete` asset | dbt build (one task per model), pandas comparand, parity report |
| `train_and_evaluate` | `features` asset | OOF + refit, gold scoring against the champion, the promotion gate |
| `score_ambiguous` | `@daily` | rank the checker's ambiguous queue with the champion, read-only |

Ingest is manual on purpose ([`platform-design.md`](platform-design.md) §5.4 amendment 1): the
Wikidata cache has no staleness check, so a scheduled run either does nothing or forces a re-pull
that moves the training population — and then a retrain changes code *and* data at once, which is
exactly what the gate cannot attribute.

Everything after ingest is asset-driven, and **an asset event is only emitted when the artefact's
content actually changed**. Re-running ingest on a cache hit therefore does not start a retrain
that would spend three minutes reproducing the champion exactly.

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

## Things that will bite

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

**MLflow only trusts Host headers it knows.** Since 3.15 the server answers HTTP 403 *"Invalid
Host header - possible DNS rebinding attack detected"* to anything outside its defaults, which
cover localhost and private IPs — so every client inside the docker network, sending
`Host: mlflow:5000`, is refused. The module passes `--allowed-hosts`; setting it **replaces** the
defaults, and each entry matches the header literally, port included, unless it contains a
wildcard. The symptom is indirect: `resolve_model()` treats any registry error as "no champion
registered yet" and falls back to the committed models, so the visible failure was the promotion
gate reporting no champion.

**A one-shot job that fails does not fail the apply.** The Docker provider creates a
`must_run = false` container and reports success whatever exit code it returns, so when Airflow's
init job died, the apply carried on and surfaced it a minute later as *"container failed to be in
healthy state"* against the **api server** — a component that was fine. When a component will not
go healthy, read `docker logs inat-match-airflow-init` before believing the resource the error
names. (The same applies to MinIO's bucket job.)

**Airflow lives in `/home/airflow/.local`, and these containers do not run as `airflow`.** They run
as the host uid so that what they write into `data/` belongs to you rather than to uid 50000 —
which changes `HOME`, and Python then cannot find the `--user` install. The long-running
components survive it because the image's entrypoint compensates; the one-shot jobs override that
entrypoint and die with `ModuleNotFoundError: No module named 'airflow'`. `HOME=/home/airflow` is
set explicitly for all of them.

**A Fernet key cannot be validated by length in HCL.** `base64decode` returns a string and errors
on anything that is not valid UTF-8 — which 32 random bytes essentially never are — and it rejects
the urlsafe alphabet (`-`, `_`) outright. `length(base64decode(...)) == 32` therefore rejects every
key `Fernet.generate_key()` produces. The variable validates the shape instead:
`^[A-Za-z0-9_-]{43}=$`.

Anything going through the pipeline will have these set for it once `src/tracking.py` lands
(spec §7 milestone 15, next slice); until then, and for anyone using the UI's own download links
or writing a client by hand, set them yourself. The alternative fix — making
`MLFLOW_S3_ENDPOINT_URL` resolve identically inside and outside the docker network — ties the
configuration to a machine's IP address and was rejected for that.
