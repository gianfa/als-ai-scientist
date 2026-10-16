"""Diagnostic figures drawn from persisted run scorecards.

Read-only, deterministic, zero cost: the only input is `run-score*.json` as written by
`evaluation/run_score.py` for one or more archived runs. Nothing here recomputes a measurement, so a
figure can never disagree with the scorecard it was drawn from, and a field a run does not carry is
drawn as absent rather than as zero.

Matplotlib is an optional dependency (`[project.optional-dependencies] figures`); importing this
module without it fails loudly with the install hint instead of degrading.
"""

from __future__ import annotations

import json
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:
    import matplotlib
except ModuleNotFoundError as exc:  # pragma: no cover - exercised only without the extra installed
    raise ModuleNotFoundError("evaluation.figures needs matplotlib: `uv sync --extra figures`") from exc

matplotlib.use("Agg")  # no display in a run container; must precede the pyplot import
import matplotlib.pyplot as plt  # noqa: E402  (backend must be selected first)
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import FancyBboxPatch  # noqa: E402

# --- Design tokens -------------------------------------------------------------------------------
# Categorical slots 1-3 of the reference palette, in fixed order, never cycled: a run keeps its hue
# whatever else is on the figure. The three-slot subset is the one validated on all pairs, so a
# fourth run must fold into a facet rather than take a generated fourth hue.
SERIES = ("#2a78d6", "#eb6834", "#1baf7a")
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
INK_MUTED = "#73726d"
GRID = "#e6e5e1"
REFERENCE = "#d03b3b"  # status-critical: a pre-registered threshold, always labelled in words too

MAX_SERIES = len(SERIES)


class FigureDataMissing(Exception):
    """A requested figure has no data in any given scorecard."""


@dataclass(frozen=True)
class Scorecard:
    """One run's persisted scorecard."""

    run_id: str
    data: dict[str, Any]

    @classmethod
    def load(cls, path: Path) -> "Scorecard":
        payload = json.loads(path.read_text())
        run_id = payload.get("run_id")
        if not run_id:
            raise ValueError(f"{path}: scorecard carries no run_id")
        return cls(run_id=run_id, data=payload)

    def get(self, *keys: str) -> Any:
        """Walk a key path, returning None at the first missing or non-dict level.

        None means "this run does not record it" and is drawn as absent; it is never coerced to 0.
        """
        node: Any = self.data
        for key in keys:
            if not isinstance(node, dict) or key not in node:
                return None
            node = node[key]
        return node


# --- Shared chart furniture ----------------------------------------------------------------------


def _style(ax: plt.Axes, *, title: str, subtitle: str, xlabel: str, vertical_grid: bool = True) -> None:
    # The title block reserves the strip above the plot; the legend sits in it, so no legend can ever
    # land on a mark or on a value label.
    ax.set_facecolor(SURFACE)
    # Title, subtitle and legend are stacked above the axes in POINTS, not in axes fractions: the
    # figures differ in height, and a fraction that clears the title on a tall one overlaps it on a
    # short one. Measured from the axes top: legend 0-14pt, subtitle 24pt, title 42pt.
    ax.set_title(title, color=INK, fontsize=12, fontweight="semibold", loc="left", pad=42)
    ax.annotate(
        subtitle,
        xy=(0, 1),
        xycoords="axes fraction",
        xytext=(0, 24),
        textcoords="offset points",
        color=INK_SECONDARY,
        fontsize=8.5,
        va="bottom",
        ha="left",
    )
    ax.set_xlabel(xlabel, color=INK_SECONDARY, fontsize=9)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=INK_SECONDARY, labelsize=9, length=0)
    if vertical_grid:
        ax.xaxis.grid(True, color=GRID, linewidth=0.8)
        ax.set_axisbelow(True)
    ax.yaxis.grid(False)


def _legend(ax: plt.Axes, labels: Sequence[str], colors: Sequence[str]) -> None:
    """A legend is present for two or more series and absent for one: the title names a lone series."""
    if len(labels) < 2:
        return
    handles = [plt.Rectangle((0, 0), 1, 1, facecolor=c, edgecolor=SURFACE, linewidth=1.5) for c in colors]
    legend = ax.legend(
        handles,
        labels,
        loc="lower left",
        bbox_to_anchor=(0, 1.0),
        bbox_transform=ax.transAxes,
        ncol=len(labels),
        frameon=False,
        fontsize=9,
        handlelength=0.9,
        handleheight=0.9,
        borderpad=0.0,
        borderaxespad=0.3,
        columnspacing=1.4,
    )
    for text in legend.get_texts():
        text.set_color(INK_SECONDARY)


def _rate_axis(ax: plt.Axes, *, label_room: float) -> None:
    """0-1 ticks, plus an unticked right margin wide enough for the value labels."""
    ax.set_xlim(0, 1.0 + label_room)
    ax.set_xticks([0.0, 0.2, 0.4, 0.6, 0.8, 1.0])


def _category_rows(ax: plt.Axes, count: int) -> None:
    """Fix the y range to every category, drawn or not, top to bottom.

    Autoscaling follows the bars only, so a category whose runs are all absent - drawn as a text note,
    not a bar - falls outside the axes and its note lands on the x axis or below the figure.
    """
    ax.set_ylim(count - 0.5, -0.5)


def _save(fig: plt.Figure, out_dir: Path, name: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / name
    fig.savefig(path, dpi=200, facecolor=SURFACE, bbox_inches="tight")
    plt.close(fig)
    return path


def _series_colors(cards: Sequence[Scorecard]) -> list[str]:
    if len(cards) > MAX_SERIES:
        raise ValueError(f"at most {MAX_SERIES} runs per figure; facet instead of adding a hue")
    return list(SERIES[: len(cards)])


def _offsets(count: int, span: float = 0.78) -> list[float]:
    """Centre `count` grouped bars on a category, leaving a surface gap between adjacent fills."""
    height = span / count
    return [(-span / 2) + height * (i + 0.5) for i in range(count)]


def _bar_height(count: int, span: float = 0.78) -> float:
    return (span / count) * 0.88  # the 12% remainder is the 2px-equivalent surface gap


# --- Figure 1: cost by phase ---------------------------------------------------------------------


def cost_by_phase(cards: Sequence[Scorecard], out_dir: Path, *, cap_usd: float | None = None) -> Path:
    """Grouped bars: what each phase of each run cost, in USD, on one shared axis."""
    stages: list[str] = []
    for card in cards:
        for stage in card.get("cost", "stages") or {}:
            if stage not in stages:
                stages.append(stage)
    if not stages:
        raise FigureDataMissing("no cost stages in any scorecard")
    stages.sort()

    colors = _series_colors(cards)
    offsets = _offsets(len(cards))
    height = _bar_height(len(cards))
    fig, ax = plt.subplots(figsize=(8.6, 0.34 * len(stages) * len(cards) + 2.0), facecolor=SURFACE)

    widest = 0.0
    for card, color, offset in zip(cards, colors, offsets):
        values = []
        for index, stage in enumerate(stages):
            usd = (card.get("cost", "stages", stage) or {}).get("usd")
            values.append((index + offset, 0.0 if usd is None else float(usd), usd is not None))
        ax.barh(
            [y for y, _, _ in values],
            [v for _, v, _ in values],
            height=height,
            color=color,
            edgecolor=SURFACE,
            linewidth=1.2,
        )
        for y, value, present in values:
            widest = max(widest, value)
            label = f"{value:.2f}" if present else "not run"
            ax.text(value + 0.06, y, label, va="center", ha="left", fontsize=8, color=INK if present else INK_MUTED)

    ax.set_yticks(range(len(stages)))
    ax.set_yticklabels(stages, color=INK_SECONDARY, fontsize=9)
    ax.invert_yaxis()
    ax.set_xlim(0, widest * 1.26)
    totals = ", ".join(f"{c.run_id} {c.get('cost', 'total_usd')}" for c in cards)
    subtitle = f"run totals in USD: {totals}"
    if cap_usd is not None:
        subtitle += f" · cap {cap_usd:g}"
    _style(ax, title="Cost per phase", subtitle=subtitle, xlabel="USD")
    _legend(ax, [c.run_id for c in cards], colors)
    return _save(fig, out_dir, "fig1-cost-by-phase.png")


# --- Figure 2: verify verdict composition --------------------------------------------------------

VERDICTS = ("confirmed", "rejected", "insufficient")


# A segment holds its own count when it spans at least this share of the axis: about 0.2 in on the
# figure's width, room for three digits at 9 pt.
INLINE_LABEL_MIN_SPAN = 0.025


def _segment_labels(counts: Mapping[str, Any], span: float) -> tuple[list[str], list[str]]:
    """Split the non-zero verdicts into those labelled inside their segment and those named at the bar's end.

    Every non-zero verdict lands in exactly one of the two, so no count is ever left unlabelled. The
    test is the segment's width on the shared axis, not its share of its own bar: a short bar's small
    segment and a long bar's small segment are the same width on the page.
    """
    inline: list[str] = []
    overflow: list[str] = []
    for verdict in VERDICTS:
        value = int(counts.get(verdict, 0))
        if value == 0:
            continue
        (inline if span and value / span >= INLINE_LABEL_MIN_SPAN else overflow).append(verdict)
    return inline, overflow


def verify_verdicts(cards: Sequence[Scorecard], out_dir: Path) -> Path:
    """One stacked bar per run: how the verifier disposed of the relations it was offered.

    The three verdicts partition the pool, so a stack is the honest form; each segment carries its
    own count, a segment too narrow to hold it has the count named after the pool size at the bar's
    end, and the bar is labelled with the pool size.
    """
    rows = [(card, card.get("verify", "counts") or {}) for card in cards]
    rows = [(card, counts) for card, counts in rows if counts]
    if not rows:
        raise FigureDataMissing("no verify counts in any scorecard")

    # Sequential blue, dark to light: an ordered disposition, not three identities. The lightest step
    # clears the 2:1 ordinal floor against the surface.
    ramp = ("#184f95", "#2a78d6", "#86b6ef")
    fig, ax = plt.subplots(figsize=(9.4, 0.62 * len(rows) + 2.2), facecolor=SURFACE)
    total_widest = max(sum(int(counts.get(v, 0)) for v in VERDICTS) for _, counts in rows)
    span = total_widest * 1.22
    for index, (card, counts) in enumerate(rows):
        left = 0.0
        total = sum(int(counts.get(v, 0)) for v in VERDICTS)
        inline, overflow = _segment_labels(counts, span)
        for verdict, color in zip(VERDICTS, ramp):
            value = int(counts.get(verdict, 0))
            if value == 0:
                continue
            ax.barh(index, value, left=left, height=0.52, color=color, edgecolor=SURFACE, linewidth=1.6)
            if verdict in inline:
                ax.text(
                    left + value / 2,
                    index,
                    f"{value}",
                    va="center",
                    ha="center",
                    fontsize=9,
                    color="#ffffff" if color != ramp[2] else INK,
                )
            left += value
        end_label = f"{total} offered"
        if overflow:
            end_label += " · " + ", ".join(f"{v} {counts[v]}" for v in overflow)
        ax.text(
            left + total_widest * 0.015,
            index,
            end_label,
            va="center",
            ha="left",
            fontsize=8.5,
            color=INK_SECONDARY,
        )

    ax.set_yticks(range(len(rows)))
    ax.set_yticklabels([card.run_id for card, _ in rows], color=INK_SECONDARY, fontsize=9)
    ax.invert_yaxis()
    ax.set_xlim(0, span)
    _style(
        ax,
        title="What the verifier did with each relation",
        subtitle="counts, not rates; the three verdicts partition the candidate pool",
        xlabel="relations",
    )
    _legend(ax, list(VERDICTS), list(ramp))
    return _save(fig, out_dir, "fig2-verify-verdicts.png")


# --- Figure 3: quote-check exact rate ------------------------------------------------------------


def quote_exact_rate(cards: Sequence[Scorecard], out_dir: Path, *, threshold: float | None = None) -> Path:
    """Grouped bars: the share of judged `check_quote` calls that came back `exact`, per phase.

    The denominator is judged calls, not all calls: an `unverifiable` verdict is the checker
    declining, not the quote failing, so it belongs outside the rate and is named in the label.
    """
    phases: list[str] = []
    for card in cards:
        for phase in card.get("quote_checks") or {}:
            if phase not in phases:
                phases.append(phase)
    if not phases:
        raise FigureDataMissing("no quote checks in any scorecard")

    colors = _series_colors(cards)
    offsets = _offsets(len(cards))
    height = _bar_height(len(cards))
    fig, ax = plt.subplots(figsize=(9.8, 0.72 * len(phases) * len(cards) + 2.2), facecolor=SURFACE)

    for card, color, offset in zip(cards, colors, offsets):
        for index, phase in enumerate(phases):
            block = card.get("quote_checks", phase)
            y = index + offset
            if not block:
                ax.text(0.01, y, "phase absent from this run", va="center", ha="left", fontsize=8, color=INK_MUTED)
                continue
            rate = float(block.get("exact_rate_of_judged") or 0.0)
            ax.barh(y, rate, height=height, color=color, edgecolor=SURFACE, linewidth=1.2)
            ax.text(
                rate + 0.012,
                y,
                f"{rate:.2f}  ({block.get('exact')}/{block.get('judged')} judged" f" of {block.get('calls')} calls)",
                va="center",
                ha="left",
                fontsize=8,
                color=INK,
            )

    if threshold is not None:
        ax.axvline(threshold, color=REFERENCE, linewidth=1.4, linestyle=(0, (4, 3)))
        ax.annotate(
            f"pre-registered floor {threshold:g}",
            xy=(threshold, 1),
            xycoords=("data", "axes fraction"),
            xytext=(4, -6),
            textcoords="offset points",
            color=REFERENCE,
            fontsize=8.5,
            va="top",
            ha="left",
        )

    ax.set_yticks(range(len(phases)))
    ax.set_yticklabels(phases, color=INK_SECONDARY, fontsize=9)
    _category_rows(ax, len(phases))
    _rate_axis(ax, label_room=0.95)
    _style(
        ax,
        title="Quoted claims that match their source",
        subtitle="`exact` share of judged check_quote calls, by phase",
        xlabel="share of judged calls",
    )
    _legend(ax, [c.run_id for c in cards], colors)
    return _save(fig, out_dir, "fig3-quote-exact-rate.png")


# --- Figure 4: evidence reach --------------------------------------------------------------------

REACH_RATIOS = (
    (
        "Reuse",
        "Reuse of fresh\nliterature claims",
        ("evidence_reach", "touched_by_authors"),
        ("evidence_reach", "fresh_claims"),
    ),
    (
        "Reliance",
        "Reliance on fresh\nliterature claims",
        ("evidence_reach", "touched_by_authors"),
        ("citations", "claims_cited"),
    ),
)
"""Two ratios whose numerator is a subset of their denominator.

`touched_by_authors` are the claims this run's `literature` phase wrote that a `generate` relation links;
`claims_cited` are every claim a `generate` relation links that `generate` did not write itself, whatever
its origin. The first is therefore a subset of both `fresh_claims` and `claims_cited`.
"""


def _reach_ratio(card: Scorecard, num_keys: tuple[str, ...], den_keys: tuple[str, ...]) -> tuple[int, int] | None:
    num, den = card.get(*num_keys), card.get(*den_keys)
    if num is None or not den:
        return None
    return int(num), int(den)


def evidence_reach(cards: Sequence[Scorecard], out_dir: Path) -> Path:
    """Two nested ratios: how much of its own evidence a run reused, and how much its authors relied on it."""
    colors = _series_colors(cards)
    offsets = _offsets(len(cards))
    height = _bar_height(len(cards))
    fig, ax = plt.subplots(figsize=(8.6, 0.42 * len(REACH_RATIOS) * len(cards) + 2.1), facecolor=SURFACE)

    drew = False
    for card, color, offset in zip(cards, colors, offsets):
        for index, (_, _, num_keys, den_keys) in enumerate(REACH_RATIOS):
            ratio = _reach_ratio(card, num_keys, den_keys)
            y = index + offset
            if ratio is None:
                ax.text(0.01, y, "not recorded", va="center", ha="left", fontsize=8, color=INK_MUTED)
                continue
            drew = True
            num, den = ratio
            share = num / den
            ax.barh(y, share, height=height, color=color, edgecolor=SURFACE, linewidth=1.2)
            ax.text(share + 0.012, y, f"{share:.2f}  ({num}/{den})", va="center", ha="left", fontsize=8.5, color=INK)
    if not drew:
        plt.close(fig)
        raise FigureDataMissing("no evidence-reach fields in any scorecard")

    ax.set_yticks(range(len(REACH_RATIOS)))
    ax.set_yticklabels([label for _, label, _, _ in REACH_RATIOS], color=INK_SECONDARY, fontsize=9)
    _category_rows(ax, len(REACH_RATIOS))
    _rate_axis(ax, label_room=0.35)
    _style(
        ax,
        title="Fresh evidence reuse in hypothesis generation",
        subtitle="",
        xlabel="Proportion",
    )
    _legend(ax, [c.run_id for c in cards], colors)
    return _save(fig, out_dir, "fig4-evidence-reach.png")


# --- Figure 5: unfaithful rate by candidate class ------------------------------------------------


def candidate_classes(cards: Sequence[Scorecard], out_dir: Path) -> Path:
    """Grouped bars: the unfaithful rate of each verify candidate class, with its pool size.

    A class a run never had (`source_quoted_unverified` before the widening) is drawn as absent.
    """
    classes: list[str] = []
    for card in cards:
        for name in card.get("verify_candidate_classes") or {}:
            if name not in classes:
                classes.append(name)
    if not classes:
        raise FigureDataMissing("no verify candidate classes in any scorecard")

    colors = _series_colors(cards)
    offsets = _offsets(len(cards))
    height = _bar_height(len(cards))
    fig, ax = plt.subplots(figsize=(9.8, 0.72 * len(classes) * len(cards) + 2.2), facecolor=SURFACE)

    for card, color, offset in zip(cards, colors, offsets):
        for index, name in enumerate(classes):
            block = card.get("verify_candidate_classes", name)
            y = index + offset
            if not block:
                ax.text(0.01, y, "class did not exist in this run", va="center", ha="left", fontsize=8, color=INK_MUTED)
                continue
            rate = float(block.get("unfaithful_rate") or 0.0)
            ax.barh(y, rate, height=height, color=color, edgecolor=SURFACE, linewidth=1.2)
            ax.text(
                rate + 0.012,
                y,
                f"{rate:.2f}  ({block.get('unfaithful')}/{block.get('candidates')} candidates,"
                f" {float(block.get('share_of_pool') or 0.0):.0%} of the pool)",
                va="center",
                ha="left",
                fontsize=8,
                color=INK,
            )

    ax.set_yticks(range(len(classes)))
    ax.set_yticklabels(classes, color=INK_SECONDARY, fontsize=9)
    _category_rows(ax, len(classes))
    _rate_axis(ax, label_room=1.05)
    _style(
        ax,
        title="Claims that say more than their quote",
        subtitle="unfaithful share of each verify candidate class",
        xlabel="share of the class",
    )
    _legend(ax, [c.run_id for c in cards], colors)
    return _save(fig, out_dir, "fig5-candidate-classes.png")


# --- Figure 6: the phase sequence, and how many agents each phase ran ----------------------------
# Structure, not performance. It answers "what is a run made of" for a reader who has only the
# archive, and it is drawn from the same scorecards as every other figure: the stage keys carry the
# run order in their numeric prefix, and `agents` is the count the host recorded for that launch.
# The two code phases (`data`, `compute`) run no agent at all and must read as zero-by-design, not
# as a missing measurement, so they are drawn as a labelled marker on the axis rather than a bar.


def phase_sequence(cards: Sequence[Scorecard], out_dir: Path) -> Path:
    """One horizontal bar per phase: the agents it launched, in run order.

    Code phases launch no agent by construction and are labelled as such instead of
    being drawn as a zero-height bar, which would read as a measurement that came back empty.
    """
    card = cards[-1]
    stages = sorted((card.get("cost", "stages") or {}).keys())
    if not stages:
        raise FigureDataMissing("no cost stages in the newest scorecard")

    phases = [("01-data", None), ("02-compute", None)]
    phases += [(stage, (card.get("cost", "stages", stage) or {}).get("agents")) for stage in stages]

    fig, ax = plt.subplots(figsize=(8.6, 0.34 * len(phases) + 2.0), facecolor=SURFACE)
    widest = max([v for _, v in phases if v is not None] or [1])
    for index, (stage, agents) in enumerate(phases):
        if agents is None:
            ax.text(
                widest * 0.012,
                index,
                "no agent — deterministic Python in the host",
                va="center",
                ha="left",
                fontsize=8.5,
                color=INK_MUTED,
            )
            continue
        ax.barh(index, float(agents), height=0.62, color=SERIES[0], edgecolor=SURFACE, linewidth=1.2)
        ax.text(float(agents) + widest * 0.018, index, str(agents), va="center", ha="left", fontsize=8.5, color=INK)

    ax.set_yticks(range(len(phases)))
    ax.set_yticklabels([stage.split("-", 1)[1] for stage, _ in phases], color=INK_SECONDARY, fontsize=9)
    ax.invert_yaxis()
    ax.set_xlim(0, widest * 1.22)
    _style(
        ax,
        title="What a run is made of",
        subtitle=f"{card.run_id}: {len(phases)} phases in order, and the agents each one launched",
        xlabel="agents launched",
    )
    return _save(fig, out_dir, "fig6-phase-sequence.png")


# --- Figure 7: quote-check outcomes per stage --------------------------------------------------

# The five groups the nine-way outcome vocabulary of `evaluation/quote_outcomes.py` folds into, best
# first. The raw nine counts stay in the scorecard and in that module's table: a stack of nine is not
# readable, and `SERIES` has three categorical slots by design (`_series_colors` refuses a fourth).
OUTCOME_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("quote found", ("checked:exact", "checked:near")),
    ("quote not found", ("checked:absent",)),
    ("declined: no paper", ("paper_not_in_store",)),
    ("declined: text unusable", ("text_not_available", "text_incomplete", "text_hash_mismatch")),
    ("declined: other", ("bad_input", "service_error", "unparsed")),
)

# Sequential blue, dark to light, five steps of the one hue: this is an ordered disposition from "the
# check worked" to "the check could not happen", not five identities. Steps 700/550/450/350/250 of the
# reference ramp, evenly spaced; the lightest clears the 2:1 ordinal floor against SURFACE at 2.06:1.
# Validated, not eyeballed: `validate_palette.js --ordinal --surface #fcfcfb` passes all four checks.
OUTCOME_RAMP = ("#0d366b", "#1c5cab", "#2a78d6", "#5598e7", "#86b6ef")


def _grouped(counts: Mapping[str, Any]) -> dict[str, int]:
    """One stage's nine-way counts folded into the five groups. An unknown key is not silently lost."""
    out = {label: 0 for label, _ in OUTCOME_GROUPS}
    known = {k for _, keys in OUTCOME_GROUPS for k in keys}
    for label, keys in OUTCOME_GROUPS:
        out[label] = sum(int(counts.get(k, 0)) for k in keys)
    if leftover := {k: v for k, v in counts.items() if k not in known and int(v or 0)}:
        # The grouping is exhaustive over the vocabulary; a new outcome joins "other" rather than vanish.
        out["declined: other"] += sum(int(v) for v in leftover.values())
    return out


def quote_outcomes_by_stage(cards: Sequence[Scorecard], out_dir: Path, *, leg: str = "agents") -> Path:
    """One stacked bar per stage: what `check_quote` said, where in the pipeline it was asked.

    Per stage and not only per run because the literature store **grows during a run**: a refusal at
    `literature` and the same refusal at `synthesize` are different facts. One leg per figure — the
    agents' own calls by default — because the host verifier and the agents are different callers and
    adding them means nothing.
    """
    rows: list[tuple[str, str, dict[str, int]]] = []
    for card in cards:
        block = card.get("quote_outcomes", "legs", leg) or {}
        for stage, counts in (block.get("by_stage") or {}).items():
            grouped = _grouped(counts)
            if sum(grouped.values()):
                rows.append((card.run_id, stage, grouped))
    if not rows:
        raise FigureDataMissing(f"no quote-check outcomes for the {leg} leg in any scorecard")

    labels = [label for label, _ in OUTCOME_GROUPS]
    fig, ax = plt.subplots(figsize=(10.2, 0.56 * len(rows) + 2.4), facecolor=SURFACE)
    widest = max(sum(counts.values()) for _, _, counts in rows)
    for index, (run_id, stage, counts) in enumerate(rows):
        left = 0.0
        total = sum(counts.values())
        for label, color in zip(labels, OUTCOME_RAMP):
            value = counts[label]
            if value == 0:
                continue
            ax.barh(index, value, left=left, height=0.52, color=color, edgecolor=SURFACE, linewidth=1.6)
            if value / total > 0.08:  # a direct label only where the segment can hold one
                ax.text(
                    left + value / 2,
                    index,
                    f"{value}",
                    va="center",
                    ha="center",
                    fontsize=9,
                    color=INK if color in OUTCOME_RAMP[3:] else "#ffffff",
                )
            left += value
        ax.text(
            left + widest * 0.015, index, f"{total} checks", va="center", ha="left", fontsize=8.5, color=INK_SECONDARY
        )

    many = len({run for run, _, _ in rows}) > 1
    ax.set_yticks(range(len(rows)))
    ax.set_yticklabels(
        [f"{run} / {stage}" if many else stage for run, stage, _ in rows], color=INK_SECONDARY, fontsize=9
    )
    ax.invert_yaxis()
    ax.set_xlim(0, widest * 1.24)
    _style(
        ax,
        title=(
            f"What the quote check said, per stage — the {leg}' own calls"
            if leg == "agents"
            else f"What the quote check said, per stage — the {leg} verifier"
        ),
        subtitle="counts, not rates; nine outcomes folded into five ordered groups, best first",
        xlabel="check_quote calls",
    )
    _legend(ax, labels, list(OUTCOME_RAMP))
    return _save(fig, out_dir, "fig7-quote-outcomes-by-stage.png")


# --- Figure 8: the mechanism chain each proposal cites, one panel per run --------------------------
# A comparison of runs by panel, not by hue, so it takes up to MAX_PANELS runs where the others take
# MAX_SERIES. Step k of every chain sits on the same row in every panel and every box has the same
# size, so the eye compares lengths and positions, not box shapes. The chain is `run_score`'s:
# the longest simple path among the causal claims section A cites, read from `chain_steps`.

MAX_PANELS = 4
BOX_CHARS = 34  # per line, at 7.5 pt in a box 0.86 of a 3.2 in panel
BOX_LINES = 2
ELLIPSIS = "..."
# Three kinds of step, dark to light by how much stands behind them. Each is also named in the arrow's label,
# so the kind never rests on colour alone.
CHAIN_KINDS = {
    "verified": ("#184f95", "-", "verified: its quote was found in the stored source"),
    "unverified": ("#2a78d6", (0, (4, 2)), "unverified source: a quote not (yet) found in it"),
    "inference": ("#86b6ef", (0, (1, 2)), "agent inference: no source, written by an agent"),
}


def _step_kind(step: Mapping[str, Any]) -> str:
    if step.get("provenance_status") == "verified":
        return "verified"
    return "inference" if step.get("origin") == "agent" else "unverified"


BOX_WIDTH = 0.86
BOX_HEIGHT = 0.46  # in rows; the rest of a row is the arrow


def _fit(text: str, width: int = BOX_CHARS, lines: int = BOX_LINES) -> str:
    """At most `lines` lines of `width` characters; what does not fit is cut and marked with ELLIPSIS."""
    wrapped = textwrap.wrap(text, width=width) or [""]
    if len(wrapped) <= lines:
        return "\n".join(wrapped)
    kept = wrapped[:lines]
    kept[-1] = kept[-1][: width - len(ELLIPSIS)].rstrip() + ELLIPSIS
    return "\n".join(kept)


def _chain_nodes(steps: Sequence[Mapping[str, Any]]) -> list[str]:
    """The entity names along the chain, cause of the first step first; a missing name shows its ID."""

    def name(ref: Any, value: Any) -> str:
        return str(value) if value else f"{ref} (no name recorded)"

    first = steps[0]
    return [name(first.get("cause_ref"), first.get("cause_name"))] + [
        name(step.get("effect_ref"), step.get("effect_name")) for step in steps
    ]


def mechanism_chains(cards: Sequence[Scorecard], out_dir: Path) -> Path:
    """One panel per run: the chain of entities its mechanism section cites, verified steps drawn solid."""
    if len(cards) > MAX_PANELS:
        raise ValueError(f"at most {MAX_PANELS} runs per figure; split the runs over two figures")
    chains = [(card, card.get("mechanism_chain", "chain_steps")) for card in cards]
    if not any(steps for _, steps in chains):
        raise FigureDataMissing("no chain_steps in any scorecard (re-score the run with evaluation.run_score)")

    rows = max(len(steps) + 1 for _, steps in chains if steps)
    fig, axes = plt.subplots(
        1, len(cards), figsize=(3.2 * len(cards), 0.62 * rows + 1.6), facecolor=SURFACE, squeeze=False
    )
    for ax, (card, steps) in zip(axes[0], chains):
        ax.set_facecolor(SURFACE)
        ax.set_xlim(0, 1)
        ax.set_ylim(rows - 0.5, -1.1)
        ax.axis("off")
        if steps:
            kinds = [_step_kind(step) for step in steps]
            caption = (
                f"{len(steps)} edges: {kinds.count('verified')} verified, "
                f"{kinds.count('inference')} agent inference{'' if kinds.count('inference') == 1 else 's'}"
            )
        else:
            reason = card.get("mechanism_chain", "reason")
            caption = (
                "chain_steps not in this scorecard: re-score"
                if card.get("mechanism_chain", "chain") is not None
                else f"not evaluable: {reason or 'no mechanism chain recorded'}"
            )
        ax.text(0.5, -0.85, card.run_id, ha="center", va="center", fontsize=10, fontweight="semibold", color=INK)
        ax.text(0.5, -0.52, _fit(caption, width=44), ha="center", va="center", fontsize=8, color=INK_SECONDARY)
        if not steps:
            continue
        for row, name in enumerate(_chain_nodes(steps)):
            ax.add_patch(
                FancyBboxPatch(
                    (0.5 - BOX_WIDTH / 2, row - BOX_HEIGHT / 2),
                    BOX_WIDTH,
                    BOX_HEIGHT,
                    boxstyle="round,pad=0,rounding_size=0.04",
                    facecolor="#ffffff",
                    edgecolor=INK_MUTED,
                    linewidth=0.6,
                    mutation_aspect=0.5,
                )
            )
            ax.text(0.5, row, _fit(name), ha="center", va="center", fontsize=7.5, color=INK)
        for row, step in enumerate(steps):
            kind = _step_kind(step)
            color, style, _ = CHAIN_KINDS[kind]
            ax.annotate(
                "",
                xy=(0.5, row + 1 - BOX_HEIGHT / 2),
                xytext=(0.5, row + BOX_HEIGHT / 2),
                arrowprops=dict(arrowstyle="-|>", color=color, linewidth=1.8, linestyle=style),
            )
            ax.text(
                0.54,
                row + 0.5,
                f"{step.get('claim_id')}  {'agent inference' if kind == 'inference' else kind}",
                ha="left",
                va="center",
                fontsize=7,
                color=color if kind == "verified" else INK_SECONDARY,
                fontweight="semibold" if kind == "verified" else "normal",
            )

    fig.suptitle(
        "The mechanism chain each proposal cites",
        x=0.01,
        y=1.0,
        ha="left",
        va="bottom",
        fontsize=12,
        fontweight="semibold",
        color=INK,
    )
    handles = [
        Line2D([], [], color=color, linewidth=1.8, linestyle=style, label=label)
        for color, style, label in CHAIN_KINDS.values()
    ]
    legend = fig.legend(
        handles=handles,
        loc="lower left",
        bbox_to_anchor=(0.01, 0.955),
        ncol=3,
        frameon=False,
        fontsize=8.5,
        handlelength=2.4,
    )
    for text in legend.get_texts():
        text.set_color(INK_SECONDARY)
    fig.text(
        0.01,
        0.95,
        "box: an entity, whose name carries the direction of the effect; arrow: the cited causal claim joining "
        f"two entities, labelled with its ID and kind. Same row = same step. Long names are cut with '{ELLIPSIS}'. "
        "Claim IDs are per knowledge base: the same ID in two runs on different bases is not the same claim. "
        "Not a causal model identified from data.",
        ha="left",
        va="top",
        fontsize=7.5,
        color=INK_SECONDARY,
        wrap=True,
    )
    fig.subplots_adjust(top=0.9, bottom=0.01, left=0.01, right=0.99, wspace=0.08)
    return _save(fig, out_dir, "fig8-mechanism-chains.png")


FIGURES = (
    cost_by_phase,
    verify_verdicts,
    quote_exact_rate,
    evidence_reach,
    candidate_classes,
    phase_sequence,
    quote_outcomes_by_stage,
    mechanism_chains,
)


# --- report.txt --------------------------------------------------------------------------------
# Every figure ships with prose saying what it plots, what its axes are, which direction is the
# desirable one, and what this particular archive shows. The prose is written by the same code that
# draws the bars, from the same scorecard fields, so the two cannot drift apart; the "observed" line
# is arithmetic over those fields and states no cause, no success and no explanation.


def _fmt(value: Any, digits: int = 2) -> str:
    return "not recorded" if value is None else f"{float(value):.{digits}f}"


def _observed_cost(cards: Sequence[Scorecard], cap_usd: float | None) -> list[str]:
    lines = []
    for card in cards:
        stages = card.get("cost", "stages") or {}
        priced = {k: v["usd"] for k, v in stages.items() if isinstance(v, dict) and "usd" in v}
        if not priced:
            lines.append(f"{card.run_id}: no priced stage.")
            continue
        top = max(priced, key=priced.get)
        lines.append(
            f"{card.run_id}: {_fmt(card.get('cost', 'total_usd'), 3)} USD over "
            f"{len(priced)} priced phases; dearest is {top} at {_fmt(priced[top], 3)} "
            f"({priced[top] / sum(priced.values()):.0%} of the run)."
        )
    if cap_usd is not None:
        lines.append(f"Cap in force: {cap_usd:g} USD.")
    return lines


def _observed_verdicts(cards: Sequence[Scorecard]) -> list[str]:
    lines = []
    for card in cards:
        counts = card.get("verify", "counts") or {}
        total = sum(int(counts.get(v, 0)) for v in VERDICTS)
        if not total:
            lines.append(f"{card.run_id}: no verdicts.")
            continue
        share = {v: int(counts.get(v, 0)) / total for v in VERDICTS}
        lines.append(
            f"{card.run_id}: {total} offered — "
            + ", ".join(f"{v} {counts.get(v, 0)} ({share[v]:.0%})" for v in VERDICTS)
            + "."
        )
    return lines


def _observed_quote(cards: Sequence[Scorecard], threshold: float | None) -> list[str]:
    lines = []
    for card in cards:
        for phase, block in (card.get("quote_checks") or {}).items():
            rate = block.get("exact_rate_of_judged")
            side = ""
            if threshold is not None and rate is not None:
                side = " (at or above the floor)" if float(rate) >= threshold else " (below the floor)"
            lines.append(
                f"{card.run_id} / {phase}: {_fmt(rate)} from {block.get('exact')} exact of "
                f"{block.get('judged')} judged, {block.get('calls')} calls, "
                f"{block.get('absent')} absent{side}."
            )
    return lines


def _observed_reach(cards: Sequence[Scorecard]) -> list[str]:
    lines = []
    for card in cards:
        parts = []
        for name, _, num_keys, den_keys in REACH_RATIOS:
            ratio = _reach_ratio(card, num_keys, den_keys)
            parts.append(
                f"{name}: {ratio[0]}/{ratio[1]} = {ratio[0] / ratio[1]:.2f}" if ratio else f"{name}: not recorded"
            )
        lines.append(f"{card.run_id}: " + "; ".join(parts) + ".")
    return lines


MIN_VERIFIER_AGENTS = 4
"""Below this, an unfaithful rate is not read as a property of the run.

`faithful` is judged by one agent per batch of candidates, and the judgement varies enormously between
agents: in one run, 28 agents of 10 relations each spanned **0.00 to 1.00** (mean 0.44, sd 0.25). A
run-level rate is therefore an average over however many agents the pool happened to fill, and
`phases.verify.only_on_run_kb` sets that pool. A run whose rate comes from two agents can look like a finding
when it is mostly the denominator.
"""


def _verify_agents(card: Scorecard) -> int | None:
    """The agents the `verify` phase launched, or None when the run does not record it."""
    stages = card.get("cost", "stages") or {}
    for name, block in stages.items():
        if name.endswith("verify"):
            agents = (block or {}).get("agents")
            return agents if isinstance(agents, int) else None
    return None


def _observed_classes(cards: Sequence[Scorecard]) -> list[str]:
    lines = []
    for card in cards:
        blocks = card.get("verify_candidate_classes") or {}
        if not blocks:
            lines.append(f"{card.run_id}: no class recorded.")
            continue
        agents = _verify_agents(card)
        judged_by = (
            "verifier agents not recorded"
            if agents is None
            else f"judged by {agents} verifier agent{'s' if agents != 1 else ''}"
        )
        for name, block in blocks.items():
            lines.append(
                f"{card.run_id} / {name}: unfaithful {_fmt(block.get('unfaithful_rate'))} "
                f"({block.get('unfaithful')} of {block.get('candidates')}), "
                f"{float(block.get('share_of_pool') or 0.0):.0%} of the pool, {judged_by}."
            )
        if agents is not None and agents < MIN_VERIFIER_AGENTS:
            lines.append(
                f"{card.run_id}: NOT COMPARABLE as a rate — {agents} verifier agent(s), below the "
                f"{MIN_VERIFIER_AGENTS} this report requires. The per-agent spread measured on "
                f"one production run is 0.00 to 1.00 (28 agents, mean 0.44, sd 0.25), so this run's share "
                f"is a few draws from that, not a property of its evidence."
            )
    return lines


def _observed_phases(cards: Sequence[Scorecard]) -> list[str]:
    card = cards[-1]
    stages = card.get("cost", "stages") or {}
    agents = [(s, (stages[s] or {}).get("agents")) for s in sorted(stages)]
    total = sum(v for _, v in agents if isinstance(v, int))
    lines = [
        f"{card.run_id}: {len(agents) + 2} phases, {total} agents over the {len(agents)} agent phases; "
        "`data` and `compute` launched none."
    ]
    ranked = sorted([(v, s) for s, v in agents if isinstance(v, int)], reverse=True)
    if ranked:
        lines.append("Most agents: " + ", ".join(f"{s.split('-', 1)[1]} {v}" for v, s in ranked[:3]) + ".")
    return lines


def _observed_quote_outcomes(cards: Sequence[Scorecard]) -> list[str]:
    """Arithmetic over the `quote_outcomes` block: the stage composition, and the refusals that named
    a resolvable identifier. It states no cause — a refusal is not a wrong answer."""
    lines: list[str] = []
    for card in cards:
        leg = card.get("quote_outcomes", "legs", "agents") or {}
        by_stage = leg.get("by_stage") or {}
        if not by_stage:
            lines.append(f"{card.run_id}: no `check_quote` call by an agent.")
            continue
        total = int(leg.get("checks") or 0)
        refused = int((leg.get("totals") or {}).get("paper_not_in_store", 0))
        resolvable = leg.get("paper_not_in_store_resolvable_total")
        lines.append(
            f"{card.run_id}: {total} agent calls over {len(by_stage)} stages — "
            + ", ".join(f"{s} {sum(_grouped(c).values())}" for s, c in by_stage.items())
            + "."
        )
        lines.append(
            f"{card.run_id}: {refused} declined for want of the paper, of which "
            f"{_fmt(resolvable, 0)} named a resolvable identifier."
        )
        fetch = card.get("quote_outcomes", "fetches") or {}
        attempts = int(fetch.get("attempts") or 0)
        got = int((fetch.get("totals") or {}).get("fetched", 0))
        lines.append(
            f"{card.run_id}: {attempts} fetch attempts recorded, {got} of them fetched"
            + (f"; stages that attempted one: {', '.join(fetch.get('by_stage') or {})}." if attempts else ".")
        )
    return lines


def _observed_chains(cards: Sequence[Scorecard]) -> list[str]:
    lines = []
    for card in cards:
        steps = card.get("mechanism_chain", "chain_steps")
        if not steps:
            lines.append(f"{card.run_id}: no chain steps recorded")
            continue
        kinds = [_step_kind(s) for s in steps]
        verified = [s.get("claim_id") for s, k in zip(steps, kinds) if k == "verified"]
        lines.append(
            f"{card.run_id}: {len(steps)} edges, {len(verified)} verified"
            + (f" ({', '.join(map(str, verified))})" if verified else "")
            + f", {kinds.count('inference')} agent inferences, {kinds.count('unverified')} unverified sources"
        )
    return lines


NOTES: dict[str, dict[str, str]] = {
    "fig1-cost-by-phase.png": {
        "shows": "What each phase of each run cost, in USD, as grouped horizontal bars.",
        "axes": "x: USD, linear from 0, increasing rightwards (longer bar = more money). "
        "y: the phase, in run order top to bottom. One bar per run per phase, coloured by run.",
        "desirable": "Lower is cheaper, but low is not a goal on its own: a phase can be cheap because "
        "it did little. Read it against the run total and the cap. What would be a "
        "problem is a phase whose cost moves without its configuration moving.",
    },
    "fig2-verify-verdicts.png": {
        "shows": "How the verifier disposed of every relation offered to it, one stacked bar per run.",
        "axes": "x: number of relations, from 0, increasing rightwards; the bar's full length is the "
        "candidate pool. y: the run. Segments are ordered confirmed, rejected, insufficient "
        "and partition the pool, so widths add up.",
        "desirable": "No direction is good in itself — the verifier is allowed to reject and to decline. "
        "A large `insufficient` share is the open question of this project: whether the "
        "verifier is right or merely unhelpful. It must not be read as a defect without a "
        "scorer.",
    },
    "fig3-quote-exact-rate.png": {
        "shows": "The share of judged `check_quote` calls that came back `exact`, per phase, per run, "
        "against the pre-registered floor.",
        "axes": "x: share of judged calls, 0 to 1, increasing rightwards. y: the phase. The dashed red "
        "rule is the pre-registered floor, named in words beside it.",
        "desirable": "Higher is better: a quoted claim matching its source. The denominator is *judged* "
        "calls — `unverifiable` is the checker declining, not the quote failing, so it is "
        "excluded and the raw call count is printed beside each bar. A high rate over few "
        "judged calls says less than a lower rate over many.",
    },
    "fig4-evidence-reach.png": {
        "shows": "Two ratios over the claims this run's literature phase wrote (fresh). Reuse: the fresh "
        "claims a hypothesis-author relation links, over the fresh claims written. Reliance: the same "
        "linked fresh claims, over every claim the authors link that they did not write, whatever its origin. "
        "Each numerator is a subset of its denominator.",
        "axes": "x: share, from 0 to 1, increasing rightwards; the counts are printed beside each bar. "
        "y: the two ratios.",
        "desirable": "Low reuse means a run paid for evidence its authors did not use. Reliance is not a "
        "score: a low value means the authors drew on claims already in the knowledge base, which is "
        "expected when the run starts from an accumulated one.",
    },
    "fig6-phase-sequence.png": {
        "shows": "The phases of a run in order, and how many agents each one launched. Structure, "
        "not performance: it answers what a run is made of.",
        "axes": "x: agents launched, from 0, increasing rightwards. y: the phase, in run order top "
        "to bottom. The two code phases carry no bar and are labelled instead.",
        "desirable": "No direction is good or bad. More agents is not better work: `verify` runs the "
        "most because its pool is batched ten relations to an agent, and `synthesize` "
        "runs one because there is one principal investigator. A zero would be wrong "
        "for an agent phase and is drawn as a bar of length zero, never as the "
        "'no agent' label, which is reserved for the phases that launch none by design.",
    },
    "fig5-candidate-classes.png": {
        "shows": "The unfaithful share of each class of verify candidate — claims saying more than "
        "their quote — with the class's size and share of the pool.",
        "axes": "x: share of the class, 0 to 1, increasing rightwards. y: the candidate class. A class "
        "a run never had is drawn as absent, never as a zero bar.",
        "desirable": "Lower is better WITHIN a run, and comparing classes is the point: if the class "
        "admitted by a widening is no worse than the one that was always there, the "
        "widening did not lower the standard. Across runs it is not a score. The rate is "
        "an average over the verify phase's agents — one per batch of ten candidates — and "
        "the per-agent judgement spans the whole range: 28 agents of one production run scored "
        "0.00 to 1.00 faithful, mean 0.44, sd 0.25. A run whose pool was small ran few "
        "agents, so its rate is a few draws from that spread; Observed names the agent "
        "count beside every rate and says NOT COMPARABLE below four.",
    },
    "fig7-quote-outcomes-by-stage.png": {
        "shows": "What `check_quote` answered the agents, per pipeline stage, as one stacked bar per "
        "stage: the nine-way outcome vocabulary folded into five ordered groups, best first.",
        "axes": "x: number of `check_quote` calls, from 0, increasing rightwards; the bar's full length "
        "is every call that stage made. y: the stage, in pipeline order; a re-attempt keeps its "
        "own row. Segments are ordered quote found, quote not found, declined for want of the "
        "paper, declined for want of usable text, declined otherwise — and partition the calls, "
        "so widths add up. One leg per figure: the agents' own calls by default, never summed "
        "with the host verifier's.",
        "desirable": "No direction is good in itself. A `declined: no paper` segment is not a wrong "
        "answer — it is a check that could not happen, which is a fact about the store, not "
        "about the quote. The stage axis is the point: the store grows during a run, so the "
        "same refusal early and late are different facts, and a refusal in a stage that "
        "attempted no fetch says the paper was never asked for. The nine raw counts and the "
        "share of refusals that named a resolvable identifier are in the scorecard's "
        "`quote_outcomes` block, not on this chart.",
    },
    "fig8-mechanism-chains.png": {
        "shows": "For each run, the chain of entities its mechanism section (output A) cites: the longest "
        "simple path among the cited causal claims, as computed by `run_score.mechanism_chain`. One "
        "panel per run, up to four.",
        "axes": "No numeric axes. Vertical: the step of the chain, first cause at the top; the same row is "
        "the same step in every panel. A box is an entity, whose name carries the direction of the effect "
        "(there is no sign, ADR 0032); an arrow is the claim joining two entities: solid dark if its quote "
        "was found in its stored source, dashed if it names a source not (yet) checked, dotted light if it "
        "is an agent's inference with no source. The kind is also written in the arrow's label.",
        "desirable": "Longer is not better: a chain of many agent inferences is a longer story, not "
        "stronger evidence, and no phase judges an inference's plausibility. What a reader should look at is how many steps are verified and where, "
        "and whether different runs reach the same entities. Claim IDs are per knowledge base and "
        "cannot be matched across runs on different bases.",
    },
}


WIDTH = 96


def _wrap(label: str, text: str) -> list[str]:
    """`  Label:    text`, wrapped to WIDTH with the continuation lines under the text."""
    head = f"  {label + ':':<11}"
    return textwrap.wrap(text, width=WIDTH, initial_indent=head, subsequent_indent=" " * len(head))


def write_report(
    cards: Sequence[Scorecard],
    out_dir: Path,
    rendered: dict[str, str],
    *,
    cap_usd: float | None = None,
    quote_threshold: float | None = None,
) -> Path:
    """Write `report.txt` beside the figures: what each plot is, and what this archive shows."""
    observers = {
        "fig1-cost-by-phase.png": lambda: _observed_cost(cards, cap_usd),
        "fig2-verify-verdicts.png": lambda: _observed_verdicts(cards),
        "fig3-quote-exact-rate.png": lambda: _observed_quote(cards, quote_threshold),
        "fig4-evidence-reach.png": lambda: _observed_reach(cards),
        "fig5-candidate-classes.png": lambda: _observed_classes(cards),
        "fig6-phase-sequence.png": lambda: _observed_phases(cards),
        "fig7-quote-outcomes-by-stage.png": lambda: _observed_quote_outcomes(cards),
        "fig8-mechanism-chains.png": lambda: _observed_chains(cards),
    }
    produced = {Path(v).name for v in rendered.values() if not v.startswith("skipped")}

    out = [
        "Diagnostic figures — what they plot and what this archive shows",
        "=" * 78,
        "",
        "Runs, in the order the colours follow: " + ", ".join(c.run_id for c in cards) + ".",
        "Source: the persisted run scorecards only. This file is written by the same code that",
        "draws the figures, from the same fields, so it cannot disagree with them. The `observed`",
        "lines are arithmetic over those fields: they state no cause and no success.",
        "",
    ]

    for name, note in NOTES.items():
        out += [name, "-" * len(name)]
        if name not in produced:
            reason = next(
                (
                    v
                    for k, v in rendered.items()
                    if v.startswith("skipped") and name.split("-", 1)[1].split(".")[0].replace("-", "_") in k
                ),
                "not rendered",
            )
            out += [f"  NOT RENDERED — {reason}", ""]
            continue
        for field in ("shows", "axes", "desirable"):
            out += _wrap(field.capitalize(), note[field])
        out += ["  Observed:"]
        out += [f"    - {line}" for line in observers[name]()]
        out += [""]

    path = out_dir / "report.txt"
    path.write_text("\n".join(out))
    return path


def render_all(
    cards: Sequence[Scorecard], out_dir: Path, *, cap_usd: float | None = None, quote_threshold: float | None = None
) -> dict[str, str]:
    """Render every figure that has data, reporting per figure what happened.

    A figure with no data is reported as skipped with its reason: the caller learns that the
    measurement is absent instead of receiving an empty chart.
    """
    report: dict[str, str] = {}
    for fn in FIGURES:
        kwargs: dict[str, Any] = {}
        if fn is cost_by_phase:
            kwargs["cap_usd"] = cap_usd
        if fn is quote_exact_rate:
            kwargs["threshold"] = quote_threshold
        if len(cards) > MAX_SERIES and fn is not mechanism_chains:
            report[fn.__name__] = f"skipped: {len(cards)} runs, and this figure colours at most {MAX_SERIES}"
            continue
        try:
            report[fn.__name__] = str(fn(cards, out_dir, **kwargs))
        except FigureDataMissing as exc:
            report[fn.__name__] = f"skipped: {exc}"
    report["report_txt"] = str(write_report(cards, out_dir, report, cap_usd=cap_usd, quote_threshold=quote_threshold))
    return report


def load_cards(paths: Iterable[Path]) -> list[Scorecard]:
    return [Scorecard.load(path) for path in paths]


def main(argv: list[str] | None = None) -> int:
    """`python -m evaluation.figures SCORECARD [SCORECARD ...] --out DIR`

    Exit: 0 = every figure rendered, 1 = at least one skipped for want of data, 2 = input error.
    """
    import argparse

    parser = argparse.ArgumentParser(description="Render diagnostic PNGs from one or more persisted run scorecards.")
    parser.add_argument(
        "scorecards", nargs="+", type=Path, help="run-score*.json files, in the order the runs should be coloured"
    )
    parser.add_argument("--out", type=Path, required=True, help="output directory for the PNGs")
    parser.add_argument("--cap-usd", type=float, help="annotate the cost figure with the run cap")
    parser.add_argument("--quote-threshold", type=float, help="draw a pre-registered floor on the quote-rate figure")
    args = parser.parse_args(argv)

    missing = [str(p) for p in args.scorecards if not p.is_file()]
    if missing:
        print(f"error: scorecard not found: {', '.join(missing)}", file=sys.stderr)
        return 2
    cards = load_cards(args.scorecards)
    report = render_all(cards, args.out, cap_usd=args.cap_usd, quote_threshold=args.quote_threshold)
    for name, outcome in report.items():
        print(f"{name}: {outcome}")
    return 1 if any(v.startswith("skipped") for v in report.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
