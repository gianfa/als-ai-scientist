"""The gate of the final proposal (outputs A-H)."""

from __future__ import annotations

from kb_factories import claim, hypothesis

from pipeline.proposals import SynthesizeOutput
from pipeline.synthesis import gate, normalize, normalize_tags, render


def proposal(text="See [[C-1]] and [[H-1]].", cites=("C-1",), outcome="proposal"):
    section = {"text": text, "cites": list(cites)}
    return SynthesizeOutput.model_validate(
        {
            "outcome": outcome,
            "summary": "s",
            **{
                k: section
                for k in (
                    "mechanism",
                    "therapeutic_hypothesis",
                    "intervention",
                    "stratification",
                    "biomarker",
                    "validation",
                    "translation",
                )
            },
            "failure_modes": [{"text": f"f{n} [[C-404]]" if n == 0 else f"f{n}", "cites": []} for n in range(5)],
        }
    )


KB = {"C-1": claim(id="C-1"), "H-1": hypothesis(id="H-1")}


def test_unknown_ids_fail_the_gate_including_tags_in_failure_modes():
    result = gate(proposal(text="[[C-99]] drives it", cites=()), KB, "RUN-1")
    assert not result["passed"]
    assert result["unknown_ids"]["mechanism"] == ["C-99"] and result["unknown_ids"]["failure_mode_1"] == ["C-404"]


def test_a_proposal_without_any_bought_experiment_is_warned():
    result = gate(proposal(), KB, "RUN-1")
    assert result["warnings"] and "no decision of this run bought" in result["warnings"][0]
    assert gate(proposal(outcome="insufficient_evidence"), KB, "RUN-1")["warnings"] == []


def test_the_rendered_proposal_carries_the_disclaimer_and_the_gate():
    out = proposal()
    text = render(out, gate(out, KB, "RUN-1"), "RUN-1")
    assert "nothing here is medical advice" in text and "## Gate" in text and "## A. Disease mechanism" in text


def test_data_support_does_not_license_a_runnable_now_claim():
    from kb_factories import experiment

    out = proposal(text="See [[EXP-1]].", cites=("EXP-1",))
    out.runnable_now_experiment_ids = ["EXP-1"]
    kb = KB | {"EXP-1": experiment()}
    result = gate(out, kb, "RUN-1")
    assert not result["passed"]
    assert result["runnable_now_without_handler"] == ["EXP-1"]
    rendered = render(out, result, "RUN-1", {"EXP-1": {"status": "data_supported", "reason": "marker present"}})
    assert "EXP-1: data_supported; not executed — marker present" in rendered
    assert "runnable now without verified handler: EXP-1" in rendered


def test_claiming_a_performed_experiment_without_a_receipt_fails_the_gate():
    from kb_factories import experiment

    out = proposal(text="Performed [[EXP-1]].", cites=("EXP-1",))
    out.performed_experiment_ids = ["EXP-1"]
    kb = KB | {"EXP-1": experiment()}
    refused = gate(out, kb, "RUN-1", execution={"status": "not_executed", "executions": []})
    assert not refused["passed"]
    assert refused["performed_without_receipt"] == ["EXP-1"]
    executed = {"status": "executed", "executions": [{"experiment_id": "EXP-1", "status": "executed"}]}
    accepted = gate(out, kb, "RUN-1", execution=executed)
    assert accepted["performed_without_receipt"] == []


def test_every_executed_experiment_may_be_called_performed_and_no_other():
    """A run may execute several EXPs, so the gate's rule is the receipt and not the count — but an
    EXP the run merely *selected*, and failed to execute, is still not performed."""
    from kb_factories import experiment

    out = proposal(text="Performed [[EXP-1]] and [[EXP-2]], and so did [[EXP-3]].", cites=("EXP-1", "EXP-2", "EXP-3"))
    out.performed_experiment_ids = ["EXP-1", "EXP-2", "EXP-3"]
    kb = KB | {eid: experiment(id=eid) for eid in ("EXP-1", "EXP-2", "EXP-3")}
    execution = {
        "status": "executed",
        "executions": [
            {"experiment_id": "EXP-1", "status": "executed"},
            {"experiment_id": "EXP-2", "status": "executed"},
            {
                "experiment_id": "EXP-3",
                "status": "not_executed",
                "reason": "the computational agent did not produce an artifact",
            },
        ],
    }
    result = gate(out, kb, "RUN-1", execution=execution)
    assert result["performed_without_receipt"] == ["EXP-3"]
    rendered = render(
        out,
        result,
        "RUN-1",
        {eid: {"status": "data_supported", "reason": "inputs present"} for eid in ("EXP-1", "EXP-2", "EXP-3")},
        execution=execution,
    )
    assert "EXP-1: data_supported; performed (verified receipt)" in rendered
    assert "EXP-2: data_supported; performed (verified receipt)" in rendered
    assert "EXP-3: data_supported; not executed" in rendered


def test_each_executed_experiment_gets_its_own_reassessment_subsection():
    """Two judgements merged into one would hide a disagreement between them."""
    out = proposal(text="Performed [[EXP-1]].", cites=("EXP-1",))
    reassessment = {
        "status": "judged",
        "judged_count": 2,
        "judgements": [
            {
                "experiment_id": "EXP-1",
                "status": "judged",
                "judgement": {"experiment_id": "EXP-1", "decision": "keep", "rationale": "The panel held."},
            },
            {
                "experiment_id": "EXP-2",
                "status": "judged",
                "judgement": {
                    "experiment_id": "EXP-2",
                    "decision": "reject",
                    "rationale": "The skew did not replicate.",
                },
            },
        ],
    }
    rendered = render(out, gate(out, KB, "RUN-1"), "RUN-1", reassessment=reassessment)
    assert "### EXP-1" in rendered and "### EXP-2" in rendered
    assert "Agent judgement: keep for EXP-1." in rendered
    assert "Agent judgement: reject for EXP-2." in rendered


def test_a_tag_with_several_ids_is_rewritten_into_one_tag_each():
    """The format is one ID per tag; `[[INT-1, INT-2]]` is a fault agents make."""
    text, rewrites = normalize_tags("D and E rest on [[INT-1, INT-2]] and on [[C-9 (contested)]].")
    assert text == "D and E rest on [[INT-1]] [[INT-2]] and on [[C-9]]."
    assert rewrites == ["[[INT-1, INT-2]] -> [[INT-1]] [[INT-2]]", "[[C-9 (contested)]] -> [[C-9]]"]


def test_a_well_formed_tag_and_an_idless_tag_are_left_alone():
    for text in ("rests on [[C-1]] alone", "as [[see above]] shows"):
        assert normalize_tags(text) == (text, [])


def test_normalization_reports_every_section_it_touched_and_the_gate_then_reads_the_ids():
    out = proposal(text="[[C-1, H-1]] together", cites=())
    rewritten = normalize(out)
    assert rewritten["mechanism"] == ["[[C-1, H-1]] -> [[C-1]] [[H-1]]"]
    assert "failure_mode_1" not in rewritten  # untouched sections are not reported
    result = gate(out, KB, "RUN-1")
    assert result["malformed_tags"] == {} and result["unknown_ids"] == {"failure_mode_1": ["C-404"]}


def test_a_tag_naming_no_id_fails_the_gate_and_is_named_in_it():
    out = proposal(text="it follows [[see the mechanism]]", cites=())
    assert normalize(out) == {}  # nothing to salvage: the PI's meaning is not invented
    result = gate(out, KB, "RUN-1")
    assert not result["passed"]
    assert result["malformed_tags"]["mechanism"] == ["[[see the mechanism]]"]
    assert "malformed tags in mechanism: [[see the mechanism]]" in render(out, result, "RUN-1")


def test_the_gate_counts_the_computed_claims_the_proposal_cites():
    """A proposal that cites none of the code-verified claims goes unnoticed unless it is counted."""
    from kb_factories import claim as make_claim

    kb = {
        **KB,
        "C-9": make_claim(
            id="C-9",
            content="a1: p_value = 0.01; n = 100; Monte Carlo.",
            origin="computation",
            verification_method="code",
            source_refs=["dataset:x@abc", "result:a1@def"],
        ),
    }
    without = gate(proposal(text="rests on [[C-1]]", cites=()), kb, "RUN-1")
    assert without["computed_claims_cited"] == []
    with_computed = gate(proposal(text="the measured value is [[C-9]]", cites=()), kb, "RUN-1")
    assert with_computed["computed_claims_cited"] == ["C-9"]
    out = proposal(text="the measured value is [[C-9]]", cites=())
    assert "computed claims cited: C-9" in render(out, gate(out, kb, "RUN-1"), "RUN-1")


def test_the_gate_names_a_citation_its_own_verifier_called_unfaithful():
    """`faithful: false` is the verifier answering "and no more?" — the question the citation audit asks. A
    run can flag an overstating citation itself and still say nothing about it in the proposal."""
    from kb_factories import claim, relation

    from pipeline.synthesis import gate, render, unfaithful_sources

    records = {
        "C-1": claim(id="C-1"),
        "C-2": claim(id="C-2", content="A target."),
        "L-1": relation(
            id="L-1",
            source_id="C-1",
            target_id="C-2",
            justification_method="agent_inference",
            provenance_status="unverified",
        ),
    }
    verdicts = {"L-1": {"verdict": "insufficient", "faithful": False, "reason": "the claim adds a magnitude"}}

    assert unfaithful_sources(verdicts, records) == {"C-1": ["L-1 (insufficient)"]}

    output = proposal(text="The mechanism rests on [[C-1]] and [[C-2]].", cites=("C-1", "C-2"))
    result = gate(output, records, "RUN-1", verdicts)

    # the fixture repeats one section in all seven, so the flag is named in each place the claim is cited
    assert result["cites_judged_unfaithful"]["mechanism"] == ["C-1 (judged unfaithful: L-1 (insufficient))"]
    assert len(result["cites_judged_unfaithful"]) == 7
    assert "C-2" not in str(result["cites_judged_unfaithful"])  # the target was not the one questioned
    # it warns; it never rejects. The fixture fails the gate on its own unknown tag (C-404 in a failure mode),
    # so the point is made with that tag resolvable: the flag alone leaves the gate passing.
    assert result["passed"] is False and result["unknown_ids"]
    known = records | {"C-404": claim(id="C-404", content="A resolvable claim.")}
    clean = gate(output, known, "RUN-1", verdicts)
    assert clean["passed"] is True and clean["cites_judged_unfaithful"]
    assert "cites judged unfaithful in mechanism" in render(output, clean, "RUN-1")


def test_the_gate_names_the_claim_an_unfaithful_citation_was_narrowed_to():
    """A run cited C-19 while its narrowing C-95 was on record: the warning now says which to cite, and
    it still does not fail the gate (one verifier's flag)."""
    from kb_factories import claim, relation

    from pipeline.synthesis import gate

    quote = "Responses to the C9orf72 pool were selectively increased in ALS."
    records = {
        r.id: r
        for r in (
            claim(id="C-19", content="Increased for C9orf72, absent for TDP-43.", quote=quote),
            claim(id="C-95", content="Selectively increased for C9orf72.", quote=quote),
            claim(id="C-2", content="A target."),
            relation(id="L-35", source_id="C-19", target_id="C-2"),
            relation(id="L-248", type="derived_from", source_id="C-95", target_id="C-19"),
            claim(id="C-404", content="Resolvable."),
        )
    }
    verdicts = {"L-35": {"verdict": "confirmed", "faithful": False}}
    output = proposal(text="Rests on [[C-19]].", cites=("C-19",))
    result = gate(output, records, "RUN-1", verdicts)
    assert result["cites_judged_unfaithful"]["mechanism"] == [
        "C-19 (judged unfaithful: L-35 (confirmed); narrowed to C-95)"
    ]
    assert result["passed"] is gate(output, records, "RUN-1")["passed"]


def test_a_faithful_verdict_and_a_missing_one_flag_nothing():
    from kb_factories import claim, relation

    from pipeline.synthesis import gate, unfaithful_sources

    records = {
        "C-1": claim(id="C-1"),
        "C-2": claim(id="C-2", content="A target."),
        "L-1": relation(
            id="L-1",
            source_id="C-1",
            target_id="C-2",
            justification_method="agent_inference",
            provenance_status="unverified",
        ),
    }

    assert unfaithful_sources({"L-1": {"verdict": "confirmed", "faithful": True}}, records) == {}
    assert unfaithful_sources({"L-1": {"verdict": "confirmed"}}, records) == {}  # absent is not false
    assert unfaithful_sources({}, records) == {}

    output = proposal(text="Rests on [[C-1]].", cites=("C-1",))
    assert gate(output, records, "RUN-1").get("cites_judged_unfaithful") == {}  # no verify phase, no claim


def test_the_gate_names_a_cited_claim_that_quotes_a_source_it_does_not_name():
    """The one weakness kind no tool of a run reports: `check_quote` is never called when there is no paper to
    look in, so a proposal can disclose every other weakness and stay silent on this one."""
    from kb_factories import claim

    from pipeline.synthesis import gate, quotes_an_unnamed_source, render

    orphan = claim(
        id="C-2",
        content="A quote with no source.",
        quote="a verbatim-looking sentence",
        origin="agent",
        provenance_status="unverified",
        verification_method="none",
        source_refs=[],
    )
    records = KB | {"C-2": orphan, "C-404": claim(id="C-404", content="Resolvable.")}

    assert quotes_an_unnamed_source(orphan)
    assert not quotes_an_unnamed_source(records["C-1"])  # a sourced quote is not the anomaly

    output = proposal(text="Rests on [[C-2]].", cites=("C-2",))
    result = gate(output, records, "RUN-1")

    assert result["cites_quoting_an_unnamed_source"]["mechanism"] == ["C-2"]
    assert result["passed"] is True  # a warning, never a rejection
    assert "cites quoting an unnamed source in mechanism" in render(output, result, "RUN-1")


def test_a_claim_with_no_quote_at_all_is_not_the_unnamed_source_anomaly():
    """Most claims of a proposal carry no quote: they are the system's own reasoning, which is a different
    weakness and is counted by the audit's support ladder, not here."""
    from kb_factories import claim

    from pipeline.synthesis import gate, quotes_an_unnamed_source

    reasoning = claim(
        id="C-2",
        content="Our own step.",
        origin="agent",
        provenance_status="unverified",
        verification_method="none",
        source_refs=[],
    )
    records = KB | {"C-2": reasoning, "C-404": claim(id="C-404", content="Resolvable.")}

    assert not quotes_an_unnamed_source(reasoning)
    assert gate(proposal(text="[[C-2]]", cites=("C-2",)), records, "RUN-1")["cites_quoting_an_unnamed_source"] == {}


def test_the_gate_names_a_cited_claim_whose_source_leads_nowhere():
    """Distinct from the unnamed-source anomaly: here there *is* a reference and it resolves to nothing.
    All three strings below are real `source_refs` from production runs — a literal search phrase, a
    claim identifier used as a source, and a news domain."""
    from kb_factories import claim

    from pipeline.synthesis import gate, render

    records = KB | {
        "C-404": claim(id="C-404", content="Resolvable."),
        "C-2": claim(
            id="C-2",
            content="Rests on a search.",
            origin="literature",
            provenance_status="unverified",
            verification_method="none",
            source_refs=["Web search results"],
        ),
        "C-3": claim(
            id="C-3",
            content="Rests on a claim id.",
            origin="literature",
            provenance_status="unverified",
            verification_method="none",
            source_refs=["C-111"],
        ),
        "C-4": claim(
            id="C-4",
            content="Rests on a news site.",
            origin="literature",
            provenance_status="unverified",
            verification_method="none",
            source_refs=["alsnewstoday.com (ALS News Today)"],
        ),
    }
    result = gate(proposal(text="On [[C-2]], [[C-3]] and [[C-4]].", cites=("C-2", "C-3", "C-4")), records, "RUN-1")

    flagged = result["cites_an_unresolvable_source"]["mechanism"]
    assert [f.split(" ", 1)[0] for f in flagged] == ["C-2", "C-3", "C-4"]
    assert "Web search results" in flagged[0]  # the string itself, so a reader can judge it
    assert result["passed"] is True  # a warning for a human, never a rejection
    assert "cites an unresolvable source in mechanism" in render(
        result and proposal(text="On [[C-2]].", cites=("C-2",)), result, "RUN-1"
    )


def test_a_canonical_identifier_is_not_flagged_as_unresolvable():
    from kb_factories import claim

    from pipeline.synthesis import gate

    records = KB | {
        "C-404": claim(id="C-404", content="Resolvable."),
        "C-2": claim(
            id="C-2",
            content="Properly sourced.",
            origin="literature",
            provenance_status="unverified",
            verification_method="none",
            source_refs=["doi:10.1038/s41586-025-09588-6", "receipt:abc"],
        ),
    }
    assert gate(proposal(text="[[C-2]]", cites=("C-2",)), records, "RUN-1")["cites_an_unresolvable_source"] == {}


def test_the_proposal_carries_the_causal_chains_the_host_read_from_the_knowledge_base():
    """The section is written by the host beside the PI's prose; without chains the document has none."""
    from kb_factories import claim

    from knowledge_base.chains import causal_chains
    from pipeline.synthesis import gate, render

    records = {"C-1": claim(id="C-1", content="A plain observation.")}
    output = proposal(text="Cites [[C-1]].", cites=("C-1",))
    result = gate(output, records, "RUN-1")

    assert "## Causal chains" not in render(output, result, "RUN-1")
    text = render(output, result, "RUN-1", chains=causal_chains(records, "RUN-1", output.model_dump()))
    assert "## Causal chains" in text and "Written deterministically from the knowledge base" in text
    assert "no live hypothesis" in text
    assert text.index("## H. Failure modes") < text.index("## Causal chains")
