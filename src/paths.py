"""Every filesystem location this project reads or writes, and the two staleness helpers that
go with them.

Before this module, thirteen constants across src/ each independently recomputed
``Path(__file__).resolve().parent.parent / "data"``, and two more pointed outside the repo
entirely via ``Path.home()``. That is fine on one laptop and impossible in a container, where
``data/`` is a mounted volume and the sibling checker's taxa index is a read-only bind mount at
whatever path the operator chose.

Every location below takes its default from the layout a plain checkout already has, so nothing
changes for an existing working copy, and every one can be redirected by an environment variable.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _env_path(name: str, default: Path) -> Path:
    """A path from the environment, or the checkout-relative default."""
    raw = os.environ.get(name)
    return Path(raw).expanduser() if raw else default


# Everything this project generates. Gitignored except models/ (spec §0).
DATA_DIR = _env_path("MATCHER_DATA_DIR", REPO_ROOT / "data")

# Overridable independently of DATA_DIR, which it otherwise lives inside. The frozen milestone
# 6/7 models are the one thing in data/ that is committed, so they are baked into the image —
# and mounting a volume over data/ would hide them. Docker seeds an empty *named* volume from
# the image's content at that path, so the default survives compose; this exists for the cases
# that do not, such as a bind mount or a volume that already has data in it.
MODEL_DIR = _env_path("MATCHER_MODEL_DIR", DATA_DIR / "models")

# The sibling Node project's iNat taxa index. Read-only, and never built here — see README's
# "The full path". In a container this is a read-only bind mount.
TAXA_DB_PATH = _env_path(
    "MATCHER_TAXA_DB", Path.home() / ".cache" / "wikidata-inat-checker" / "taxa.db"
)

# The sibling repo itself, for build_gold_labeling_kit.py's HTML source.
SIBLING_REPO = _env_path("MATCHER_SIBLING_REPO", Path.home() / "repos" / "wikidata-inat-checker")

# Committed, so these follow the source rather than the data volume.
GOLD_DIR = REPO_ROOT / "gold"
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures"
IMG_DIR = REPO_ROOT / "docs" / "img"

# Hashing the whole of a 493 MB lookup.sqlite on every cache check costs seconds for no benefit,
# so files above this size are fingerprinted from their size plus their head and tail instead.
# That is sound for both files it applies to: SQLite keeps a change counter in its first 100
# bytes, which moves on every write, and parquet keeps its metadata footer at the end.
_FULL_HASH_MAX_BYTES = 64 * 1024 * 1024
_SAMPLE_BYTES = 64 * 1024


def file_fingerprint(path: Path) -> str | None:
    """Content fingerprint of a file, or None if it does not exist.

    Used instead of st_mtime for cache manifests. Mtimes do not survive a container image layer,
    a volume restore or a fresh checkout, so an mtime-keyed manifest either rebuilds a cache that
    was perfectly good or — worse — matches one that is not. sha256 for the same reason
    wikidata._qid_set_fingerprint() uses it: hash() is salted per process by PYTHONHASHSEED and
    would never match across runs.
    """
    if not path.exists():
        return None
    size = path.stat().st_size
    digest = hashlib.sha256(str(size).encode())
    with path.open("rb") as fh:
        if size <= _FULL_HASH_MAX_BYTES:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
            return f"sha256:{digest.hexdigest()}"
        digest.update(fh.read(_SAMPLE_BYTES))
        fh.seek(max(0, size - _SAMPLE_BYTES))
        digest.update(fh.read(_SAMPLE_BYTES))
    return f"sha256-sampled:{digest.hexdigest()}"


def worker_count(requested: int | None = None, cap: int = 16) -> int:
    """How many processes to fan candidate generation out across.

    MATCHER_WORKERS is the only thing that reliably works under a container CPU limit:
    `docker run --cpus=2` sets a CFS quota, which neither os.cpu_count() nor
    os.process_cpu_count() can see — both report the host's cores and the pool oversubscribes.
    """
    if requested:
        return requested
    env = os.environ.get("MATCHER_WORKERS")
    if env:
        return max(1, int(env))
    # process_cpu_count() respects CPU affinity (taskset, cpuset); cpu_count() does not.
    detect = getattr(os, "process_cpu_count", None) or os.cpu_count
    return min(detect() or 4, cap)
