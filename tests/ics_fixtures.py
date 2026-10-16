"""Builders of a synthetic Michaelis-shaped ICS deposit, for `tests/test_experiment_ics.py` and for the static
fixture under `tests/fixtures/execute/` (generated once by `tests/fixtures/execute/build_ics_deposit.py`).

Writes only what `analysis/experiment_ics_cytokines.py` reads: FlowJo workspace XML with the authors' own CD4+
and per-cytokine gate counts, empty `.fcs` files whose names carry group, well and condition, and an `.xlsx`
holding the "ED Fig. 4" columns. No event-level data and no real patient data: every count here is made up.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from analysis.experiment_ics_cytokines import ICS_RECORDS, SOURCE_COLUMNS, SOURCE_SHEET

GATES = {"IFNg": "IFNy FITC +", "IL-4": "IL-4 BV421 +", "IL-10": "IL-10 APC +", "IL-17": "IL-17 BV785 +"}

# One donor's three wells: (peptide, vehicle) cytokine counts per 10 000 CD4+ cells.
DONORS = {
    "ALS": [
        ({"IFNg": 20, "IL-4": 150, "IL-10": 60, "IL-17": 5}, {"IFNg": 10, "IL-4": 30, "IL-10": 20, "IL-17": 5}),
        ({"IFNg": 15, "IL-4": 120, "IL-10": 40, "IL-17": 8}, {"IFNg": 5, "IL-4": 20, "IL-10": 10, "IL-17": 8}),
    ],
    "HC": [
        ({"IFNg": 200, "IL-4": 40, "IL-10": 15, "IL-17": 6}, {"IFNg": 20, "IL-4": 20, "IL-10": 5, "IL-17": 6}),
        ({"IFNg": 180, "IL-4": 30, "IL-10": 10, "IL-17": 9}, {"IFNg": 30, "IL-4": 10, "IL-10": 10, "IL-17": 9}),
    ],
}


def population(name: str, count: int, children: str = "") -> str:
    return (
        f'<Population name="{name}" count="{count}"><Gate/>'
        + (f"<Subpopulations>{children}</Subpopulations>" if children else "")
        + "</Population>"
    )


def sample_xml(sid: int, file_name: str, cd4: int, positive: dict[str, int]) -> str:
    cytokines = "".join(population(GATES[k], v) for k, v in positive.items())
    tree = population(
        "Lymphocytes", cd4 * 2, population("CD3", cd4 + 10, population("CD4 Single Positive", cd4, cytokines))
    )
    return (
        f'<Sample><DataSet uri="file:/Volumes/x/{file_name.replace(" ", "%20")}"/>'
        f'<SampleNode sampleID="{sid}" count="{cd4 * 3}"><Subpopulations>{tree}</Subpopulations>'
        f"</SampleNode></Sample>"
    )


def workspace(
    path: Path, samples: list[tuple[int, str, int, dict[str, int]]], donors: dict[str, list[int]] | None = None
) -> None:
    groups = "".join(
        f'<GroupNode name="{name}"><Group>{"".join(f"<SampleRef sampleID={chr(34)}{i}{chr(34)}/>" for i in ids)}'
        f"</Group></GroupNode>"
        for name, ids in (donors or {}).items()
    )
    path.write_text(
        f'<?xml version="1.0"?><Workspace><Groups>{groups}</Groups><SampleList>'
        + "".join(sample_xml(*s) for s in samples)
        + "</SampleList></Workspace>"
    )


def deposit(root: Path, donors_by_group: dict = DONORS) -> Path:
    """Four ICS records: the first grouped by FlowJo "Donor" groups, the others by consecutive well triplets."""
    cytometry = root / "michaelis-2025-zenodo"
    for index, record in enumerate(ICS_RECORDS):
        record_dir = cytometry / record
        record_dir.mkdir(parents=True)
        for group, donors in donors_by_group.items():
            samples, groups, sid = [], {}, 1
            for d, (peptide, vehicle) in enumerate(donors):
                wells = [f"C{3 * d + i}" for i in (1, 2, 3)]
                for well, condition, counts in zip(
                    wells, ("Negative", "Peptide", "Positive"), (vehicle, peptide, peptide)
                ):
                    name = f"Specimen_001_{well}_{well} {condition}.fcs"
                    (record_dir / f"{group} {name}").write_bytes(b"")
                    samples.append((sid, name if index == 0 else f"Specimen_001_{well}_{well}.fcs", 10_000, counts))
                    groups.setdefault(f"Donor {d + 1}", []).append(sid)
                    sid += 1
            label = "ALS Donors" if group == "ALS" else "Healthy Donors"
            workspace(record_dir / f"{label}.wsp", samples, groups if index == 0 else None)
    return cytometry


def source_data(path: Path, rows: dict[str, list[tuple[float, ...]]]) -> Path:
    """A minimal .xlsx with the four CD4+ columns per group of ED Fig. 4e, inline numbers, three header rows."""
    cells = []
    for group, values in rows.items():
        for i, cytokine in enumerate(("IFNg", "IL-4", "IL-10", "IL-17")):
            column = SOURCE_COLUMNS[(group, cytokine)]
            cells += [f'<c r="{column}{4 + r}"><v>{row[i]}</v></c>' for r, row in enumerate(values)]
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    rel = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    with zipfile.ZipFile(path, "w") as book:
        book.writestr(
            "xl/workbook.xml",
            f"<workbook {ns} {rel}><sheets>"
            f'<sheet name="{SOURCE_SHEET}" sheetId="1" r:id="rId1"/></sheets></workbook>',
        )
        book.writestr(
            "xl/_rels/workbook.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>',
        )
        book.writestr("xl/sharedStrings.xml", f"<sst {ns}></sst>")
        book.writestr(
            "xl/worksheets/sheet1.xml",
            f"<worksheet {ns}><sheetData><row>{''.join(cells)}</row>" "</sheetData></worksheet>",
        )
    return path


def expected_rows(group: str) -> list[tuple[float, ...]]:
    return [
        tuple(round(max(0.01, (p[k] - v[k]) / 100), 2) for k in ("IFNg", "IL-4", "IL-10", "IL-17"))
        for p, v in DONORS[group]
    ]
