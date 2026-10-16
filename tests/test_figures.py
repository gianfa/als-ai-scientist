"""The diagnostic figures must draw what the scorecard says and nothing else.

What is worth testing here is not how a bar looks but the two ways a figure could lie: inventing a
value for a run that does not record one, and silently drawing an empty chart when a measurement is
absent. Rendering itself is checked only for "a non-empty PNG appeared".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("matplotlib", reason="figures are an optional extra: uv sync --extra figures")

from evaluation import figures
from evaluation.figures import (  # noqa: E402
    FigureDataMissing,
    Scorecard,
    _fit,
    _segment_labels,
    candidate_classes,
    cost_by_phase,
    evidence_reach,
    mechanism_chains,
    quote_exact_rate,
    quote_outcomes_by_stage,
    render_all,
    verify_verdicts,
)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _card(run_id: str, payload: dict) -> Scorecard:
    return Scorecard(run_id=run_id, data={"run_id": run_id, **payload})


def _full(run_id: str = "pl-test-001") -> Scorecard:
    return _card(
        run_id,
        {
            "cost": {"total_usd": 3.0, "stages": {"03-literature": {"usd": 1.0}, "06-verify": {"usd": 2.0}}},
            "verify": {"counts": {"confirmed": 3, "rejected": 2, "insufficient": 5}},
            "quote_checks": {"literature": {"calls": 10, "judged": 8, "exact": 6, "exact_rate_of_judged": 0.75}},
            "evidence_reach": {"fresh_claims": 20, "touched_by_any_later_phase": 7, "touched_by_authors": 7},
            "citations": {"claims_cited": 9},
            "mechanism_chain": {
                "cited_causal": 2,
                "chain": ["C-1", "C-2"],
                "chain_steps": [
                    {
                        "claim_id": "C-1",
                        "cause_ref": "N-1",
                        "cause_name": "a",
                        "effect_ref": "N-2",
                        "effect_name": "b",
                        "origin": "literature",
                        "provenance_status": "verified",
                    },
                    {
                        "claim_id": "C-2",
                        "cause_ref": "N-2",
                        "cause_name": "b",
                        "effect_ref": "N-3",
                        "effect_name": "c",
                        "origin": "agent",
                        "provenance_status": "unverified",
                    },
                ],
            },
            "verify_candidate_classes": {
                "source_verified": {"candidates": 10, "unfaithful": 5, "share_of_pool": 1.0, "unfaithful_rate": 0.5}
            },
            "quote_outcomes": {
                "legs": {
                    "agents": {
                        "stage_source": "recorded",
                        "checks": 10,
                        "by_stage": {
                            "03-literature": {"checked:exact": 6, "checked:absent": 1, "paper_not_in_store": 3}
                        },
                        "totals": {"checked:exact": 6, "checked:absent": 1, "paper_not_in_store": 3},
                        "paper_not_in_store_resolvable": {"03-literature": 2},
                        "paper_not_in_store_resolvable_total": 2,
                    }
                },
                "fetches": {"by_stage": {"literature": {"fetched": 1}}, "totals": {"fetched": 1}, "attempts": 1},
            },
        },
    )


def test_scorecard_load_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "run-score.json"
    path.write_text(json.dumps({"run_id": "pl-test-001", "cost": {"total_usd": 1.5}}))
    card = Scorecard.load(path)
    assert card.run_id == "pl-test-001"
    assert card.get("cost", "total_usd") == 1.5


def test_scorecard_without_run_id_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "run-score.json"
    path.write_text(json.dumps({"cost": {}}))
    with pytest.raises(ValueError, match="run_id"):
        Scorecard.load(path)


def test_missing_field_is_none_not_zero() -> None:
    """The distinction the figures rest on: absent is drawn as absent, never as a zero bar."""
    card = _card("pl-test-002", {"cost": {"total_usd": 1.0}})
    assert card.get("verify", "counts") is None
    assert card.get("cost", "stages", "06-verify") is None


@pytest.mark.parametrize(
    "fn", [cost_by_phase, verify_verdicts, quote_exact_rate, evidence_reach, candidate_classes, quote_outcomes_by_stage]
)
def test_each_figure_writes_a_png(fn, tmp_path: Path) -> None:
    path = fn([_full()], tmp_path)
    assert Path(path).read_bytes()[:8] == PNG_MAGIC


@pytest.mark.parametrize(
    "fn", [cost_by_phase, verify_verdicts, quote_exact_rate, evidence_reach, candidate_classes, quote_outcomes_by_stage]
)
def test_a_figure_with_no_data_raises_instead_of_drawing_nothing(fn, tmp_path: Path) -> None:
    with pytest.raises(FigureDataMissing):
        fn([_card("pl-test-003", {})], tmp_path)


def test_two_runs_share_one_figure(tmp_path: Path) -> None:
    report = render_all([_full("pl-test-001"), _full("pl-test-002")], tmp_path)
    assert len(report) == len(figures.FIGURES) + 1  # every figure plus report.txt
    assert not any(v.startswith("skipped") for v in report.values())
    assert len(list(tmp_path.glob("*.png"))) == len(figures.FIGURES)


def test_a_run_missing_one_measurement_skips_only_that_figure(tmp_path: Path) -> None:
    thin = _card("pl-test-004", {"cost": {"total_usd": 1.0, "stages": {"03-literature": {"usd": 1.0}}}})
    report = render_all([thin], tmp_path)
    assert not report["cost_by_phase"].startswith("skipped")
    for name in ("verify_verdicts", "quote_exact_rate", "candidate_classes"):
        assert report[name].startswith("skipped"), name


def test_a_class_one_run_never_had_is_not_drawn_as_zero(tmp_path: Path) -> None:
    """An archive written before the class existed must read `source_quoted_unverified` as absent."""
    widened = _full("pl-test-005")
    widened.data["verify_candidate_classes"]["source_quoted_unverified"] = {
        "candidates": 9,
        "unfaithful": 6,
        "share_of_pool": 0.47,
        "unfaithful_rate": 0.67,
    }
    path = candidate_classes([_full("pl-test-004"), widened], tmp_path)
    assert Path(path).read_bytes()[:8] == PNG_MAGIC


def test_more_runs_than_categorical_slots_is_refused(tmp_path: Path) -> None:
    """A fourth run must be facetted, not given a generated hue."""
    cards = [_full(f"pl-test-{i}") for i in range(4)]
    with pytest.raises(ValueError, match="facet"):
        cost_by_phase(cards, tmp_path)


def test_render_all_writes_report_txt_beside_the_figures(tmp_path: Path) -> None:
    report = render_all([_full()], tmp_path, cap_usd=24.0, quote_threshold=0.70)
    path = Path(report["report_txt"])
    assert path.name == "report.txt" and path.parent == tmp_path
    text = path.read_text()
    for name in (
        "fig1-cost-by-phase.png",
        "fig2-verify-verdicts.png",
        "fig3-quote-exact-rate.png",
        "fig4-evidence-reach.png",
        "fig5-candidate-classes.png",
    ):
        assert name in text, name
    for field in ("Shows:", "Axes:", "Desirable:", "Observed:"):
        assert field in text, field


def test_report_states_the_axis_direction_of_every_figure(tmp_path: Path) -> None:
    """Each plot says what its axes are and which way is desirable."""
    text = Path(render_all([_full()], tmp_path)["report_txt"]).read_text()
    # The chain figure has no numeric axis, and its note must say so rather than invent a direction.
    assert text.count("increasing rightwards") == len(figures.FIGURES) - 1
    assert "No numeric axes" in figures.NOTES["fig8-mechanism-chains.png"]["axes"]


def test_report_numbers_come_from_the_scorecard(tmp_path: Path) -> None:
    """The observed lines are arithmetic over the persisted fields, so they must quote them."""
    text = Path(render_all([_full()], tmp_path, cap_usd=24.0)["report_txt"]).read_text()
    assert "3.000 USD" in text  # cost.total_usd
    assert "10 offered" in text  # the verify pool, 3 + 2 + 5
    assert "insufficient 5 (50%)" in text  # computed share, not a stored field
    assert "Cap in force: 24 USD." in text
    assert "Reuse: 7/20 = 0.35; Reliance: 7/9 = 0.78" in text  # the two nested evidence-reach ratios


def test_report_marks_the_floor_side_only_when_a_floor_was_given(tmp_path: Path) -> None:
    with_floor = Path(render_all([_full()], tmp_path / "a", quote_threshold=0.70)["report_txt"]).read_text()
    assert "(at or above the floor)" in with_floor  # the fixture's 0.75 clears 0.70
    without = Path(render_all([_full()], tmp_path / "b")["report_txt"]).read_text()
    assert "the floor" not in without.split("Observed:")[3]


def test_report_names_a_figure_that_could_not_be_drawn(tmp_path: Path) -> None:
    thin = _card("pl-test-006", {"cost": {"total_usd": 1.0, "stages": {"03-literature": {"usd": 1.0}}}})
    text = Path(render_all([thin], tmp_path)["report_txt"]).read_text()
    assert "NOT RENDERED" in text
    # The thin card carries only `cost.stages`, which two figures read (the cost one and the phase
    # sequence); every other figure is named as not rendered. A formula, not a literal, so adding a
    # figure cannot silently satisfy this count.
    assert text.count("NOT RENDERED") == len(figures.FIGURES) - 2


# ---- the unfaithful rate must carry its denominator -------------------------------------------------


def _with_verify_agents(run_id: str, agents: int | None) -> Scorecard:
    card = _full(run_id)
    stage = card.data["cost"]["stages"]["06-verify"]
    if agents is None:
        stage.pop("agents", None)
    else:
        stage["agents"] = agents
    return card


def test_the_unfaithful_rate_is_reported_with_the_agents_that_judged_it(tmp_path: Path) -> None:
    render_all([_with_verify_agents("pl-many", 28)], tmp_path)
    report = (tmp_path / "report.txt").read_text()
    assert "judged by 28 verifier agents" in report
    assert "NOT COMPARABLE as a rate" not in report  # the caveat in `desirable` is not the flag


def test_a_rate_from_too_few_agents_is_marked_not_comparable(tmp_path: Path) -> None:
    render_all([_with_verify_agents("pl-two", 2)], tmp_path)
    report = (tmp_path / "report.txt").read_text()
    assert "judged by 2 verifier agents" in report
    assert "NOT COMPARABLE as a rate" in report and "2 verifier agent(s)" in report
    assert "0.00 to 1.00" in report  # the spread that makes it not comparable


def test_an_unrecorded_agent_count_is_said_not_assumed(tmp_path: Path) -> None:
    render_all([_with_verify_agents("pl-silent", None)], tmp_path)
    report = (tmp_path / "report.txt").read_text()
    assert "verifier agents not recorded" in report
    assert "NOT COMPARABLE as a rate" not in report  # absent is not the same as too few


# ---- the quote-outcome stack: its grouping must be exhaustive ---------------------------------------


def test_the_five_groups_cover_every_outcome_the_vocabulary_allows() -> None:
    """A stack whose segments do not partition the calls lies about the total, so no outcome of
    `evaluation.quote_outcomes`'s vocabulary may fall outside the five groups."""
    from evaluation.quote_outcomes import OUTCOMES

    grouped = {key for _, keys in figures.OUTCOME_GROUPS for key in keys}
    assert grouped == set(OUTCOMES)
    assert len(figures.OUTCOME_GROUPS) == len(figures.OUTCOME_RAMP)


def test_an_outcome_the_grouping_does_not_know_joins_other_rather_than_vanishing() -> None:
    counts = {"checked:exact": 2, "something_new": 3}
    grouped = figures._grouped(counts)
    assert sum(grouped.values()) == 5, "the stack must still add up to the calls made"
    assert grouped["declined: other"] == 3


def test_the_stack_reads_one_leg_and_never_sums_them(tmp_path: Path) -> None:
    """The host verifier and the agents are different callers; a figure that added them would be wrong."""
    card = _full()
    card.data["quote_outcomes"]["legs"]["host"] = {
        "stage_source": "inferred_from_timestamps",
        "checks": 99,
        "by_stage": {"03-literature": {"checked:exact": 99}},
        "totals": {"checked:exact": 99},
        "paper_not_in_store_resolvable": {},
        "paper_not_in_store_resolvable_total": 0,
    }
    text = Path(render_all([card], tmp_path)["report_txt"]).read_text()
    assert "10 agent calls" in text and "99" not in text.split("fig7")[1]
    assert quote_outcomes_by_stage([card], tmp_path, leg="host").is_file()


def _captured(monkeypatch: pytest.MonkeyPatch) -> list:
    """Record each figure as it is saved, so a test can read where its texts landed."""
    seen: list = []
    original = figures._save

    def keep(fig, out_dir, name):
        seen.append(fig.axes[0])
        return original(fig, out_dir, name)

    monkeypatch.setattr(figures, "_save", keep)
    return seen


def _texts_outside_rows(ax) -> list[str]:
    """Value labels and notes placed in data coordinates; the subtitle sits above the axes by design."""
    low, high = sorted(ax.get_ylim())
    in_data = [t for t in ax.texts if t.get_transform() == ax.transData]
    return [t.get_text() for t in in_data if not low <= t.get_position()[1] <= high]


def test_a_class_every_other_run_lacks_keeps_its_note_inside_the_axes(tmp_path: Path, monkeypatch) -> None:
    """pl-prod-014..016: one run had `source_quoted_unverified`; the other two notes fell below the axis."""
    only_one = _full("pl-test-006")
    only_one.data["verify_candidate_classes"]["source_quoted_unverified"] = {
        "candidates": 3,
        "unfaithful": 3,
        "share_of_pool": 0.06,
        "unfaithful_rate": 1.0,
    }
    seen = _captured(monkeypatch)
    candidate_classes([only_one, _full("pl-test-007"), _full("pl-test-008")], tmp_path)
    (ax,) = seen
    assert sum("class did not exist" in t.get_text() for t in ax.texts) == 2
    assert _texts_outside_rows(ax) == []


def test_a_phase_absent_from_a_run_keeps_its_note_inside_the_axes(tmp_path: Path, monkeypatch) -> None:
    """The notes of the runs drawn after the only run with the phase are the ones that used to fall off."""
    first = _full("pl-test-009")
    first.data["quote_checks"]["review"] = {"calls": 4, "judged": 4, "exact": 1, "exact_rate_of_judged": 0.25}
    seen = _captured(monkeypatch)
    quote_exact_rate([first, _full("pl-test-010"), _full("pl-test-011")], tmp_path)
    (ax,) = seen
    assert _texts_outside_rows(ax) == []


def test_every_nonzero_verdict_is_labelled_exactly_once() -> None:
    counts = {"confirmed": 1, "rejected": 30, "insufficient": 29}
    inline, overflow = _segment_labels(counts, span=60 * 1.22)
    assert sorted(inline + overflow) == sorted(counts)
    assert overflow == ["confirmed"]


def test_a_small_segment_on_a_wide_axis_keeps_its_count(tmp_path: Path, monkeypatch) -> None:
    """pl-prod-015: 3 confirmed of 38 next to a 51-relation bar lost its label to a share-of-bar test."""
    wide = _card("pl-test-011", {"verify": {"counts": {"confirmed": 19, "rejected": 13, "insufficient": 19}}})
    narrow = _card("pl-test-012", {"verify": {"counts": {"confirmed": 3, "rejected": 16, "insufficient": 19}}})
    seen = _captured(monkeypatch)
    verify_verdicts([wide, narrow], tmp_path)
    (ax,) = seen
    labels = [t.get_text() for t in ax.texts]
    assert "3" in labels or any("confirmed 3" in label for label in labels)


def _chain_card(run_id: str, length: int, verified: tuple[int, ...] = (0,)) -> Scorecard:
    steps = [
        {
            "claim_id": f"C-{i + 1}",
            "cause_ref": f"N-{i}",
            "cause_name": f"entity {i}",
            "effect_ref": f"N-{i + 1}",
            "effect_name": f"entity {i + 1}",
            "origin": "literature" if i in verified else "agent",
            "provenance_status": "verified" if i in verified else "unverified",
        }
        for i in range(length)
    ]
    return _card(
        run_id,
        {"mechanism_chain": {"cited_causal": length, "chain": [s["claim_id"] for s in steps], "chain_steps": steps}},
    )


def _figures_saved(monkeypatch: pytest.MonkeyPatch) -> list:
    seen: list = []
    original = figures._save

    def keep(fig, out_dir, name):
        seen.append(fig)
        return original(fig, out_dir, name)

    monkeypatch.setattr(figures, "_save", keep)
    return seen


def test_the_chain_figure_takes_four_runs_and_refuses_a_fifth(tmp_path: Path) -> None:
    four = [_chain_card(f"pl-test-{i}", 2) for i in range(4)]
    assert Path(mechanism_chains(four, tmp_path)).read_bytes()[:8] == PNG_MAGIC
    with pytest.raises(ValueError, match="at most 4"):
        mechanism_chains(four + [_chain_card("pl-test-9", 2)], tmp_path)


def test_every_box_has_one_size_and_step_k_sits_on_one_row_in_every_panel(tmp_path: Path, monkeypatch) -> None:
    seen = _figures_saved(monkeypatch)
    mechanism_chains([_chain_card("pl-test-1", 2), _chain_card("pl-test-2", 5)], tmp_path)
    (fig,) = seen
    short, long = fig.axes
    sizes = {(round(p.get_width(), 6), round(p.get_height(), 6)) for ax in fig.axes for p in ax.patches}
    assert len(sizes) == 1
    assert short.get_ylim() == long.get_ylim()
    rows = lambda ax: [round(p.get_y(), 6) for p in ax.patches]  # noqa: E731
    assert rows(short) == rows(long)[: len(rows(short))]


def test_a_long_name_is_cut_with_an_ellipsis_and_a_short_one_is_left_alone() -> None:
    long = "a very long entity name that goes on well beyond what two lines of a box can hold at all"
    cut = _fit(long, width=20, lines=2)
    assert cut.endswith("...") and len(cut.split("\n")) == 2
    assert all(len(line) <= 20 for line in cut.split("\n"))
    assert _fit("survival time", width=20, lines=2) == "survival time"


def test_the_chain_figure_carries_its_legend_above_the_panels(tmp_path: Path, monkeypatch) -> None:
    seen = _figures_saved(monkeypatch)
    mechanism_chains([_chain_card("pl-test-1", 3, verified=(1,))], tmp_path)
    (fig,) = seen
    (legend,) = fig.legends
    labels = [t.get_text() for t in legend.get_texts()]
    assert [label.split(":")[0] for label in labels] == ["verified", "unverified source", "agent inference"]
    # No template placeholder survives into the figure (an f-prefix lost in an edit printed "{ELLIPSIS}").
    assert not any("{" in t.get_text() for t in fig.texts)
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    assert legend.get_window_extent(renderer).y0 > max(ax.get_window_extent(renderer).y1 for ax in fig.axes)


def test_a_scorecard_without_chain_steps_is_said_not_drawn_empty(tmp_path: Path, monkeypatch) -> None:
    """A scorecard written before `chain_steps` existed gets a note in its panel, not a blank column."""
    old = _card("pl-test-old", {"mechanism_chain": {"cited_causal": 2, "chain": ["C-1", "C-2"]}})
    seen = _figures_saved(monkeypatch)
    mechanism_chains([_chain_card("pl-test-new", 2), old], tmp_path)
    (fig,) = seen
    assert any("re-score" in t.get_text() for t in fig.axes[1].texts)
    with pytest.raises(FigureDataMissing):
        mechanism_chains([old], tmp_path)


def test_four_runs_render_the_chain_figure_and_skip_the_coloured_ones(tmp_path: Path) -> None:
    cards = [_full(f"pl-test-{i}") for i in range(4)]
    for card, chain in zip(cards, [_chain_card("x", 2) for _ in range(4)]):
        card.data["mechanism_chain"] = chain.data["mechanism_chain"]
    report = render_all(cards, tmp_path)
    assert not report["mechanism_chains"].startswith("skipped")
    assert report["cost_by_phase"].startswith("skipped") and "at most 3" in report["cost_by_phase"]
    assert "fig8-mechanism-chains.png" in (tmp_path / "report.txt").read_text()


def test_each_arrow_names_its_kind_and_no_arrow_carries_a_sign(tmp_path: Path, monkeypatch) -> None:
    """ADR 0032: the sign is gone; the label says verified, unverified or agent inference, so colour is not the only cue."""
    card = _chain_card("pl-test-1", 3, verified=(0,))
    card.data["mechanism_chain"]["chain_steps"][1]["origin"] = "literature"  # quoted, not verified
    card.data["mechanism_chain"]["chain_steps"][0]["sign"] = "positive"  # a legacy scorecard field, ignored
    seen = _figures_saved(monkeypatch)
    mechanism_chains([card], tmp_path)
    (fig,) = seen
    labels = [t.get_text() for t in fig.axes[0].texts if t.get_text().startswith("C-")]
    assert labels == ["C-1  verified", "C-2  unverified", "C-3  agent inference"]
    assert not any("positive" in t.get_text() or "negative" in t.get_text() for t in fig.axes[0].texts)
