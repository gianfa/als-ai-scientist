"""The read-only data server: what an agent may learn about the run's datasets, and what it may not."""

from __future__ import annotations

import json

import pytest

from tools.data import mcp_server
from tools.data.mcp_server import PUBLIC_FIELDS, data_describe, data_list

MANIFEST = {
    "root": "/repo",
    "generated_at": "2026-10-03T10:00:00+00:00",
    "sources": [
        {
            "id": "michaelis-table1-epitopes",
            "kind": "table",
            "path": "dist/t1.csv",
            "sha256": "a" * 64,
            "ref": "dataset:michaelis-table1-epitopes@aaaaaaaaaaaa",
            "files": 1,
            "bytes": 2048,
            "is_directory": False,
            "modified_at": "2026-09-28T10:00:00+00:00",
            "license_note": "CC BY-NC-ND 4.0",
        },
        {
            "id": "michaelis-zenodo-cytometry",
            "kind": "raw",
            "path": "data/external/zenodo",
            "sha256": "b" * 64,
            "ref": "dataset:michaelis-zenodo-cytometry@bbbbbbbbbbbb",
            "files": 141,
            "bytes": 2_133_000_000,
            "is_directory": True,
            "modified_at": "2026-10-01T10:00:00+00:00",
            "license_note": "not redistributed",
        },
    ],
    "absent": [{"id": "some-optional-set", "path": "data/external/missing", "required": False}],
    "bytes": 2_133_002_048,
    "files": 142,
}


@pytest.fixture()
def manifest(tmp_path):
    path = tmp_path / "data-manifest.json"
    path.write_text(json.dumps(MANIFEST))
    return path


def test_the_listing_names_every_dataset_and_the_ones_not_found(manifest):
    answer = data_list(manifest)
    assert [s["id"] for s in answer["sources"]] == ["michaelis-table1-epitopes", "michaelis-zenodo-cytometry"]
    assert answer["absent"] == ["some-optional-set"]
    assert answer["files"] == 142 and answer["bytes"] == 2_133_002_048


def test_no_answer_carries_a_path_or_a_way_to_read_a_file(manifest):
    """Reading a data file belongs to code that writes a verifiable artifact: a path invites a read."""
    answer = data_list(manifest)
    assert all("path" not in source and "sha256" not in source for source in answer["sources"])
    assert "path" not in data_describe(manifest, "michaelis-table1-epitopes")["source"]
    assert "path" not in PUBLIC_FIELDS and "sha256" not in PUBLIC_FIELDS
    # The server exposes exactly two tools, and neither returns contents.
    assert sorted(n for n in dir(mcp_server) if n.startswith("data_")) == ["data_describe", "data_list"]


def test_a_description_says_which_analyses_read_the_dataset(manifest):
    described = data_describe(manifest, "michaelis-table1-epitopes")
    assert described["analysed_by"] == ["a1_immunodominance", "a2_epitope_clustering", "a4_hla_binding"]
    assert described["source"]["ref"] == "dataset:michaelis-table1-epitopes@aaaaaaaaaaaa"
    # A dataset nothing reads must be visibly unanalysed, not silently equivalent to an analysed one.
    assert data_describe(manifest, "michaelis-zenodo-cytometry")["analysed_by"] == []


def test_a_declared_but_absent_dataset_is_reported_as_absent(manifest):
    answer = data_describe(manifest, "some-optional-set")
    assert answer["absent"] is True and "not found on disk" in answer["note"]


def test_an_unknown_id_is_an_error_naming_what_the_run_has(manifest):
    answer = data_describe(manifest, "michaelis-table-1")
    # An agent must not read a typo as "this dataset does not exist".
    assert "unknown source 'michaelis-table-1'" in answer["error"]
    assert "michaelis-table1-epitopes" in answer["error"]


def test_an_unreadable_manifest_is_returned_as_data_not_raised(tmp_path):
    broken = tmp_path / "data-manifest.json"
    broken.write_text("{not json")
    assert "error" in data_list(broken) and "error" in data_describe(broken, "x")
    assert "JSONDecodeError" in data_list(broken)["error"]


def test_the_server_refuses_to_start_without_a_manifest(tmp_path, capsys):
    assert mcp_server.main(["--manifest", str(tmp_path / "absent.json"), "--check"]) == 2
    assert "cannot start" in capsys.readouterr().err


def test_the_shape_of_a_source_reaches_the_agents(tmp_path):
    """Without it, a designer chooses a dataset by size and may rest an experiment on a cytokine the
    deposit never measured."""
    manifest = tmp_path / "data-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "id": "cyto",
                        "kind": "raw",
                        "ref": "dataset:cyto@abc",
                        "files": 141,
                        "bytes": 2_000_000_000,
                        "path": "data/external/cyto",
                        "sha256": "x" * 64,
                        "shape": {
                            "markers": ["IL-17", "IL-10"],
                            "channels": ["BV785-A"],
                            "fcs_files": 129,
                            "note": "A marker absent here was not measured.",
                        },
                    }
                ],
                "absent": [],
            }
        )
    )

    described = data_describe(manifest, "cyto")

    assert described["source"]["shape"]["markers"] == ["IL-17", "IL-10"]
    assert "path" not in described["source"] and "sha256" not in described["source"]  # still metadata only
    assert data_list(manifest)["sources"][0]["shape"]["fcs_files"] == 129
