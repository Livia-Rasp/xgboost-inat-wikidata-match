# Platform design — spec milestones 13–16

The design doc for the productionization work: Docker, a dbt-core/DuckDB transformation layer,
MLflow, and an Airflow DAG on Terraform-provisioned infrastructure. Spec §7 states what each
milestone has to demonstrate; this file records *how*, *which versions*, and *why these choices
rather than the alternatives*. It is a design doc, so rejected options are kept here on purpose —
they are the part that is expensive to re-derive.

Written 2026-08-26, before any of the four milestones had code.

---

## 1. What problem this actually solves

Milestones 1–12 produced a working classifier and a defensible set of numbers. They also produced:

- a pipeline that exists as six `python -m src.*` invocations whose correct order lives only in
  prose in `CLAUDE.md`,
- seven hand-rolled cache manifests in **three mutually incompatible flavours** — mtime-based
  (`candidates`), row-count-based (`features`), and content-fingerprint-based (`wikidata`,
  `ancestors`, `gold attributes`, `observation counts`) — each re-implementing staleness detection
  slightly differently,
- no dependency lockfile, against a stack (`xgboost`, `scikit-learn`, `shap`) where version skew
  changes results and, in the calibrators' case, breaks loading outright,
- no record of which code produced which number beyond git history and a prose freeze,
- and two functions that are pipeline stages in every sense except that they have no way to be
  invoked.

None of that is a modelling problem, which is why milestones 13–16 are explicitly not expected to
improve any metric. The goal is that the pipeline can be run, reproduced and inspected by someone
who did not write it.

---

## 2. Decisions taken, and what was rejected

### 2.1 Terraform provisions the local Docker stack

**Chosen:** the `kreuzwerker/docker` provider, provisioning the containers, networks and volumes
the platform runs on, in one `envs/local` environment.

**Rejected — production-shaped AWS HCL, validated but never applied.** It would look the most
"real" and would be the most dishonest thing in the repo: several hundred lines of infrastructure
code that has never been executed, in a project whose stated rule is never to describe unverified
work as done. `terraform validate` proves syntax, not that the thing stands up.

**Rejected — LocalStack with genuine `aws_s3_bucket` / `aws_ecr_repository` resources applied via
`tflocal`.** Genuinely tempting: real AWS-provider HCL that really runs, for free. Rejected as a
second moving part that buys authenticity in the *provider name* rather than in the
infrastructure, on a project that has no cloud footprint to justify it. Worth revisiting only if
the stack ever actually needs an object store that MinIO cannot stand in for.

The honest framing, which belongs in `docs/platform.md` when milestone 16 lands: this is a local
stack defined as code, and the thing being demonstrated is that it is reproducible and
tear-down-able, not that it is multi-region.

### 2.2 The model freeze is released, deliberately

The milestone 6/7 models are currently frozen, and every published number is quoted against them
(`CLAUDE.md`, milestone 7). Moving feature construction into SQL puts that in direct conflict:
DuckDB's `jaro_winkler_similarity` is not guaranteed to agree with `rapidfuzz`'s to the last bit,
and two other changes (§4.3) are known in advance to move rows.

**Chosen:** accept the drift, retrain, and re-report every affected number, with the old models
preserved as registry version 1 so the published history stays traceable.

**Rejected — hold the freeze absolutely and force bit-for-bit parity**, using dbt Python models
calling `rapidfuzz` wherever DuckDB disagrees. This keeps the report untouched and makes the
migration provably behaviour-preserving, which is a real virtue. Rejected because it would leave
the transformation layer as SQL-shaped wrapping around Python string functions — the appearance of
a dbt migration without the substance — and because the retrain is itself the most interesting
thing MLflow can be pointed at.

**Consequence to keep in view:** milestone 15 rewrites the README results table, `findings.md`
§§1–8, the six figures and the notebook's milestone 6/7 sections. That is the one irreversible
step in 13–16.

### 2.3 dbt owns features; candidate generation stays in Python

**Chosen:** dbt computes labels, all 41 feature columns and the fold assignment, reading
`candidates.parquet` as a source. `candidates.py` is untouched.

**Rejected — port candidate generation too**, with strategies 1/2/3/5 as SQL joins over DuckDB's
`levenshtein` and strategy 4 as a dbt Python model wrapping the existing FTS5 code. A larger
lineage graph, but the Python model would be a lineage node that is not really a transformation,
and it puts the recall ceiling — the number that caps everything downstream — at risk for
presentation value.

**Rejected — full replacement using DuckDB's own `fts` extension.** DuckDB's FTS is stemmed
token BM25, not trigram. It cannot reproduce the chunked-trigram behaviour that
`_trigram_candidates` relies on, so recall would change in a way that needs its own study before
anything downstream could be trusted. `candidates.py`'s 6-character-chunk/stride-4 construction is
also the single most considered piece of code in the repo (the docstring records the ~100×
speed-up over per-trigram OR, and why chunking ranks *better* under typos) — replacing it to widen
a lineage graph would be a straight downgrade.

### 2.4 No writes into `wikidata-inat-checker`

The sibling repo now reserves `score` and `scoredBy` fields, always null, on every ambiguous
candidate payload (`lib/discoverLinks.js`), and its `docs/links.md` has a section describing a
confidence model filling them in. It is a standing invitation.

**Chosen, for now:** read only. Milestone 16's scoring DAG reads the checker's ambiguous findings
and writes its output locally. Nothing is written back.

**Deferred, with the direction inverted:** when this is built, `wikidata-inat-checker` should call
a scoring service exposed by this repo, rather than this repo pushing rows into its database.
That keeps the model's deployment surface here, where the registry and the feature code are, and
keeps the checker's database owned by exactly one writer. Recorded in `docs/future-work.md`; not
settled.

### 2.5 Everything lands in this repo

New top-level `docker/`, `dbt/`, `dags/`, `terraform/` alongside `src/`. A separate platform repo
would keep this one tidy at the cost of splitting one story across two clones and inventing a
packaging boundary between them for no current benefit.

---

## 3. Tooling: versions and why

Researched August 2026; pins should be re-checked if this sits unbuilt for long.

**Python 3.14 everywhere**, matching the interpreter the committed numbers were produced under.
The versions below are what `uv lock` actually resolved against it in milestone 13, not what was
guessed beforehand — two of the guesses were wrong and are marked.

| Tool | Resolved | Why this version, and what matters |
|---|---|---|
| Apache Airflow | **3.3.1** | Airflow 3 replaced the webserver with an `api-server` and split the DAG processor into its own service, so any pre-3.0 compose file or tutorial is misleading. Brings first-class **Assets** with event-driven scheduling, DAG versioning, and the Task SDK. Supports 3.10–3.14, and `apache/airflow:3.3.1-python3.14` is published (plus a slim variant). Note: if the `api-server` is unavailable or CPU-starved the `dag-processor` hangs on import — fix the api-server first when debugging. |
| dbt-core | **1.12.3** | Corrected from an earlier `1.10.x` pin: **3.14 support landed in 1.12.x**, so 1.10 was never an option here. Still 1.x, so the original reason for the pin holds — dbt-core 2.0 is in beta and DuckDB is one of the *last* adapters on that track (Snowflake/BigQuery/Databricks/Redshift in preview; Spark and DuckDB in beta). |
| dbt-duckdb | **1.11.0** | Also corrected: the adapter's numbering has moved on from the `1.3.x` this doc originally recorded. Two features carry the design: `materialized='external'` writes a model straight out as parquet/csv/json at a chosen `location`, so dbt can produce the same `data/features.parquet` the Python side already reads; and Python models, where `dbt.ref()` yields a DuckDB Relation and the function may return a Relation, pandas DataFrame or Arrow table. Limits worth knowing: Python functions cannot be imported between models, ephemeral models cannot be referenced from Python models, and a Python model is single-threaded and memory-bound — filter in SQL upstream. |
| DuckDB | **1.5.5** (needs ≥1.5) | 3.14 was enabled in 1.5.0. `ATTACH '…/taxa.db' (TYPE sqlite)` reads a SQLite file in place, so the 236 MB iNat index needs no copy or import step. Native string similarity — `jaro_winkler_similarity`, `jaro_similarity`, `levenshtein`, `damerau_levenshtein`, `jaccard`, `hamming` — covers the fuzzy features in pure SQL. Verified live on 3.14: `jaro_winkler_similarity('prunella','prunela')` = 0.975 and `levenshtein('rubrum','ruber')` = 3, the latter agreeing with what this project already documents. Caveat: `jaro_winkler_similarity` is case-sensitive and returns 0 below its `score_cutoff`, so normalise before comparing. |
| MLflow | **3.15.2** | `mlflow.xgboost.autolog()` captures params, per-boosting-round metrics, feature importances, model signature and the environment, for both the native and sklearn-style APIs. Use `model_format="json"` for portability across XGBoost versions (`ubj` is faster but less portable). MLflow 3 removed Recipes and several flavors — not used here. |
| astronomer-cosmos | **1.15.1** | Renders a dbt project as Airflow tasks, one per model, via `DbtTaskGroup` inside an existing DAG (or `DbtDag` for a standalone one), so dbt failures land in the Airflow UI at model granularity with per-model retries, rather than as one opaque `dbt build` task. **Its PyPI classifiers claim support only up to 3.12 and are simply stale** — 1.15.1 installs and imports on 3.14 without complaint. Worth remembering before treating classifiers as evidence again. |
| Terraform | ≥ 1.1.5, `kreuzwerker/docker` ≥ 3.0 | The provider's source is shorthand for `registry.terraform.io/kreuzwerker/docker`. Resources used: `docker_image`, `docker_container`, `docker_network`, `docker_volume`. |
| Postgres / MinIO | current stable | MLflow backend store and S3-compatible artifact store. One container each; also what gives Terraform something with real dependency ordering to provision. |

---

## 4. Audit of the existing pipeline

Findings from reading `src/` end to end before designing anything. The first three are bugs that
block containerization; the rest are constraints the design has to respect.

### 4.1 `candidates.manifest.json` keys its cache on file mtimes

`_candidates_manifest_matches()` compares `source_mtimes = {"lookup_sqlite": …,
"wikidata_parquet": …}`. Container image layers and CI checkouts do not preserve mtimes, so the
cache will miss or hit essentially at random the moment anything runs in Docker — silently, since
a spurious miss just looks slow and a spurious hit just looks fast. Fix: content fingerprints,
reusing the existing sha256 pattern from `wikidata._qid_set_fingerprint()` rather than inventing a
fourth staleness scheme. (That function is already sha256-over-sorted-input specifically because
`hash()` is `PYTHONHASHSEED`-randomized — the same instinct, applied one file over.)

### 4.2 Two pipeline stages have no entry point

`wikidata.build_ancestor_chains()` and `train.build_final_models()` are called only from the
notebook and from `build_gold_set.py`. `features.py` does a bare `pd.read_parquet()` on the
ancestors cache and will `FileNotFoundError` on a clean `data/`, which means the README's
milestone-4 line describes a command that cannot currently produce its own input. Both need CLI
flags before they can be Airflow tasks.

### 4.3 Four places where the numbers are order- or seed-dependent

These decide what "drift" means in §2.2, and each must be handled explicitly in SQL rather than
discovered afterwards. The first three came out of the audit; the fourth was found while building
milestone 14 and is the largest of them.

1. **Ancestor-rank resolution is first-wins on row order.** `_wd_ancestor_names_by_rank()` takes
   the first ancestor it sees at each target rank, and a transitive P171 chain really can contain
   two ancestors at the same rank. `build_fixtures.py` measured this: sorting the ancestor fixture
   changed `family_match`/`order_match` on 4 of 2,610 rows and moved `rank:map`'s gold top-1 by
   half a point. SQL has no implicit row order, so this becomes an explicit
   `row_number() over (…)` rule — and the rule chosen is a documented cause of moved numbers.
2. **The 15% synthetic dropout uses `random.Random(42).sample()` over a pandas groupby order.**
   Reproducible in Python, not expressible in SQL. A deterministic hash-modulo selection drops a
   *different* 15%, which is fine but must be named.
3. **`monotone_constraints_tuple()` is positional.** It maps `MONOTONE_UP` onto `FEATURE_COLUMNS`
   by index, so reordering the feature list silently changes which features are constrained.
   Anything that regenerates the feature set must preserve column order, and MLflow should log the
   ordered list as a parameter.

   Milestone 14 found this one has *already* happened, harmlessly: `data/features.parquet` on disk
   carries the ten `strategy_*` columns alphabetically, while `build_features()` emits them in
   `STRATEGY_TAGS` declaration order. Nothing broke, because `train.py` selects by name — but the
   artefact and the code that writes it have drifted apart in exactly the way this item warns
   about, which is a good argument for MLflow logging the ordered list rather than trusting it.
4. **`sim_rank_in_group` ties break on row order too** (`features.py:208`, `.rank(method="first")`).
   Not in the original audit, and bigger than the other three: ties are the common case rather than
   the exception, since every exact-match candidate scores `similarity = 1.0`. The row order it
   falls back on is `candidates.parquet`'s, which comes out of an `imap_unordered` pool and is
   therefore not stable across regenerations even on the pandas side. Same treatment as item 1 —
   an explicit rule (`similarity desc, inat_taxon_id`).

### 4.4 Packaging hazards

- The calibrators are pickled `IsotonicRegression` objects. A container with a different
  scikit-learn will fail to load them. Needs an exact pin now, and MLflow's environment capture as
  the durable fix.
- `USER_AGENT` is built from `importlib.metadata.version("xgboost-inat-wikidata-match")`, so the
  image must `pip install -e .`; copying the source tree alone degrades it to `0.0.0`, which is
  poor manners against a shared public SPARQL endpoint.
- `beautifulsoup4` is imported by `build_gold_labeling_kit.py` and undeclared in `pyproject.toml`.
- `PYTHONHASHSEED` and `OMP_NUM_THREADS` are unset. The project pins seeds meticulously
  everywhere else, but XGBoost's `hist` tree method is thread-count sensitive, so results can
  differ across machines with different core counts. Set both in the image.
- `candidates.py` parallelises with a fork-based `multiprocessing.Pool`, one SQLite connection per
  worker, sized on `os.cpu_count()`. Under a container CPU limit that reports host cores, this
  over-subscribes; the worker count should read the effective limit.

### 4.5 `features.manifest.json` fingerprints on row counts only

Its shape key is five integers and `N_SPLITS`. Any content change that preserves row counts — for
instance a re-pull that returns the same number of rows with different values — will not
invalidate it. dbt's own state handling replaces this in milestone 14, which is the cleanest
possible fix: delete the manifest rather than improve it.

### 4.6 What the sibling repo now offers

`wikidata-inat-checker` migrated its links workflow into a Fastify webapp backed by a durable
`data/findings.db` (STRICT tables, `PRAGMA user_version` migrations, WAL). Relevant here:

- `GET /api/findings?kind=link&status=ambiguous&limit=2000` returns the full evidence blob as
  JSON — `wdChain`, per-candidate `inatChain`, `evidence`, `rank` — which its own docs name as the
  replacement for scraping `links-ambiguous.html`.
- `findings.db` can be attached read-only from DuckDB alongside everything else. A concurrent
  reader against a WAL database is safe.
- Human decisions are a status lifecycle on `(qid, kind='link')`, not a labels table.
  `POST /api/findings/:id/pick` **overwrites the ambiguous row in place**, so the rejected
  candidates are destroyed: positives are recoverable, per-candidate negatives are not, and there
  is no audit trail of what was rejected. Anything wanting rejections as training data needs a
  snapshot before the pick, or an upstream change.
- The HTML contract `build_gold_labeling_kit.py` depends on (`id="row-{qid}"`, `td.wd-col`,
  `tr.candidate-row[data-qid]`) is explicitly preserved on their side and was tested against this
  repo's actual parser. Switching the gold kit to the JSON API is therefore optional, not urgent.

---

## 5. Milestone designs

### 5.1 Milestone 13 — Docker — **done**

`docker/Dockerfile` (multi-stage; builder runs `uv sync` into `/opt/venv`, runtime is
`python:3.14-slim`, non-root uid 1000 matching the sibling repo's convention, editable install
over a source copy), `docker/Dockerfile.airflow` (`FROM apache/airflow:3.3.1-python3.14`),
`.dockerignore`, `compose.yaml`, `Makefile`, `src/paths.py`, `uv.lock`.

Locking is **uv**, not pip-tools. The deciding argument was not speed: `pip-compile` resolves
only for the interpreter it runs under, and this project has three (3.12 and 3.13 in CI, 3.14
locally and in the image). `uv.lock` is universal and covers all of them in one file.

`compose.yaml` has two services and no overlap with milestone 16's Terraform. `pipeline` takes
**no host mounts at all** — the fixtures, `gold/hard_cases.csv` and the frozen models are baked
into the image, which is what makes the acceptance check meaningful on a machine that has never
seen this project. `pipeline-full` adds the read-only `taxa.db` bind and a named volume for
`data/`. They are separate services rather than one with an optional mount because compose fails
outright on a missing bind source, and most people running the five-minute path will not have the
sibling checkout.

The `Makefile` gives each stage one target, which is also the task boundary milestone 16 will
turn into Airflow tasks. The fixes from §4.1–§4.4 all landed, each with a mutation-checked
regression test in `tests/test_paths.py`; §4.1 was a genuine bug, not just an inconvenience.

Two things worth carrying forward:

- **The pipeline image does not install dbt or MLflow.** They are declared and locked so that
  one resolution proves all four platform tools co-exist on one interpreter, and
  `Dockerfile.airflow` is where that proof runs. Installing them into the pipeline image cost
  ~600 MB for code no milestone imports yet; 14 and 15 add their own extras.
- **`MODEL_DIR` is overridable independently of `DATA_DIR`.** Docker seeds an empty named volume
  from the image's content, so the frozen models survive `pipeline-full` by default — but a
  volume that already holds a previous run's output is not empty and is never seeded, and a bind
  mount never is either.

### 5.2 Milestone 14 — dbt-core over DuckDB — **done**

Warehouse at `data/warehouse.duckdb`. Sources: the iNat taxa index via
`ATTACH … (TYPE sqlite, READ_ONLY)`; the Wikidata taxa, ancestors and candidates parquet caches
via `read_parquet()`. Every path derives from `MATCHER_DATA_DIR`, so one profile target serves
both a real run and the fixture-scale build in `tests/test_dbt.py`.

```
staging/       stg_inat_taxa   stg_wd_taxa   stg_wd_ancestors  stg_candidates
               stg_rank_names(.py)  stg_rank_levels(.py)
intermediate/  int_name_parts  int_ancestor_by_rank  int_inat_ancestor_by_rank
               int_name_collisions  int_labels(.py)  int_group_stats
marts/         fct_features (materialized='external' → data/features_dbt.parquet)
               dim_folds(.py)
```

15 models and 103 tests, ~19 seconds over the real 590,671-row frame.

**Four deviations from what this section originally planned**, all pulling the same way: keep the
measured drift down to the part that is worth measuring, so the parity report reads as an argument
rather than as noise.

- **The feature table is written beside `data/features.parquet`, not over it.** The frozen
  milestone 6/7 models are quoted against the pandas artefact, and this is the milestone that only
  *measures* the difference — overwriting it here would destroy the comparand. §5.3 promotes the
  dbt path when it releases the freeze.
- **`normalize.py` is registered as a DuckDB UDF** (`src/dbt_udf.py`, a dbt-duckdb `Plugin` whose
  `configure_connection` calls `create_function`) rather than transcribed into `regexp_extract`.
  It is a token-by-token state machine with `break` semantics, not a regex, and ten of the 41
  features derive from it. A transcription would have buried the deliberate drift of §2.2 under a
  tail of parser bugs in code nobody wanted to change. The known limitation the transcription plan
  already flagged — ligatures (`æ`/`œ`/`ß`) are not decomposed by NFKD and parse empty — is
  preserved for free by using the real function.
- **The 15% synthetic dropout stays the real seeded function**, as a Python model, against
  §4.3.2's plan to accept a hash-modulo selection. `label` and `no_answer_reason` come out
  identical between the two paths, so no downstream metric moves for a reason unrelated to the
  migration.
- **`stg_link_findings` is deferred to milestone 16.** It has no consumer here, and a model that
  fails when the sibling repo is absent would break `dbt build` in the container.

Per-model notes:

- `int_name_parts` — the UDF applied to the ~650k *distinct* name strings across both sides, not
  once per candidate row per side. It returns a `STRUCT`, whose fields must not be declared
  nullable (duckdb#18600 creates the function fine and then fails at call time), and
  `null_handling` is `'special'` so a null name reaches Python's degenerate-input branch.
- `int_ancestor_by_rank` — the explicit `row_number()` rule from §4.3.1: self-as-own-ancestor
  first, then lowest QID number. `int_inat_ancestor_by_rank` is its iNat counterpart, ordering by
  position in the slash-joined `ancestry` string, which *reproduces* the pandas walk rather than
  replacing it — that side was never row-order-dependent.
- `int_group_stats` — `n_candidates`, `sim_rank_in_group`, `sim_margin_to_runner_up` as window
  functions over `wikidata_qid`, with §4.3.4's explicit tie-break.
- `dim_folds` — a dbt **Python** model calling `GroupKFold(5, shuffle=True, random_state=42)` on
  `family_key`. Deliberately not reimplemented in SQL: the leakage guarantee is the one property
  that must not move, and there is nothing to gain from moving it.
- `stg_rank_names` / `stg_rank_levels` — `WD_RANK_TO_NAME` and `RANK_LEVEL` as tables, read from
  `src/labels.py` rather than copied into `seeds/`. A seed CSV would be a second copy of a mapping
  the pandas path also uses, and the parity report would then be measuring the copy.

Tests are where dbt earns its inclusion, because these invariants already matter to this project
and were enforced by `print` statements and a single pytest: the `(wikidata_qid, inat_taxon_id)`
grain, `not_null` across the feature columns, ranges on the similarity columns, the accepted
strategy tags, plus three singular tests — milestone 4's no-QID-in-two-folds leakage check,
milestone 3's ≥97% recall check at `warn` severity, and the grain itself.

Two `not_null` tests that looked obvious turned out to be wrong on real data: `Q2125371` is a
genuine Wikidata taxon with no label, so `stg_wd_taxa.wikidata_name` and `int_name_parts.raw_name`
are both legitimately null. `fct_features` joins the name parts with `is not distinct from` so
that row's parse is reached rather than silently missed.

`build_parity_report.py` (repo root, matching the convention for one-off tooling that drives the
pipeline) diffs the SQL-built feature table against the pandas one column by column: exact-match
rate, max absolute delta, row-level disagreement counts. Its output is `docs/findings.md` §9,
where every non-zero delta has a written cause.

### 5.3 Milestone 15 — MLflow

Tracking server with a Postgres backend and MinIO artifact store. `src/tracking.py` keeps the
instrumentation out of `train.py` and `evaluate.py`.

Parameters worth logging beyond `TREE_PARAMS`: the **ordered** `FEATURE_COLUMNS` (§4.3.3),
`MONOTONE_UP`, `N_SPLITS`, `RANDOM_STATE`, `SYNTHETIC_DROPOUT_FRACTION`, `K`,
`MAX_EDIT_DISTANCE`, and the git SHA — which no manifest records today. The existing OOF
`shape_key` is already almost exactly an MLflow params blob and can be logged as-is.

Metrics: top-1 and MRR on **both** raw and calibrated scores — `findings.md` §3 exists precisely
because these diverge, by 0.03pp at OOF scale and by eleven points on the gold set — plus Brier,
the auto-accept threshold with its coverage and precision, the reject threshold, and per-fold
`best_iteration`. Gold-set metrics log against the same run as the model that produced them, so
evaluation stops being a terminal print.

The registry replaces the prose freeze: the existing `data/models/*.json` are backfilled as
version 1 with their published n=263 metrics attached, the retrain on dbt features becomes
version 2, and the winner takes the champion alias.

**When regenerating the report, do not blanket-execute the notebook.** `CLAUDE.md` records this
trap: a full `nbconvert --execute` re-runs milestone 1's cells, which read live external state,
and it has already silently rewritten that section's own narrative once. Execute only the affected
range and diff against the previous commit's outputs before saving.

### 5.4 Milestone 16 — Airflow and Terraform

Four DAGs on Airflow 3 Assets:

| DAG | Trigger | Shape |
|---|---|---|
| `taxonomy_ingest` | manual (was `@weekly`, see below) | lookup cache → Wikidata pull ∥ ancestor pull → candidate generation; produces four assets |
| `feature_build` | asset-triggered | `DbtTaskGroup` (Cosmos, one task per model, tests inside each) → parity; produces the features asset |
| `train_and_evaluate` | asset-triggered | OOF → final models → gold eval (challenger ∥ champion) → compare against champion → promote or hold |
| `score_ambiguous` | `@daily` | read the checker's ambiguous findings (read-only) → features → score → local parquet (the `.qs` output moved to milestone 10) |

Retries are matched to failure modes this project has actually hit, not a blanket `retries=3`:
WDQS returning HTTP 200 with a silently truncated body (guarded today by
`ANCESTOR_MIN_COVERAGE`, and the *same* failure mode that took four attempts and two upstream
fixes to get a clean `npm run links` run), 429/502/503/504, and hung connections that surface as
timeouts rather than statuses. Deterministic local tasks get no retries.

`compare_to_champion` is a real gate: the DAG fails if gold top-1 or band precision regresses
beyond a stated tolerance. That gate is the difference between a DAG and a shell script.

Terraform, `envs/local`, modules for postgres / minio / mlflow / airflow. **LocalExecutor, not
Celery** — no Redis, four Airflow containers instead of six, which is the right size for a laptop.
Outputs print the service URLs. Credentials come from a gitignored `terraform.tfvars` with a
committed `.example`; nothing secret enters the repo.

#### Amendments made when implementation began (2026-09-16)

The table above is the design as written before any DAG existed. Planning the implementation —
with a round of reading the Airflow 3.3 and Cosmos 1.15 docs — changed six things. Each is recorded
here with its reason rather than silently built differently.

1. **`taxonomy_ingest` is triggered by hand, with a `force_refresh` parameter.** The Wikidata pull
   cache has no staleness check by design (milestone 2 — it must not silently re-hit a shared
   public endpoint), so a `@weekly` run would do nothing unless it *forced* a re-pull. A forced
   re-pull changes the training population — new P3151 statements, including the ones added by
   hand while labelling the gold set — and a retrain would then move code and data at once. The
   point of this milestone, and of everything in `future-work.md` that was deferred until it
   lands, is to measure one change at a time against the champion. Data therefore moves only when
   someone decides it should. Everything downstream of ingest stays asset-triggered.
2. **The repository is bind-mounted into the Airflow containers** (read-only; `data/` read-write),
   rather than copied into the image. The working loop is *edit → trigger → compare in MLflow*,
   and baking the code in would put a multi-gigabyte image rebuild between every edit and its
   measurement. Provenance is not lost: `tracking.git_sha()` runs `git` against the mounted
   checkout and logs the dirty flag, so a run from uncommitted code says so.
3. **The dbt tasks share an Airflow pool with a single slot.** DuckDB allows one writing process
   per database file, and Cosmos renders one Airflow task — one `dbt` process — per model. Run in
   parallel they fail with `Could not set lock on file`. The pool serialises them without giving
   up the per-model tasks (and per-model retries and logs) that are the reason to use Cosmos.
4. **The features asset is emitted by an explicit task of this project's, not by Cosmos.** In
   `ExecutionMode.LOCAL`, Cosmos derives the assets it emits from dbt's OpenLineage artefacts,
   and when that parsing fails it emits nothing, without a warning
   ([astronomer-cosmos#2959](https://github.com/astronomer/astronomer-cosmos/issues/2959)). The
   trigger for retraining must not depend on that. The emitting task also compares the content
   fingerprint (`paths.file_fingerprint`) against the previous event's `extra` and emits only on a
   real change, so a rebuild that reproduces the same bytes does not start a pointless retrain.
5. **The gate is §10's pre-registered rule, made precise, and it distinguishes *hold* from
   *regress*.** Eligibility: OOF top-1 may not fall more than 0.1pp below the champion's, checked
   for both objectives. Ranking, on `rank:map` (the reported default): gold top-1 with a ±2-item
   equivalence band, then the number of *wrong rows* in the gold score band with the same ±2
   band (§10 called 4, 4 and 3 wrong rows "tied"; comparing counts rather than percentages keeps
   that verdict while the band's size moves between 164 and 183 rows), then gold Brier, then the
   champion keeps its place. The champion is re-scored on the *current* gold set every time,
   because labels are added and corrected. A challenger that is merely not better is **held**:
   registered under a `challenger` alias, run green. Only a failed eligibility check or a gold
   top-1 more than two items worse fails the task — a red run then means something.
6. **SimpleAuthManager, not FAB.** The FAB provider is not in the lock, and Airflow's own
   `docker-compose.yaml` pulls it in only for its Celery setup. SimpleAuthManager is documented
   as intended for development and testing, which is exactly what a laptop stack is; one admin
   user, with the password seeded from `terraform.tfvars`.

---

## 6. Open questions

1. ~~**Lockfile tool** — pip-tools or `uv`.~~ Settled in milestone 13: uv, for the
   multiple-interpreter reason in §5.1.
2. ~~**Whether the retrain waits on the parity report.**~~ Answered by milestone 14: no column
   differs for a reason that is a bug. Every delta traces to one of the four deviations or
   order-dependencies named in §4.3 and §5.2, and `docs/findings.md` §9 gives each a cause. The
   retrain does not have to wait — though §9 also records one *pre-existing* pandas-side bug that
   milestone 15's retrain is the right moment to fix, since fixing it changes the features.
3. **Whether the gold labeling kit moves from HTML scraping to the checker's JSON API** (§4.6).
   Cheap, and removes a markup contract both repos currently defend — but it touches the workflow
   that produced milestones 7–9's numbers, so it is not free.
4. ~~**What `score_ambiguous` should do with its output** until the write-back direction in §2.4
   is settled.~~ Settled when milestone 16 began: a local parquet only. The QuickStatements file
   overlapped with milestone 10 and moved there.
