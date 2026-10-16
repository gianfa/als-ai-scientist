"""What a cytometry deposit measures (`tools/data/cytometry.py`): read from the files, stdlib only, no gating."""

from __future__ import annotations

from pathlib import Path

from tools.data.cytometry import METADATA_FIELDS, Panel, channels_of, keywords, markers_of, measures, read_panel


def fcs(
    path: Path,
    *,
    channels: list[str],
    events: int = 1000,
    cytometer: str = "LSRFortessa",
    stains: list[str] | None = None,
) -> Path:
    """A minimal FCS 3.0 file: the header's TEXT offsets and a delimited key/value segment. No event data —
    this module never reads any."""
    pairs = [("$PAR", str(len(channels))), ("$TOT", str(events)), ("$CYT", cytometer)]
    for i, name in enumerate(channels, start=1):
        pairs += [(f"$P{i}N", name)]
        if stains:
            pairs += [(f"$P{i}S", stains[i - 1])]
    text = "/" + "/".join(f"{k}/{v}" for k, v in pairs).replace("/", "/") + "/"
    begin = 58
    header = f"FCS3.0    {begin:>8}{begin + len(text) - 1:>8}{0:>8}{0:>8}{0:>8}{0:>8}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header.encode("ascii") + text.encode("ascii"))
    return path


def wsp(path: Path, gates: list[str], groups: list[str] | None = None) -> Path:
    body = "".join(f'<Population name="{g}"  annotation=""  owningGroup="x">\n</Population>\n' for g in gates)
    group_nodes = "".join(f'<GroupNode name="{g}"></GroupNode>\n' for g in (groups or []))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'<?xml version="1.0"?>\n<Workspace>\n<Groups>{group_nodes}</Groups>\n{body}</Workspace>\n')
    return path


def test_the_channels_and_the_event_count_are_read_from_the_text_segment(tmp_path):
    path = fcs(tmp_path / "a.fcs", channels=["FSC-A", "BV785-A", "APC-A", "Time"], events=50531)

    assert keywords(path)["$PAR"] == "4"
    assert channels_of(path) == (["FSC-A", "BV785-A", "APC-A", "Time"], 50531, "LSRFortessa")


def test_a_file_that_is_not_fcs_is_reported_unreadable_and_not_guessed_at(tmp_path):
    (tmp_path / "b.fcs").write_bytes(b"not a cytometry file at all")

    assert keywords(tmp_path / "b.fcs") == {}
    panel = read_panel(tmp_path)
    assert panel.fcs_files == 0 and "not readable as FCS 3.x" in panel.unreadable[0]


def test_the_markers_come_from_the_gate_names_because_the_files_do_not_carry_them(tmp_path):
    """In the real deposit every `$PnS` stain label is empty, so the antibody is only in the workspace."""
    path = wsp(tmp_path / "ics.wsp", ["IL-17 BV785 +", "IL-10 APC +", "IFNy FITC +", "CD4+", "Lymphocytes"])

    assert markers_of(path) == ["IL-17", "IL-10", "IFNY", "CD4"]


def test_a_deposit_is_the_union_of_its_files_and_its_workspaces(tmp_path):
    fcs(tmp_path / "r1" / "a.fcs", channels=["FSC-A", "BV785-A"], events=10)
    fcs(tmp_path / "r2" / "b.fcs", channels=["FSC-A", "APC-A"], events=20, cytometer="Aurora")
    wsp(tmp_path / "r1" / "one.wsp", ["IL-17 BV785 +"])
    wsp(tmp_path / "r2" / "two.wsp", ["IL-10 APC +", "CD8+"])

    panel = read_panel(tmp_path)

    assert panel.fcs_files == 2 and panel.workspaces == 2 and panel.events == 30
    assert set(panel.channels) == {"FSC-A", "BV785-A", "APC-A"}
    assert set(panel.markers) == {"IL-17", "IL-10", "CD8"}
    assert sorted(panel.cytometers) == ["Aurora", "LSRFortessa"]
    assert "A marker absent here was not measured" in panel.as_dict()["note"]


def test_the_question_design_should_be_able_to_ask(tmp_path):
    """A single-cell IL-5/IL-10 co-expression reanalysis of a deposit with no IL-5 channel must be refusable."""
    wsp(tmp_path / "ics.wsp", ["IL-17 BV785 +", "IL-10 APC +", "IFNy FITC +"])
    panel = read_panel(tmp_path)

    assert measures(panel, "IL-10") and measures(panel, "IL-17")
    assert not measures(panel, "IL-5")
    assert not measures(panel, "FoxP3")


def test_an_interferon_matches_however_it_was_spelled_but_il17a_is_not_il17(tmp_path):
    """A gate named `IFNy` must answer a question about `IFN`; merging `IL-17` with `IL-17A` would be a
    biological judgement, so it is left to a human and the module answers False."""
    wsp(tmp_path / "ics.wsp", ["IFNy FITC +", "IL-17 BV785 +"])
    panel = read_panel(tmp_path)

    assert measures(panel, "IFN") and measures(panel, "IFNy") and measures(panel, "IFN-gamma")
    assert measures(panel, "IL-17") and not measures(panel, "IL-17A")


def test_max_files_caps_the_reads_for_a_quick_look(tmp_path):
    for i in range(5):
        fcs(tmp_path / f"f{i}.fcs", channels=["FSC-A"], events=1)

    assert read_panel(tmp_path, max_files=2).fcs_files == 2
    assert read_panel(tmp_path).fcs_files == 5


def test_an_empty_panel_says_nothing_rather_than_claiming_a_deposit_measures_nothing(tmp_path):
    panel = read_panel(tmp_path)

    assert panel == Panel()
    assert panel.as_dict()["markers"] == [] and panel.as_dict()["fcs_files"] == 0


# --- metadata_fields: structural facts about group/condition labels and donor grouping -------------------------


def test_two_leading_file_name_labels_are_a_group_label(tmp_path):
    """The designers of a real run declared exactly this fact (a disease/control group label) as unverifiable."""
    fcs(tmp_path / "ALS Specimen_001_C1 Negative.fcs", channels=["FSC-A"])
    fcs(tmp_path / "HC Specimen_001_C1 Negative.fcs", channels=["FSC-A"])

    assert "fcs_file_name_group_label" in read_panel(tmp_path).metadata_fields


def test_one_leading_label_or_none_is_not_a_group_label(tmp_path):
    fcs(tmp_path / "ALS Specimen_001_C1 Negative.fcs", channels=["FSC-A"])
    fcs(tmp_path / "ALS Specimen_001_C2 Peptide.fcs", channels=["FSC-A"])
    assert "fcs_file_name_group_label" not in read_panel(tmp_path).metadata_fields

    (tmp_path / "a.fcs").unlink(missing_ok=True)
    for f in tmp_path.glob("*.fcs"):
        f.unlink()
    fcs(tmp_path / "a.fcs", channels=["FSC-A"])
    fcs(tmp_path / "b.fcs", channels=["FSC-A"])
    assert read_panel(tmp_path).metadata_fields == []  # no space-separated label at all


def test_two_trailing_labels_within_one_group_are_a_condition_label(tmp_path):
    fcs(tmp_path / "ALS Specimen_001_C1 Negative.fcs", channels=["FSC-A"])
    fcs(tmp_path / "ALS Specimen_001_C2 Peptide.fcs", channels=["FSC-A"])
    fcs(tmp_path / "HC Specimen_001_C1 Negative.fcs", channels=["FSC-A"])

    fields = read_panel(tmp_path).metadata_fields
    assert "fcs_file_name_group_label" in fields and "fcs_file_name_condition_label" in fields


def test_a_single_trailing_label_per_group_is_not_a_condition_label(tmp_path):
    fcs(tmp_path / "ALS Specimen_001_C1 Negative.fcs", channels=["FSC-A"])
    fcs(tmp_path / "HC Specimen_001_C1 Negative.fcs", channels=["FSC-A"])

    assert "fcs_file_name_condition_label" not in read_panel(tmp_path).metadata_fields


def test_a_workspace_group_named_donor_is_the_flowjo_donor_group_field(tmp_path):
    wsp(tmp_path / "a.wsp", ["CD4+"], groups=["All Samples", "Donor 1", "Donor 2"])

    assert "flowjo_donor_group" in read_panel(tmp_path).metadata_fields


def test_a_workspace_with_no_donor_named_group_lacks_the_field(tmp_path):
    wsp(tmp_path / "a.wsp", ["CD4+"], groups=["All Samples", "peptide samples"])

    assert "flowjo_donor_group" not in read_panel(tmp_path).metadata_fields


def test_metadata_fields_reach_as_dict_and_every_key_is_in_the_controlled_vocabulary(tmp_path):
    fcs(tmp_path / "ALS Specimen_001_C1 Negative.fcs", channels=["FSC-A"])
    fcs(tmp_path / "ALS Specimen_001_C2 Peptide.fcs", channels=["FSC-A"])
    fcs(tmp_path / "HC Specimen_001_C1 Negative.fcs", channels=["FSC-A"])
    wsp(tmp_path / "a.wsp", ["CD4+"], groups=["Donor 1"])

    data = read_panel(tmp_path).as_dict()
    assert set(data["metadata_fields"]) == {
        "fcs_file_name_group_label",
        "fcs_file_name_condition_label",
        "flowjo_donor_group",
    }
    assert set(data["metadata_fields"]) <= set(METADATA_FIELDS)
    assert "metadata_fields" in data["note"]
