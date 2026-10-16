"""The local data sources of a run: declared, resolved, hashed, and never guessed."""

from __future__ import annotations

import pytest

from tools.data.local_sources import (
    HASH_PREFIX,
    Acquisition,
    LocalSource,
    MissingSource,
    describe,
    manifest,
    path_of,
    sha256_file,
)


def table(tmp_path, name="t.csv", text="a,b\n1,2\n"):
    path = tmp_path / name
    path.write_text(text)
    return LocalSource(id=name.split(".")[0], path=path, kind="table", license_note="note")


def test_a_file_source_is_hashed_and_carries_a_citable_reference(tmp_path):
    data = manifest([table(tmp_path)], tmp_path)
    [entry] = data["sources"]
    assert entry["sha256"] == sha256_file(tmp_path / "t.csv")
    assert entry["ref"] == f"dataset:t@{entry['sha256'][:HASH_PREFIX]}"
    assert entry["files"] == 1 and entry["bytes"] == 8 and entry["is_directory"] is False
    assert entry["path"] == "t.csv"  # recorded relative to the root it was resolved against


def test_a_changed_file_changes_the_reference(tmp_path):
    source = table(tmp_path)
    before = manifest([source], tmp_path)["sources"][0]["ref"]
    (tmp_path / "t.csv").write_text("a,b\n1,3\n")  # same path, different content
    assert manifest([source], tmp_path)["sources"][0]["ref"] != before


def test_a_directory_source_hashes_names_and_contents(tmp_path):
    folder = tmp_path / "raw"
    (folder / "sub").mkdir(parents=True)
    (folder / "sub" / "a.fcs").write_text("x")
    (folder / "b.fcs").write_text("yy")
    source = LocalSource(id="raw", path=folder, kind="raw", license_note="note")

    entry = manifest([source], tmp_path)["sources"][0]
    assert entry["is_directory"] and entry["files"] == 2 and entry["bytes"] == 3
    before = entry["sha256"]
    (folder / "sub" / "a.fcs").rename(folder / "sub" / "renamed.fcs")
    # A rename with identical contents is a different dataset version: the names are part of the digest.
    assert manifest([source], tmp_path)["sources"][0]["sha256"] != before


def test_every_missing_required_source_is_named_at_once(tmp_path):
    present = table(tmp_path)
    gone = [LocalSource(id=f"m{n}", path=tmp_path / f"m{n}.csv", kind="table", license_note="note") for n in (1, 2)]
    with pytest.raises(MissingSource) as err:
        manifest([present, *gone], tmp_path)
    # One run should tell a user every file to fetch, not the first one.
    assert "m1" in str(err.value) and "m2" in str(err.value)


def test_a_missing_source_report_names_the_procedure_that_produces_it(tmp_path):
    """The first failure a new user meets must say what to run, not only what is absent."""
    gone = LocalSource(
        id="gwas",
        path=tmp_path / "sumstats.tsv.gz",
        kind="sumstats",
        license_note="note",
        acquire=Acquisition(
            see="docs/data-acquisition.md section 4", steps=(("run", "scripts/data/gwas-data-download.sh"),)
        ),
    )
    with pytest.raises(MissingSource) as err:
        manifest([gone], tmp_path)
    message = str(err.value)
    assert "sumstats.tsv.gz" in message and "docs/data-acquisition.md section 4" in message
    assert "      $ scripts/data/gwas-data-download.sh" in message.splitlines()


def test_a_command_is_printed_alone_on_its_line_and_a_manual_step_is_marked_as_such(tmp_path):
    """Every line after `$ ` must be exactly the command, so pasting it runs that command and nothing else,
    not the surrounding prose."""
    procedure = Acquisition(
        see="docs/x.md section 1",
        steps=(
            ("run", "scripts/a.py --format pdf"),
            ("by hand", "extract it with Paper2Agent"),
            ("run", "scripts/check.sh"),
        ),
    )
    gone = LocalSource(id="t", path=tmp_path / "t.csv", kind="table", license_note="note", acquire=procedure)
    with pytest.raises(MissingSource) as err:
        manifest([gone], tmp_path)
    lines = str(err.value).splitlines()
    commands = [line.split("$ ", 1)[1] for line in lines if line.lstrip().startswith("$ ")]
    assert commands == ["scripts/a.py --format pdf", "scripts/check.sh"]
    assert "      by hand: extract it with Paper2Agent" in lines


def test_one_procedure_producing_several_missing_files_is_printed_once(tmp_path):
    procedure = Acquisition(see="docs/x.md section 1", steps=(("run", "scripts/a.py"),))
    gone = [
        LocalSource(id=f"t{n}", path=tmp_path / f"t{n}.csv", kind="table", license_note="note", acquire=procedure)
        for n in (1, 2)
    ]
    with pytest.raises(MissingSource) as err:
        manifest(gone, tmp_path)
    message = str(err.value)
    assert message.count("$ scripts/a.py") == 1
    assert "same procedure as t1" in message


def test_a_source_with_no_declared_procedure_says_so_rather_than_inventing_one(tmp_path):
    gone = LocalSource(id="mystery", path=tmp_path / "x.csv", kind="table", license_note="note")
    with pytest.raises(MissingSource) as err:
        manifest([gone], tmp_path)
    assert "no procedure declared" in str(err.value)


def test_an_absent_optional_source_is_recorded_as_absent(tmp_path):
    optional = LocalSource(id="cytometry", path=tmp_path / "zenodo", kind="raw", license_note="note", required=False)
    data = manifest([table(tmp_path), optional], tmp_path)
    assert [a["id"] for a in data["absent"]] == ["cytometry"]
    assert [e["id"] for e in data["sources"]] == ["t"]


def test_a_relative_declaration_is_resolved_against_the_root(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "t.csv").write_text("a\n")
    source = LocalSource(id="t", path="data/t.csv", kind="table", license_note="note")
    data = manifest([source], tmp_path)
    assert path_of(data, "t", tmp_path) == tmp_path / "data" / "t.csv"
    with pytest.raises(MissingSource, match="no source 'absent'"):
        path_of(data, "absent", tmp_path)


def test_a_cytometry_deposit_says_what_it_measures(tmp_path):
    """Size and a hash do not let a designer choose: a manifest that describes a deposit only by size lets
    an experiment rest on a cytokine the deposit never measured."""
    from test_tools_data_cytometry import fcs, wsp

    root = tmp_path / "deposit"
    fcs(root / "a.fcs", channels=["FSC-A", "BV785-A"], events=7)
    wsp(root / "ics.wsp", ["IL-17 BV785 +", "IL-10 APC +"])
    source = LocalSource(id="cyto", path=root, kind="raw", license_note="test fixture")

    entry = describe(source, tmp_path)

    assert entry["shape"]["markers"] == ["IL-17", "IL-10"]
    assert entry["shape"]["fcs_files"] == 1 and entry["shape"]["events"] == 7
    assert "was not measured" in entry["shape"]["note"]


def test_a_source_with_nothing_to_read_carries_no_shape_rather_than_an_empty_one(tmp_path):
    table = tmp_path / "t.csv"
    table.write_text("a,b\n1,2\n")
    source = LocalSource(id="table", path=table, kind="table", license_note="test fixture")

    assert "shape" not in describe(source, tmp_path)
