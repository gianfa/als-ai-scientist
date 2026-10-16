"""The deduplication stages against 107 labelled pairs drawn from the live knowledge base.

The fixture `tests/data/entity-pairs.csv` is real: `scripts/dev/dedup_fixture_candidates.py` drew the pairs from
`knowledgebase/entities.jsonl` (git-ignored, hence a committed fixture) in five bands, and the `duplicate`
column is a hand label — `yes`, `no` or `unclear` — with its reason. A label is a judgement and is not evidence
of anything beyond itself; what these tests check is whether a stage agrees with it, and they print the
disagreements rather than only a number.

Two stages, two kinds of test:

- the deterministic stage runs by default and its behaviour is asserted;
- the Claude-backed second stage is **paid** and runs only under `ALS_DEDUP_REVIEW=claude`. Without it the
  second-stage test is skipped and `identity_review` is checked instead: it must decide nothing.

The thresholds asserted below are a measurement, not targets. If a rule changes, the numbers
change with it, and the point of pinning them is that the change has to be deliberate.
"""

from __future__ import annotations

import csv
import os
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pytest

from knowledge_base.dedup import REVIEW_FLOOR, Verdict, compare, forms, identity_review, resolve
from knowledge_base.schemas import Entity

FIXTURE = Path(__file__).parent / "data" / "entity-pairs.csv"
REVIEW_BACKEND = os.environ.get("ALS_DEDUP_REVIEW", "identity")


def entity(identifier: str, name: str) -> Entity:
    """A fixture entity: only the id and the name matter to this module."""
    return Entity(id=identifier, run_id="fixture", canonical_name=name, created_at="2026-10-08T00:00:00Z")


@dataclass(frozen=True)
class Pair:
    pair_id: str
    band: str
    a_id: str
    a_name: str
    b_id: str
    b_name: str
    duplicate: str
    reason: str


@pytest.fixture(scope="module")
def pairs() -> list[Pair]:
    with FIXTURE.open(newline="") as handle:
        rows = [Pair(**row) for row in csv.DictReader(handle)]
    assert rows, "the fixture is empty"
    return rows


def _agreement(pairs: list[Pair], verdicts: dict[str, Verdict]) -> dict[str, Counter]:
    """Label against decision, per band and overall. A label of `unclear` is counted, never scored."""
    table: dict[str, Counter] = {}
    for pair in pairs:
        cell = (pair.duplicate, verdicts[pair.pair_id].decision)
        table.setdefault(pair.band, Counter())[cell] += 1
        table.setdefault("all", Counter())[cell] += 1
    return table


def _report(title: str, pairs: list[Pair], verdicts: dict[str, Verdict]) -> None:
    table = _agreement(pairs, verdicts)
    print(f"\n=== {title}: {len(pairs)} pairs")
    print(f"{'band':5} {'label':>8} -> " + "  ".join(f"{d:>8}" for d in ("merge", "review", "distinct")))
    for band in sorted(table):
        for label in ("yes", "no", "unclear"):
            row = [table[band][(label, decision)] for decision in ("merge", "review", "distinct")]
            if any(row):
                print(f"{band:5} {label:>8} -> " + "  ".join(f"{value:8d}" for value in row))
    wrong = [p for p in pairs if (p.duplicate, verdicts[p.pair_id].decision) in (("yes", "distinct"), ("no", "merge"))]
    print(f"--- hard disagreements: {len(wrong)}")
    for pair in wrong:
        verdict = verdicts[pair.pair_id]
        print(
            f"    {pair.pair_id} [{pair.band}] label={pair.duplicate} decision={verdict.decision} "
            f"rule={verdict.rule}\n        {pair.a_name!r} || {pair.b_name!r}  ({pair.reason})"
        )


# --- the normalisation ladder ------------------------------------------------------------------------------


def test_ladder_is_ordered_and_idempotent() -> None:
    ladder = forms("  The  IL-10-mediated  T-cell   Responses (MBP) ")
    assert ladder.raw.startswith("  The")
    assert ladder.case == ladder.unicode.casefold()
    assert "(" not in ladder.punctuation and "  " not in ladder.punctuation
    assert ladder.lexical == forms(ladder.lexical).lexical, "entity_key must be a fixed point"
    assert ladder.parentheticals == ("mbp",)


def test_ladder_phases_each_do_their_own_job() -> None:
    assert forms("ALS patients").unicode == "ALS patients"  # NFKC: no-break space
    assert forms("Motor Neurons").case == "motor neurons"
    assert forms("T cells, subsets.").punctuation == "t cells subsets"
    assert forms("T-cells").lexical == forms("T cell").lexical  # the versioned dictionary


# --- stage 1: the rules that may merge ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "left,right,rule",
    [
        ("motor neuron", "motor neuron", "exact"),
        ("Motor Neurons ", "motor neurons", "normalized"),
        ("T-cells", "T cells", "lexical"),
        ("myelin basic protein (MBP)", "myelin basic protein", "lexical"),
        ("C9orf72-specific CD4+ T cells", "CD4+ C9orf72-specific T cells", "token_set"),
    ],
)
def test_merging_rules_fire_in_order(left: str, right: str, rule: str) -> None:
    verdict = compare(left, right)
    assert (verdict.decision, verdict.rule) == ("merge", rule)
    assert verdict.score == 1.0


@pytest.mark.parametrize(
    "left,right",
    [
        ("motor neuron damage", "motor neuron damage (reduced via c6)"),  # the parenthetical reverses the sign
        ("IL-10", "IL-10 (AAV spinal cord overexpression)"),  # an intervention delivering it
        ("autologous Treg infusion", "Autologous Treg infusion (with IL-2)"),  # a combination
        ("C9orf72 protein", "C9orf72 protein (loss of function)"),
    ],
)
def test_a_modifier_parenthetical_is_reviewed_not_merged(left: str, right: str) -> None:
    verdict = compare(left, right)
    assert verdict.decision == "review"
    assert verdict.rule == "lexical_parenthetical"


@pytest.mark.parametrize(
    "left,right,rule",
    [
        ("T cells", "CD8+ T cells", "containment"),
        ("antigen presenting cell", "activated myeloid antigen presenting cell", "containment"),
        ("IL-5", "IL-5 suppression", "containment"),
        ("C9orf72 epitope T cell reactivity", "C9orf72 epitope-specific T cell reactivity", "jaccard"),
    ],
)
def test_similarity_and_containment_never_merge(left: str, right: str, rule: str) -> None:
    """The measured reason: ordered containment would have merged 1021 of the base's pairs, a genus into a
    species each time. Similarity proposes; it does not decide."""
    verdict = compare(left, right)
    assert verdict.decision == "review"
    assert verdict.rule == rule


def test_jaccard_band_is_half_open_at_the_floor() -> None:
    assert (
        compare("IL-10 production by CD4+ T cells", "IL-10 production by C9orf72-specific CD4+ T cells").decision
        == "review"
    )
    far = compare("sargramostim", "motor neuron damage")
    assert (far.decision, far.rule) == ("distinct", "distinct")
    assert far.score <= REVIEW_FLOOR


def test_a_verdict_explains_itself() -> None:
    verdict = compare("T-cells", "T cells")
    assert verdict.rule and verdict.stage == "deterministic"
    assert verdict.left == verdict.right == "t cell"


# --- stage 1 against the labelled fixture ------------------------------------------------------------------


def test_deterministic_stage_over_the_real_fixture(pairs: list[Pair]) -> None:
    verdicts = {pair.pair_id: compare(pair.a_name, pair.b_name) for pair in pairs}
    _report("deterministic stage", pairs, verdicts)
    counts = Counter((pair.duplicate, verdicts[pair.pair_id].decision) for pair in pairs)

    # The one failure that matters: a pair labelled `no` that the stage merges would invent a graph edge.
    assert counts[("no", "merge")] == 0, "a labelled non-duplicate was merged"
    # And a labelled duplicate must never be declared distinct outright; review is allowed, silence is not.
    assert counts[("yes", "distinct")] == 0, "a labelled duplicate was declared distinct"
    # Recall of the easy half, measured: 42 of 51 labelled duplicates merge without review.
    assert counts[("yes", "merge")] >= 42
    # Every labelled non-duplicate is either refused or sent to review; none is silently kept apart by luck.
    assert counts[("no", "review")] + counts[("no", "distinct")] == 42


def test_the_bands_cover_every_rule(pairs: list[Pair]) -> None:
    """A fixture that exercised only the easy band would make the thresholds above meaningless."""
    rules = {compare(pair.a_name, pair.b_name).rule for pair in pairs}
    assert {"exact", "normalized", "lexical", "token_set"} & rules
    assert {"lexical_parenthetical", "jaccard", "containment"} <= rules
    assert {pair.band for pair in pairs} == {"A", "B", "C", "D1", "D2"}


def test_resolution_records_source_and_target(pairs: list[Pair]) -> None:
    entities = {}
    for pair in pairs:
        for identifier, name in ((pair.a_id, pair.a_name), (pair.b_id, pair.b_name)):
            entities[identifier] = entity(identifier, name)

    resolution = resolve(entities)
    assert set(resolution.assignment) == set(entities)
    for merge in resolution.merges:
        assert merge.source_id != merge.target_id
        assert resolution.assignment[merge.source_id] == merge.target_id
        assert merge.rule and merge.stage == "deterministic"
    # A survivor is a survivor: nothing points at an id that itself points elsewhere.
    for source, target in resolution.assignment.items():
        assert resolution.assignment[target] == target
    # The lowest record number survives, so the pass does not depend on iteration order.
    for target, members in resolution.groups.items():
        assert min(int(m.split("-")[1]) for m in members) == int(target.split("-")[1])
    print(
        f"\n=== resolution: {len(resolution.merges)} merges, "
        f"{len(resolution.groups)} groups, {len(resolution.open_reviews)} open reviews"
    )


def test_resolution_is_deterministic(pairs: list[Pair]) -> None:
    entities = {pair.a_id: entity(pair.a_id, pair.a_name) for pair in pairs}
    first, second = resolve(entities), resolve(dict(reversed(list(entities.items()))))
    assert first.assignment == second.assignment


# --- stage 2 ------------------------------------------------------------------------------------------------


def test_identity_review_decides_nothing() -> None:
    verdicts = identity_review([("a", "b"), ("c", "d")])
    assert [v.decision for v in verdicts] == ["review", "review"]
    assert all(v.stage == "review" for v in verdicts)


def test_identity_review_leaves_the_reviews_open(pairs: list[Pair]) -> None:
    entities = {pair.a_id: entity(pair.a_id, pair.a_name) for pair in pairs}
    resolution = resolve(entities, review=identity_review)
    assert all(verdict.decision == "review" for _, _, verdict in resolution.open_reviews)


@pytest.mark.skipif(REVIEW_BACKEND != "claude", reason="paid: set ALS_DEDUP_REVIEW=claude to run the second stage")
def test_claude_review_over_the_real_fixture(pairs: list[Pair]) -> None:
    """The second stage on the pairs stage 1 could not decide. Paid, and reported, not asserted into silence.

    The only hard assertion is the one that would be a defect in the pipeline rather than a weak answer: the
    stage must return one verdict per pair, in order, and must not merge a pair labelled `no`.
    """
    from knowledge_base.dedup_review import ClaudeReview

    open_pairs = [pair for pair in pairs if compare(pair.a_name, pair.b_name).decision == "review"]
    assert open_pairs, "stage 1 left nothing to review"

    verdicts = ClaudeReview()([(pair.a_name, pair.b_name) for pair in open_pairs])
    assert len(verdicts) == len(open_pairs)

    combined = {pair.pair_id: compare(pair.a_name, pair.b_name) for pair in pairs}
    combined.update({pair.pair_id: verdict for pair, verdict in zip(open_pairs, verdicts)})
    _report("deterministic + claude review", pairs, combined)

    counts = Counter((pair.duplicate, combined[pair.pair_id].decision) for pair in pairs)
    print(
        f"--- of {len(open_pairs)} reviewed: "
        f"{sum(1 for v in verdicts if v.decision == 'merge')} merged, "
        f"{sum(1 for v in verdicts if v.decision == 'distinct')} distinct, "
        f"{sum(1 for v in verdicts if v.decision == 'review')} still open"
    )
    assert counts[("no", "merge")] == 0, "the review stage merged a labelled non-duplicate"
