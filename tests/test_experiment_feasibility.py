"""Declared experiment inputs against the run's recorded dataset inventory."""

from kb_factories import experiment

from analysis.experiment_runner import HANDLERS
from pipeline.experiment_feasibility import assess

INPUT_IDS = HANDLERS["hla_panel_compact_v1"].input_ids


MANIFEST = {
    "sources": [
        {
            "id": "cytometry",
            "ref": "dataset:cytometry@abc123",
            "sha256": "abc123",
            "shape": {"markers": ["CD4", "IL-4", "IL-10", "IL-17"]},
        }
    ],
    "absent": [],
}


def proposed(**overrides):
    fields = {"kind": "in_silico", "dataset_refs": ["dataset:cytometry@abc123"], "dataset_location": "local"}
    return experiment(**(fields | overrides))


def test_missing_markers_are_named_and_outweigh_unknown_metadata():
    row = assess(proposed(required_markers=["FoxP3", "IL-5"], required_metadata=["disease_stage"]), MANIFEST)
    assert row["status"] == "missing_inputs"
    assert row["missing"] == ["marker:FoxP3", "marker:IL-5"]
    assert row["unknown"] == ["metadata:disease_stage has no inventory"]
    assert row["sources"] == [{"id": "cytometry", "ref": "dataset:cytometry@abc123", "sha256": "abc123"}]


def test_present_marker_is_data_supported_only():
    row = assess(proposed(required_markers=["IL-4", "IL-10"]), MANIFEST)
    assert row["status"] == "data_supported"
    assert row["matched"] == ["marker:IL-4", "marker:IL-10"]
    assert "analysis code is not implied" in row["reason"]


def test_unknown_inventory_and_undeclared_requirements_never_pass():
    assert assess(proposed(), MANIFEST)["status"] == "unverified"
    assert assess(proposed(required_metadata=["disease_stage"]), MANIFEST)["status"] == "unverified"
    assert assess(proposed(required_markers=["IL-10"]), None)["status"] == "unverified"


def test_missing_dataset_and_version_mismatch_are_visible():
    missing = proposed(dataset_refs=["dataset:absent@123"], required_markers=["IL-10"])
    row = assess(missing, {**MANIFEST, "absent": [{"id": "absent"}]})
    assert row["status"] == "missing_inputs" and "dataset:absent" in row["missing"]
    stale = proposed(dataset_refs=["dataset:cytometry@old"], required_markers=["IL-10"])
    assert "version differs" in assess(stale, MANIFEST)["reason"]


def test_legacy_experiment_is_not_misread_as_ready():
    row = assess(experiment(), MANIFEST)
    assert row["status"] == "not_applicable"


def test_registered_handler_requires_its_exact_manifest_inputs():
    sources = [{"id": sid, "ref": f"dataset:{sid}@abc123", "sha256": "abc123"} for sid in INPUT_IDS]
    record = proposed(analysis_id="hla_panel_compact_v1", dataset_refs=[entry["ref"] for entry in sources])
    row = assess(record, {"sources": sources, "absent": []})
    assert row["status"] == "data_supported"
    assert row["matched"] == ["registered_inputs:hla_panel_compact_v1"]
    assert assess(record, {"sources": sources[:2], "absent": []})["status"] == "unverified"
    wrong = proposed(analysis_id="hla_panel_compact_v1", dataset_refs=[sources[0]["ref"]])
    assert assess(wrong, {"sources": sources, "absent": []})["status"] == "missing_inputs"


IFN_MANIFEST = {
    "sources": [
        {
            "id": "cytometry",
            "ref": "dataset:cytometry@abc123",
            "sha256": "abc123",
            "shape": {"markers": ["CD4", "IFNY", "IL-10"]},
        }
    ],
    "absent": [],
}


def test_a_spelling_variant_of_a_measured_marker_is_not_reported_missing():
    """The inventory is stored canonicalised, so a raw comparison called a measured marker absent."""
    for spelling in ("IL-10", "il-10", "Il10", "il10"):
        row = assess(proposed(required_markers=[spelling]), IFN_MANIFEST)
        assert row["status"] == "data_supported", spelling
        assert row["matched"] == [f"marker:{spelling}"]
        assert row["missing"] == []


def test_an_interferon_subtype_is_neither_matched_nor_called_absent():
    """`marker_family` collapses every interferon, so a resemblance must not license `data_supported` and must
    not be reported as absence either. The row says what the panel records instead."""
    row = assess(proposed(required_markers=["IFN-gamma"]), IFN_MANIFEST)
    assert row["status"] == "unverified"
    assert row["matched"] == [] and row["missing"] == []
    assert row["unknown"] == [
        "marker:IFN-gamma resembles IFNY in the recorded panel, which does not "
        "distinguish the subtype; declare the name the inventory uses"
    ]
    assert assess(proposed(required_markers=["IFN-alpha"]), IFN_MANIFEST)["status"] == "unverified"


def test_a_marker_the_panel_does_not_measure_is_still_missing():
    """The audit's two findings must survive the normalisation: FoxP3 and IL-5 are absent, not misspelled."""
    row = assess(proposed(required_markers=["FoxP3", "IL-5", "il-10"]), IFN_MANIFEST)
    assert row["status"] == "missing_inputs"
    assert row["missing"] == ["marker:FoxP3", "marker:IL-5"]
    assert row["matched"] == ["marker:il-10"]


# --- metadata_fields: a real gap, and its fix -----------------------------------------------------

METADATA_MANIFEST = {
    "sources": [
        {
            "id": "cytometry",
            "ref": "dataset:cytometry@abc123",
            "sha256": "abc123",
            "shape": {
                "markers": [],
                "metadata_fields": ["fcs_file_name_group_label", "fcs_file_name_condition_label", "flowjo_donor_group"],
            },
        }
    ],
    "absent": [],
}


def test_free_prose_metadata_can_never_match_a_machine_generated_inventory():
    """The designers of a real run wrote free prose ("donor disease-group label (ALS vs healthy control)"),
    which cannot equal a short inventory key even once one exists — this is the bug the prompt fix (below)
    closes, not something `assess()` itself should paper over with a fuzzy match."""
    row = assess(proposed(required_metadata=["donor disease-group label (ALS vs healthy control)"]), METADATA_MANIFEST)
    assert row["status"] == "missing_inputs"
    assert row["missing"] == ["metadata:donor disease-group label (ALS vs healthy control)"]


def test_the_exact_controlled_key_is_matched_and_reaches_data_supported():
    """The fix: a designer naming the exact key `data_describe` now reports (as for markers) resolves cleanly."""
    row = assess(proposed(required_metadata=["fcs_file_name_group_label", "flowjo_donor_group"]), METADATA_MANIFEST)
    assert row["status"] == "data_supported"
    assert row["matched"] == ["metadata:fcs_file_name_group_label", "metadata:flowjo_donor_group"]


def test_a_controlled_key_absent_from_this_deposit_is_reported_missing_not_unknown():
    thin = {
        "sources": [
            {
                "id": "cytometry",
                "ref": "dataset:cytometry@abc123",
                "sha256": "abc123",
                "shape": {"markers": [], "metadata_fields": ["fcs_file_name_group_label"]},
            }
        ],
        "absent": [],
    }
    row = assess(proposed(required_metadata=["flowjo_donor_group"]), thin)
    assert row["status"] == "missing_inputs"
    assert row["missing"] == ["metadata:flowjo_donor_group"]


def test_a_source_with_no_metadata_fields_inventory_at_all_stays_unknown():
    """A non-cytometry source, or one `read_panel` never ran on, still gets the honest `unknown` it had before —
    this fix does not turn every unrelated source's missing inventory into a false `missing`."""
    row = assess(proposed(required_metadata=["flowjo_donor_group"]), MANIFEST)
    assert row["status"] == "unverified"
    assert row["unknown"] == ["metadata:flowjo_donor_group has no inventory"]


def test_a_second_dataset_ref_with_no_inventory_does_not_blank_out_the_first():
    """The shape of a real run: `ics_cytokine_skew_v1` always names the cytometry deposit *and* the
    source-data spreadsheet. Before this fix, `len(inventories) != 1` fired as soon as a second source was
    named at all, so `metadata_fields` could never resolve for any EXP naming two sources — independent of
    whether the first cause (no `metadata_fields` key) was fixed."""
    two_sources = {
        "sources": [
            {
                "id": "cytometry",
                "ref": "dataset:cytometry@abc123",
                "sha256": "abc123",
                "shape": {"markers": [], "metadata_fields": ["flowjo_donor_group"]},
            },
            {"id": "source-data", "ref": "dataset:source-data@def456", "sha256": "def456"},
        ],
        "absent": [],
    }
    row = assess(
        proposed(
            dataset_refs=["dataset:cytometry@abc123", "dataset:source-data@def456"],
            required_metadata=["flowjo_donor_group"],
        ),
        two_sources,
    )
    assert row["status"] == "data_supported"
    assert row["matched"] == ["metadata:flowjo_donor_group"]
