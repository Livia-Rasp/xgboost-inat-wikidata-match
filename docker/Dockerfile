# The pipeline image: every stage of spec §7 milestones 1-11, runnable with no venv, no Node and
# nothing installed on the host. See docs/platform-design.md §5.1.
#
# Python 3.14 matches the interpreter the committed numbers in README.md were produced under, so
# `make gold` in this image is a like-for-like reproduction rather than a comparison with an
# asterisk on it.

ARG PYTHON_VERSION=3.14

# ---------------------------------------------------------------------------------------------
# Builder — resolve and install into a self-contained venv at a fixed path.
# ---------------------------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.12.6 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /opt/project

# Dependencies first, in their own layer: they change when uv.lock does, which is far less often
# than the source. --no-install-project is what keeps the two separable.
#
# `check` gets pytest and ruff so the image can verify itself. dbt and mlflow are deliberately
# absent: they are declared in pyproject.toml and pinned in uv.lock so the resolution is proven
# (docker/Dockerfile.airflow is where that proof is exercised), but milestones 14 and 15 are what
# actually import them, and carrying ~1.5 GB of unused dependencies until then would make every
# build and pull slower for nothing. Each milestone adds its own extra when it needs it.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-project --extra check

# hatchling reads `readme` and `license` from [project], so the build fails without both files
# present — they are not optional extras here.
COPY README.md LICENSE ./
COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --extra check

# ---------------------------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime

# libgomp is XGBoost's OpenMP runtime; the slim base does not carry it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 make \
    && rm -rf /var/lib/apt/lists/*

# uid 1000, matching the sibling wikidata-inat-checker image's convention.
RUN useradd --create-home --uid 1000 matcher

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    # The project pins every seed it can (spec §5, features.RANDOM_STATE, labels.RANDOM_SEED) but
    # never pinned these two. PYTHONHASHSEED affects set/dict iteration order; XGBoost's `hist`
    # method varies with the thread count, so leaving it to the host's core count would make the
    # image's results depend on the machine it runs on.
    PYTHONHASHSEED=0 \
    OMP_NUM_THREADS=4 \
    # Neither os.cpu_count() nor os.process_cpu_count() can see a --cpus quota. See paths.py.
    MATCHER_WORKERS=4 \
    MATCHER_TAXA_DB=/opt/inat-cache/taxa.db

# Owned by matcher, not just populated by it: the pipeline writes data/ here, and the tooling
# wants somewhere for .ruff_cache / .pytest_cache. WORKDIR alone creates a root-owned directory.
RUN mkdir -p /opt/project /opt/inat-cache && chown matcher:matcher /opt/project
WORKDIR /opt/project

COPY --from=builder /opt/venv /opt/venv

# Runtime data, not just source. src/fixtures.py falls back to tests/fixtures/ whenever data/ has
# nothing to offer, gold/hard_cases.csv has no fallback at all (evaluate.py exits without it),
# and data/models/ holds the frozen models the gold path scores with. A built wheel contains
# none of these — [tool.hatch.build.targets.wheel] packages = ["src"] — which is why this is an
# editable install over a source copy rather than `pip install .`.
COPY --chown=matcher:matcher pyproject.toml uv.lock README.md LICENSE Makefile ./
COPY --chown=matcher:matcher src/ ./src/
COPY --chown=matcher:matcher tests/ ./tests/
COPY --chown=matcher:matcher gold/hard_cases.csv ./gold/
COPY --chown=matcher:matcher data/models/ ./data/models/
COPY --chown=matcher:matcher build_figures.py build_fixtures.py build_gold_set.py build_gold_labeling_kit.py ./

USER matcher

# PYTHON is what the Makefile shells out to; in the image the venv is already on PATH.
ENV PYTHON=python

CMD ["make", "gold"]
