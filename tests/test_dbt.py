"""The dbt transformation layer (spec §7 milestone 14).

`dbt build` against the real caches takes ~20 seconds and needs 190 MB of parquet plus a 493 MB
SQLite index, none of which CI has. So this builds the same project against a throwaway
MATCHER_DATA_DIR holding the hand-written fixture universe — the real lookup cache from
conftest's taxa_mini.csv, real candidates from candidates.py's own generator, and eight Wikidata
items arranged into exactly five family groups, which is what GroupKFold(5) needs.

The parity assertion is the point. Milestone 14 accepts four sources of drift and rules out
everything else (docs/findings.md §9); this pins the "everything else" at fixture scale, so a
change to either implementation that moves a column nobody expected to move fails here rather
than showing up as a surprise in the next parity report.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

from src.candidates import STRATEGY_TAGS, build_candidates_cache, build_lookup_cache
from src.features import _load_inat_index, build_features_and_splits

# The dbt extra is optional (`pip install -e ".[dev,dbt]"`, or `make sync`). CI installs it, so
# these run there; a checkout without it skips the module rather than erroring on import.
pytest.importorskip("dbt", reason="the dbt extra is not installed")

REPO_ROOT = Path(__file__).resolve().parent.parent
DBT_DIR = REPO_ROOT / "dbt"

WD_RANK_KINGDOM = "Q36732"
WD_RANK_ORDER = "Q36602"
WD_RANK_FAMILY = "Q35409"
WD_RANK_GENUS = "Q34740"
WD_RANK_SPECIES = "Q7432"
WD_RANK_VARIETY = "Q767728"


def _wikidata_taxa() -> pd.DataFrame:
    """Eight Wikidata items over the fixture index.

    Q_ROSA has no family or order in its chain, so build_family_keys falls back to its kingdom;
    Q_ORPHAN has no chain at all, so it falls back to itself. Together with the three real
    families that gives five distinct grouping keys, the minimum GroupKFold(N_SPLITS) accepts.
    """
    rows = [
        ("Q_BIRD", "13982", "Prunella", WD_RANK_GENUS, "Q_BIRDFAM", "Prunellidae"),
        ("Q_MINT", "52765", "Prunella", WD_RANK_GENUS, "Q_MINTFAM", "Lamiaceae"),
        ("Q_ACER", "203", "Acer rubrum", WD_RANK_SPECIES, "Q_ACERG", "Acer"),
        ("Q_ACERVAR", "208", "Acer rubrum var. tomentosum", WD_RANK_VARIETY, "Q_ACER", "Acer rubrum"),
        ("Q_SACC", "206", "Acer saccharum", WD_RANK_SPECIES, "Q_ACERG", "Acer"),
        ("Q_HYBRID", "209", "Acer × freemanii", WD_RANK_SPECIES, "Q_ACERG", "Acer"),
        ("Q_ROSA", "211", "Rosa canina", WD_RANK_SPECIES, "Q_ROSAG", "Rosa"),
        ("Q_ORPHAN", "205", "Acer rubrium", WD_RANK_SPECIES, "Q_ACERG", "Acer"),
    ]
    return pd.DataFrame(
        [
            {
                "qid": qid,
                "inat_id": inat_id,
                "name": name,
                "rank_qid": rank_qid,
                "parent_qid": parent_qid,
                "parent_name": parent_name,
                "iucn_qid": None,
                "sitelinks": 10,
                "statements": 20,
                "has_commons_cat": True,
                "p3151_has_reference": False,
                "synonym_names": [],
                "basionym_names": [],
            }
            for qid, inat_id, name, rank_qid, parent_qid, parent_name in rows
        ]
    )


def _ancestors() -> pd.DataFrame:
    chains = {
        "Q_BIRD": [
            ("Q_BIRDFAM", "Prunellidae", WD_RANK_FAMILY),
            ("Q_BIRDORD", "Passeriformes", WD_RANK_ORDER),
            ("Q_ANIMALIA", "Animalia", WD_RANK_KINGDOM),
        ],
        "Q_MINT": [
            ("Q_MINTFAM", "Lamiaceae", WD_RANK_FAMILY),
            ("Q_MINTORD", "Lamiales", WD_RANK_ORDER),
            ("Q_PLANTAE", "Plantae", WD_RANK_KINGDOM),
        ],
        "Q_ROSA": [("Q_PLANTAE", "Plantae", WD_RANK_KINGDOM)],
        "Q_ORPHAN": [],
    }
    sapindaceae = [
        ("Q_SAPFAM", "Sapindaceae", WD_RANK_FAMILY),
        ("Q_SAPORD", "Sapindales", WD_RANK_ORDER),
        ("Q_PLANTAE", "Plantae", WD_RANK_KINGDOM),
    ]
    for qid in ("Q_ACER", "Q_ACERVAR", "Q_SACC", "Q_HYBRID"):
        chains[qid] = sapindaceae

    return pd.DataFrame(
        [
            {
                "qid": qid,
                "ancestor_qid": ancestor_qid,
                "ancestor_name": ancestor_name,
                "ancestor_rank_qid": rank_qid,
            }
            for qid, chain in chains.items()
            for ancestor_qid, ancestor_name, rank_qid in chain
        ]
    )


@pytest.fixture(scope="module")
def mini_data_dir(tmp_path_factory, taxa_db) -> Path:
    """A throwaway MATCHER_DATA_DIR holding everything the dbt project sources."""
    data_dir = tmp_path_factory.mktemp("dbt_data")

    build_lookup_cache(taxa_db_path=taxa_db, cache_path=data_dir / "lookup.sqlite").close()

    wikidata_taxa = _wikidata_taxa()
    wikidata_taxa.to_parquet(data_dir / "wikidata_taxa.parquet", index=False)
    _ancestors().to_parquet(data_dir / "wikidata_ancestors.parquet", index=False)

    # The real generator, not a hand-written candidate table: `strategies` is what the ten
    # strategy_* columns are derived from, and inventing those strings would test nothing.
    build_candidates_cache(
        wikidata_taxa,
        candidates_path=data_dir / "candidates.parquet",
        manifest_path=data_dir / "candidates.manifest.json",
        lookup_sqlite_path=data_dir / "lookup.sqlite",
        processes=1,
    )
    return data_dir


@pytest.fixture(scope="module")
def dbt_run(mini_data_dir) -> subprocess.CompletedProcess:
    """`dbt build` over the fixture universe, in its own target directory.

    --target-path keeps this out of dbt/target/, so running the suite never clobbers the
    artefacts of a real `make features-sql`.
    """
    env = {
        **os.environ,
        "MATCHER_DATA_DIR": str(mini_data_dir),
        # dbt's usage ping is a network call, and this suite makes none.
        "DO_NOT_TRACK": "1",
    }
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "dbt.cli.main",
            "build",
            "--project-dir",
            str(DBT_DIR),
            "--profiles-dir",
            str(DBT_DIR),
            "--target-path",
            str(mini_data_dir / "target"),
            "--no-partial-parse",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


@pytest.fixture(scope="module")
def dbt_features(dbt_run, mini_data_dir) -> pd.DataFrame:
    assert dbt_run.returncode == 0, dbt_run.stdout + dbt_run.stderr
    return pd.read_parquet(mini_data_dir / "features_dbt.parquet")


@pytest.fixture(scope="module")
def pandas_features(mini_data_dir, tmp_path_factory) -> pd.DataFrame:
    out = tmp_path_factory.mktemp("pandas_features")
    return build_features_and_splits(
        candidates=pd.read_parquet(mini_data_dir / "candidates.parquet"),
        wikidata_taxa=_wikidata_taxa(),
        ancestors=_ancestors(),
        inat_index=_load_inat_index(mini_data_dir / "lookup.sqlite"),
        features_path=out / "features.parquet",
        manifest_path=out / "features.manifest.json",
    )


def _aligned(dbt_features: pd.DataFrame, pandas_features: pd.DataFrame):
    key = ["wikidata_qid", "inat_taxon_id"]
    a = pandas_features.sort_values(key).reset_index(drop=True)
    b = dbt_features.sort_values(key).reset_index(drop=True)
    return a, b


def test_dbt_build_is_green(dbt_run):
    assert dbt_run.returncode == 0, dbt_run.stdout + dbt_run.stderr
    assert "ERROR=0" in dbt_run.stdout


def test_the_two_implementations_produce_the_same_rows(dbt_features, pandas_features):
    a, b = _aligned(dbt_features, pandas_features)
    assert len(a) == len(b)
    assert list(a.columns) == list(b.columns)
    assert a[["wikidata_qid", "inat_taxon_id"]].equals(b[["wikidata_qid", "inat_taxon_id"]])


# sim_rank_in_group is excluded deliberately: pandas breaks ties on candidates.parquet row order
# (`.rank(method="first")`) and SQL cannot, so the two disagree *inside* a group of equally
# similar candidates by design. Its own invariant is checked separately below.
DRIFTING_COLUMNS = {"sim_rank_in_group"}


def test_every_other_column_agrees(dbt_features, pandas_features):
    a, b = _aligned(dbt_features, pandas_features)
    mismatched = {}
    for column in a.columns:
        if column in DRIFTING_COLUMNS:
            continue
        left, right = a[column], b[column]
        if left.dtype.kind == "f":
            # 1 ULP: DuckDB's jaro_winkler and rapidfuzz's agree to float rounding on ASCII.
            differs = ((left - right).abs() > 1e-9) & ~(left.isna() & right.isna())
        else:
            differs = left.astype(object) != right.astype(object)
        if differs.any():
            mismatched[column] = int(differs.sum())
    assert not mismatched, f"columns disagree between the pandas and dbt paths: {mismatched}"


def test_sim_rank_in_group_orders_by_similarity(dbt_features):
    """The tie-break rule changed; the ordering it produces still has to be a ranking."""
    for _, group in dbt_features.groupby("wikidata_qid"):
        ordered = group.sort_values("sim_rank_in_group")
        assert list(ordered["sim_rank_in_group"]) == list(range(1, len(group) + 1))
        assert ordered["similarity"].is_monotonic_decreasing


def test_no_qid_appears_in_two_folds(dbt_features):
    """Milestone 4's leakage guarantee, on the SQL path. dbt/tests asserts it at full scale too."""
    assert (dbt_features.groupby("wikidata_qid")["fold"].nunique() == 1).all()


def test_strategy_tags_var_matches_the_python_tuple():
    """dbt_project.yml carries a copy of STRATEGY_TAGS because SQL cannot import a tuple.

    The copy decides which ten strategy_* columns exist and in what order, so a tag added on the
    Python side and forgotten here would silently drop a feature rather than fail.
    """
    import yaml

    project = yaml.safe_load((DBT_DIR / "dbt_project.yml").read_text())
    assert tuple(project["vars"]["strategy_tags"]) == STRATEGY_TAGS


def test_the_dbt_project_directory_is_copied_into_the_image():
    """docker/Dockerfile has to carry dbt/ or `make features-sql` cannot run in the container."""
    dockerfile = (REPO_ROOT / "docker" / "Dockerfile").read_text()
    assert "dbt/ ./dbt/" in dockerfile


def test_fixture_universe_has_enough_groups_for_groupkfold(pandas_features):
    """A guard on the fixture itself: GroupKFold(5) needs five family groups, and losing one to
    an edit here would fail as an opaque sklearn error rather than as this."""
    assert pandas_features["family_key"].nunique() >= 5


def test_lookup_sqlite_is_attached_read_only():
    """The index is the sibling project's output; nothing here may write to it."""
    profiles = (DBT_DIR / "profiles.yml").read_text()
    assert "read_only: true" in profiles


