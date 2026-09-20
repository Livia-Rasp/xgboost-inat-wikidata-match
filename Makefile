# One target per pipeline stage. These are the same boundaries spec §7 milestone 16 turns into
# Airflow tasks, so the DAG and this file should stay in step.
#
# PYTHON defaults to the checkout's venv and is overridden to plain `python` inside the
# container, where the venv is already on PATH.

PYTHON ?= .venv/bin/python
UV     ?= $(PYTHON) -m uv
DBT    ?= .venv/bin/dbt
IMAGE  ?= ghcr.io/livia-rasp/xgboost-inat-wikidata-match
TF_ENV ?= terraform/envs/local

.PHONY: help lock sync test lint wikidata ancestors candidates features features-sql parity \
        baseline train final-models challenger gold figures fixtures all image image-airflow shell \
        platform-up platform-plan platform-down platform-url platform-scratch airflow-logs

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

# -- environment ------------------------------------------------------------------------------

lock:  ## Re-resolve uv.lock from pyproject.toml
	$(UV) lock

sync:  ## Install the locked dependencies into .venv, including dev tooling
	$(UV) sync --locked --extra dev --extra dbt --extra tracking

# -- checks -----------------------------------------------------------------------------------

test:  ## Run the test suite (fixtures only, no network, no data/)
	$(PYTHON) -m pytest

lint:  ## ruff check
	$(PYTHON) -m ruff check .

# -- pipeline (spec §7 milestones 1-7, in dependency order) -----------------------------------

wikidata:  ## milestone 2: batched SPARQL pull of taxa carrying P3151
	$(PYTHON) -m src.wikidata

ancestors:  ## milestone 4's input: transitive P171 ancestor chains
	$(PYTHON) -m src.wikidata --ancestors

candidates:  ## milestones 1+3: build the lookup cache, then generate candidates
	$(PYTHON) -m src.candidates

features:  ## milestone 4: the pandas feature build, into data/features_pandas.parquet (the parity comparand)
	$(PYTHON) -m src.features

# Run from the repo root: dbt does not chdir, so `data/` in dbt/profiles.yml and in
# fct_features' `location` resolves against the caller's working directory, not dbt/.
features-sql:  ## milestone 15: build the CANONICAL feature table with dbt, into data/features.parquet
	$(DBT) build --project-dir dbt --profiles-dir dbt

parity:  ## milestone 14: diff the dbt feature table against the pandas one, column by column
	$(PYTHON) build_parity_report.py

baseline:  ## milestone 5: the exact-match baseline, per fold and overall
	$(PYTHON) -m src.evaluate

train:  ## milestone 6: both objectives, 5-fold OOF, thresholds
	$(PYTHON) -m src.train

final-models:  ## refit both variants on all folds into data/models/
	$(PYTHON) -m src.train --final

challenger:  ## milestone 16: train a challenger, score it against the champion, promote or hold
	$(PYTHON) -m src.promote

gold:  ## milestone 7: score the hand-labelled gold set
	$(PYTHON) -m src.evaluate --gold

figures:  ## milestone 11: regenerate docs/img/
	$(PYTHON) build_figures.py

fixtures:  ## regenerate tests/fixtures/ from the full caches
	$(PYTHON) build_fixtures.py

# The full path from an empty data/, in order. Roughly 15 minutes, most of it waiting on WDQS.
# features-sql builds the canonical table the model trains on; features builds the pandas
# comparand `parity` diffs it against. Both, in that order, so `make all` ends with a run whose
# feature table has been checked against a second independent implementation.
all: wikidata candidates ancestors features-sql features parity baseline train final-models gold  ## Run every stage in order

# -- containers -------------------------------------------------------------------------------

image:  ## Build the pipeline image
	docker build -f docker/Dockerfile -t $(IMAGE):dev .

image-airflow:  ## Build the Airflow image (unused until milestone 16; proves the lock resolves)
	docker build -f docker/Dockerfile.airflow -t $(IMAGE)-airflow:dev .

shell:  ## Interactive shell in the pipeline image
	docker compose run --rm --entrypoint bash pipeline

# -- platform (spec §7 milestone 15; milestone 16 adds Airflow to the same environment) --------
#
# Terraform, not compose: compose.yaml runs the pipeline and states the invariant at its first
# line that no service appears in both files. Needs terraform/envs/local/terraform.tfvars, which
# is gitignored — copy terraform.tfvars.example and change every value.

platform-up:  ## terraform apply: Postgres + MinIO + MLflow + the four Airflow components
	terraform -chdir=$(TF_ENV) init -input=false
	terraform -chdir=$(TF_ENV) apply -auto-approve -input=false
	@echo
	@terraform -chdir=$(TF_ENV) output -raw tracking_env; echo
	@echo "Airflow: $$(terraform -chdir=$(TF_ENV) output -raw airflow_url) (user 'admin')"

platform-plan:  ## terraform plan; a clean plan after apply is the milestone's acceptance check
	terraform -chdir=$(TF_ENV) plan -input=false

# Milestone 16's other acceptance check — apply from nothing, plan clean, destroy — run against a
# throwaway copy of the same configuration rather than against the real stack. `make platform-down`
# takes the volumes with it, and those volumes hold the MLflow registry every published number
# resolves to: the v1 backfill, the ladder rungs, the champion. A Terraform *workspace* gives the
# copy its own state, and the overrides below give it its own container names, network and ports,
# so the two stacks cannot touch each other. The trap puts the workspace back even if apply fails
# half way — otherwise the next `make platform-up` would silently target the scratch stack.
SCRATCH_VARS = -var name_prefix=inat-scratch -var mlflow_port=5010 -var postgres_port=5442 \
               -var minio_api_port=9010 -var minio_console_port=9011 -var airflow_port=8090

platform-scratch:  ## Stand the whole stack up from nothing in a throwaway workspace, then destroy it
	@set -e; \
	trap 'terraform -chdir=$(TF_ENV) workspace select default' EXIT; \
	terraform -chdir=$(TF_ENV) workspace new scratch 2>/dev/null || terraform -chdir=$(TF_ENV) workspace select scratch; \
	terraform -chdir=$(TF_ENV) apply -auto-approve -input=false $(SCRATCH_VARS); \
	echo "--- a second plan must report no changes:"; \
	terraform -chdir=$(TF_ENV) plan -detailed-exitcode -input=false $(SCRATCH_VARS); \
	docker ps --filter name=inat-scratch --format '{{.Names}} {{.Status}}'; \
	terraform -chdir=$(TF_ENV) destroy -auto-approve -input=false $(SCRATCH_VARS)

platform-down:  ## terraform destroy: removes the containers, and the volumes with them
	terraform -chdir=$(TF_ENV) destroy -auto-approve -input=false

platform-url:  ## Print the export line that points the pipeline at the stack, and the Airflow URL
	@terraform -chdir=$(TF_ENV) output -raw tracking_env; echo
	@echo "Airflow: $$(terraform -chdir=$(TF_ENV) output -raw airflow_url) (user 'admin')"

airflow-logs:  ## Tail the Airflow scheduler's log (where task output lands under LocalExecutor)
	docker logs -f $$(terraform -chdir=$(TF_ENV) output -json airflow_container_names | tr -d '[]"' | cut -d, -f2)
