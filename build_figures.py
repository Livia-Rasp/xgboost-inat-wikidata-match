"""Regenerate the README's figures from the cached artifacts in data/.

Lives at the repo root alongside build_gold_set.py and build_gold_labeling_kit.py — one-off
tooling that drives the src/ pipeline, not part of the pipeline itself.

Three figures, each written twice (light and dark), because the README embeds them through a
<picture> element and GitHub serves whichever matches the reader's theme:

  calibration-*.png       the reliability diagram behind the overconfidence finding
  shap-summary-*.png      which features the model actually leans on
  threshold-bands-*.png   how the candidate rows split into reject / review / auto-accept

Deterministic by construction: the SHAP sample is drawn with a fixed seed and matplotlib's
timestamp metadata is suppressed, so rerunning produces byte-identical files. That is the whole
point of having a script rather than screenshotting notebook cells — a stale figure cannot hide.

    .venv/bin/python build_figures.py
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.colors import LinearSegmentedColormap  # noqa: E402

from src.paths import IMG_DIR, REPO_ROOT  # noqa: E402
from src.tracking import resolve_model  # noqa: E402
from src.train import (  # noqa: E402
    DEFAULT_OOF_PATH,
    FEATURE_COLUMNS,
    _prepare_X,
    find_auto_accept_threshold,
    find_reject_threshold,
    precision_at_threshold_table,
    reliability_diagram_data,
)

SHAP_SAMPLE_SIZE = 5000
SHAP_SAMPLE_SEED = 0
DPI = 160

# Validated with the dataviz skill's palette validator (all-pairs, both modes): worst CVD
# separation 9.2 light / 9.4 dark, worst normal-vision separation 24.0 / 20.9. Aqua sits below
# 3:1 on the light surface, so every chart using it carries visible direct labels.
THEMES = {
    "light": {
        "surface": "#fcfcfb",
        "ink": "#0b0b0b",
        "secondary": "#52514e",
        "muted": "#898781",
        "grid": "#e1e0d9",
        "axis": "#c3c2b7",
        "series": ("#2a78d6", "#eb6834", "#1baf7a"),
        "neutral_fill": "#d8d7d0",
    },
    "dark": {
        "surface": "#1a1a19",
        "ink": "#ffffff",
        "secondary": "#c3c2b7",
        "muted": "#898781",
        "grid": "#2c2c2a",
        "axis": "#383835",
        "series": ("#3987e5", "#d95926", "#199e70"),
        "neutral_fill": "#45443f",
    },
}


def _style(theme: dict) -> None:
    plt.rcParams.update({
        "figure.facecolor": theme["surface"],
        "axes.facecolor": theme["surface"],
        "savefig.facecolor": theme["surface"],
        "text.color": theme["ink"],
        "axes.labelcolor": theme["secondary"],
        "axes.edgecolor": theme["axis"],
        "xtick.color": theme["muted"],
        "ytick.color": theme["muted"],
        "grid.color": theme["grid"],
        "font.family": "sans-serif",
        "font.size": 10,
        "axes.titlesize": 11,
        "axes.titleweight": "medium",
    })


def _save(fig, name: str, mode: str) -> Path:
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    path = IMG_DIR / f"{name}-{mode}.png"
    # Software/date metadata would otherwise change on every run and make each regeneration look
    # like a real diff.
    fig.savefig(path, dpi=DPI, bbox_inches="tight", metadata={"Software": None})
    plt.close(fig)
    return path


def _recessive_axes(ax, theme: dict) -> None:
    ax.grid(True, linewidth=0.6, alpha=0.9)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_linewidth(0.8)


def calibration_figure(oof: pd.DataFrame, mode: str) -> Path:
    """Reliability diagram: mean predicted probability against the observed positive rate.

    Perfect calibration is the diagonal. The raw curve sags far below it at the top of the
    range, which is the finding — the model's confidence outruns its accuracy — and isotonic
    regression pulls it back onto the line."""
    theme = THEMES[mode]
    _style(theme)
    labels = oof["label"].to_numpy()
    raw = reliability_diagram_data(oof["binary_raw_score"].to_numpy(), labels)
    cal = reliability_diagram_data(oof["binary_calibrated_prob"].to_numpy(), labels)

    fig, ax = plt.subplots(figsize=(6.2, 4.6))
    ax.plot([0, 1], [0, 1], linestyle=(0, (4, 4)), linewidth=1.2, color=theme["axis"], zorder=1)
    ax.annotate("perfect calibration", (0.62, 0.62), rotation=38, ha="center", va="bottom",
                fontsize=8.5, color=theme["muted"], rotation_mode="anchor")

    for data, color, label in [
        (raw, theme["series"][0], "raw model score"),
        (cal, theme["series"][1], "after isotonic calibration"),
    ]:
        ax.plot(data["mean_predicted"], data["mean_observed"], marker="o", markersize=5.5,
                linewidth=2, color=color, label=label, zorder=3,
                markeredgecolor=theme["surface"], markeredgewidth=1.2)

    # Annotate the exact [0.95, 1.0) band the write-up quotes, not the last decile bin — the two
    # are close but not the same number, and a figure that disagrees with the prose by 0.3pp is
    # worse than no figure.
    band = oof["binary_raw_score"] >= 0.95
    top = raw.iloc[-1]
    ax.annotate(
        f"{int(band.sum()):,} rows score ≥0.95\nbut only {labels[band].mean():.1%} are correct",
        xy=(top["mean_predicted"], top["mean_observed"]),
        xytext=(0.53, 0.28), fontsize=9, color=theme["ink"],
        arrowprops=dict(arrowstyle="-", linewidth=1, color=theme["muted"], shrinkA=2, shrinkB=6),
    )

    ax.set_xlabel("mean predicted probability")
    ax.set_ylabel("observed fraction correct")
    ax.set_title("The model is overconfident before calibration", loc="left", color=theme["ink"])
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    _recessive_axes(ax, theme)
    legend = ax.legend(loc="upper left", frameon=False, fontsize=9)
    for text in legend.get_texts():
        text.set_color(theme["secondary"])
    return _save(fig, "calibration", mode)


def shap_figure(features: pd.DataFrame, mode: str) -> Path:
    """Beeswarm over a fixed 5,000-row sample: which features move the score, and in which
    direction. Colour here is the feature's own value (low → high), not series identity."""
    import shap

    theme = THEMES[mode]
    _style(theme)
    # The registry's champion when MLFLOW_TRACKING_URI is set, the committed file otherwise.
    # shap.TreeExplainer needs a real booster, which is why resolve_model() hands one back rather
    # than only a pyfunc — and it comes out of the same registered version as its calibrator, so
    # the figure cannot end up depicting a model the numbers did not come from.
    model = resolve_model("binary").booster

    sample = features.sample(SHAP_SAMPLE_SIZE, random_state=SHAP_SAMPLE_SEED)
    explainer = shap.TreeExplainer(model)
    shap_values = explainer(_prepare_X(sample))
    shap_values.feature_names = FEATURE_COLUMNS

    # Diverging blue↔red for "low value / high value", the palette's diverging pair, with its
    # neutral gray midpoint. Never a rainbow.
    cmap = LinearSegmentedColormap.from_list(
        "feature_value",
        ["#2a78d6", "#f0efec" if mode == "light" else "#383835", "#d03b3b"],
    )

    fig = plt.figure(figsize=(7.2, 5.6))
    # beeswarm shuffles tied points through numpy's *global* RNG to spread them, so without this
    # seed every rerun produces a visually identical but byte-different PNG — a permanent
    # spurious diff. Seeding here rather than at import keeps the scope obvious.
    np.random.seed(SHAP_SAMPLE_SEED)
    shap.plots.beeswarm(shap_values, show=False, max_display=12, color=cmap, color_bar=True)
    ax = plt.gca()
    ax.set_title("What the model actually leans on", loc="left", color=theme["ink"], pad=12)
    ax.set_xlabel("SHAP value (impact on the match score)", color=theme["secondary"])
    ax.tick_params(colors=theme["muted"])
    for label in ax.get_yticklabels():
        label.set_color(theme["secondary"])
    for spine in ax.spines.values():
        spine.set_color(theme["axis"])
    fig.set_facecolor(theme["surface"])
    ax.set_facecolor(theme["surface"])
    return _save(fig, "shap-summary", mode)


def _bands(probs: np.ndarray, labels: np.ndarray) -> dict:
    accept_row = find_auto_accept_threshold(precision_at_threshold_table(probs, labels))
    accept_threshold = float(accept_row["threshold"]) if accept_row is not None else None
    reject_threshold = find_reject_threshold(probs, labels)

    n = len(probs)
    accept = (probs >= accept_threshold) if accept_threshold is not None else np.zeros(n, bool)
    reject = (probs < reject_threshold) if reject_threshold is not None else np.zeros(n, bool)
    review = ~accept & ~reject
    return {
        "reject": (reject.sum() / n, float((labels[reject] == 0).mean()) if reject.any() else float("nan")),
        "review": (review.sum() / n, float("nan")),
        "accept": (accept.sum() / n, float(labels[accept].mean()) if accept.any() else float("nan")),
        "accept_n": int(accept.sum()),
        "reject_threshold": reject_threshold,
        "accept_threshold": accept_threshold,
    }


def threshold_bands_figure(oof: pd.DataFrame, mode: str) -> Path:
    """Where the 590,671 candidate rows land once both 99.5%-precision thresholds are applied.

    The auto-accept band is a hairline on purpose — that is the honest result, and drawing it to
    scale says more than a table would."""
    theme = THEMES[mode]
    _style(theme)
    labels = oof["label"].to_numpy()
    variants = [
        ("binary:logistic", _bands(oof["binary_calibrated_prob"].to_numpy(), labels)),
        ("rank:map", _bands(oof["rank_calibrated_prob"].to_numpy(), labels)),
    ]

    fig, ax = plt.subplots(figsize=(7.4, 2.4))
    segments = [
        ("reject", "confidently rejected", theme["series"][2]),
        ("review", "left for human review", theme["neutral_fill"]),
        ("accept", "auto-accepted", theme["series"][0]),
    ]
    gap = 0.002  # 2px surface gap between adjacent fills, per the mark spec

    for row, (_variant, bands) in enumerate(variants):
        left = 0.0
        for key, _, color in segments:
            width = bands[key][0]
            ax.barh(row, max(width - gap, 0), left=left, height=0.55, color=color, zorder=3)
            left += width
        ax.annotate(f"{bands['reject'][0]:.1%} rejected\n({bands['reject'][1]:.2%} truly negative)",
                    (0.012, row), va="center", ha="left", fontsize=9, color=theme["surface"], zorder=4)
        review_mid = bands["reject"][0] + bands["review"][0] / 2
        ax.annotate(f"{bands['review'][0]:.1%}\nreview", (review_mid, row), va="center", ha="center",
                    fontsize=8.5, color=theme["ink"], zorder=4)
        accept_label = (
            f"auto-accept: {bands['accept_n']:,} rows ({bands['accept'][0]:.4%})"
            if bands["accept_n"]
            else "auto-accept: no threshold reaches 99.5% precision"
        )
        ax.annotate(accept_label, xy=(1.0, row), xytext=(1.045, row), va="center", ha="left",
                    fontsize=8.5, color=theme["secondary"],
                    arrowprops=dict(arrowstyle="-", linewidth=1, color=theme["muted"], shrinkA=0, shrinkB=1))

    ax.set_yticks(range(len(variants)))
    ax.set_yticklabels([v for v, _ in variants], color=theme["secondary"])
    ax.set_ylim(len(variants) - 0.55, -0.55)
    ax.set_xlim(0, 1)
    ax.set_xticks([0, 0.25, 0.5, 0.75, 1.0])
    ax.set_xticklabels(["0%", "25%", "50%", "75%", "100%"])
    ax.set_xlabel("share of the 590,671 candidate rows")
    ax.set_title("Both 99.5%-precision bands, drawn to scale", loc="left", color=theme["ink"])
    ax.grid(True, axis="x", linewidth=0.6, alpha=0.9)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_linewidth(0.8)

    handles = [plt.Rectangle((0, 0), 1, 1, color=segment_color) for *_, segment_color in segments]
    legend = ax.legend(handles, [label for _, label, _ in segments], loc="upper left",
                       bbox_to_anchor=(0, -0.42), ncol=3, frameon=False, fontsize=9)
    for text in legend.get_texts():
        text.set_color(theme["secondary"])
    return _save(fig, "threshold-bands", mode)


def main() -> None:
    from src.features import DEFAULT_FEATURES_PATH

    if not DEFAULT_OOF_PATH.exists():
        raise SystemExit(f"{DEFAULT_OOF_PATH} not found — run `python -m src.train` first.")
    oof = pd.read_parquet(DEFAULT_OOF_PATH)
    features = pd.read_parquet(DEFAULT_FEATURES_PATH)

    for mode in ("light", "dark"):
        for path in (
            calibration_figure(oof, mode),
            shap_figure(features, mode),
            threshold_bands_figure(oof, mode),
        ):
            print(f"wrote {path.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
