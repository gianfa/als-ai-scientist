"""A preregistered re-analysis of the C9orf72-specific CD4+ cytokine profile in the Michaelis ICS deposit.

Michaelis et al. (Nature 2025) expanded PBMCs for two weeks with a C9orf72 peptide megapool, restimulated them,
and measured IFN-gamma, IL-4, IL-10 and IL-17 by intracellular cytokine staining (ICS). They report that the
ratio IFN-gamma / (IL-4 + IL-10) in CD4+ T cells is lower in ALS than in healthy controls (Extended Data Fig. 4j).

This analysis recomputes that comparison from the deposited FlowJo workspaces. It reads the event counts FlowJo
saved for the authors' own gates; it does not open the FCS files and does not re-gate, so it inherits the
authors' gating and cannot check it. Per donor, the cytokine frequency in the peptide well minus the one in the
vehicle (DMSO) well, floored at 0.01 as the published source data are, gives the antigen-specific response.

The preregistered rule: the skew is replicated when the ALS median of IFN-gamma / (IL-4 + IL-10) is below the
control median and an exact two-sided Mann-Whitney test gives p < 0.05. Before the test, each donor's four
responses are compared with the published source data (Extended Data Fig. 4e), screen by screen, and the
reproduction is reported beside the outcome: a re-analysis that does not reproduce its source is not evidence
of the source's claim.
"""

from __future__ import annotations

import re
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any

ANALYSIS_ID = "ics_cytokine_skew_v1"
CYTOKINES = ("IFNg", "IL-4", "IL-10", "IL-17")
GATE_PREFIX = {"IFNg": "IFNy", "IL-4": "IL-4", "IL-10": "IL-10", "IL-17": "IL-17"}  # FlowJo gate names
CD4_GATE = "CD4 Single Positive"
CONDITIONS = ("Negative", "Peptide", "Positive")
FLOOR = 0.01  # the smallest value in the published source data: responses at or below it are set to it
ALPHA = 0.05
# The four ICS records of the deposit (paper, Data availability); the spectral record 16749283 is not ICS.
ICS_RECORDS = ("16749334", "16754553", "16754591", "16754609")
# Source data, sheet "ED Fig. 4", panel e: CD4+ cytokine responses, one column per cytokine and group.
SOURCE_SHEET = "ED Fig. 4"
SOURCE_COLUMNS = {
    ("HC", "IFNg"): "M",
    ("ALS", "IFNg"): "N",
    ("HC", "IL-4"): "O",
    ("ALS", "IL-4"): "P",
    ("HC", "IL-10"): "Q",
    ("ALS", "IL-10"): "R",
    ("HC", "IL-17"): "S",
    ("ALS", "IL-17"): "T",
}
SOURCE_HEADER_ROWS = 3

_XLSX = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


@dataclass(frozen=True)
class Sample:
    record: str
    group: str  # "ALS" or "HC"
    donor: str  # unique within (record, group)
    well: str
    condition: str
    cd4: int
    positive: dict[str, int]


# --- reading -------------------------------------------------------------------------------------------------------


def _well(name: str) -> str:
    match = re.search(r"_([A-H]\d{1,2})(?=[ .])", name)
    if not match:
        raise ValueError(f"no well identifier in {name!r}")
    return match.group(1)


def _condition(name: str) -> str:
    # The deposit spells one vehicle well "Negatice" (16754553, H1); it is read as the vehicle it is.
    match = re.search(r"\b(Negati\w+|Peptide|Positive)\.fcs$", name)
    if not match:
        raise ValueError(f"no condition in {name!r}")
    return "Negative" if match.group(1).startswith("Negati") else match.group(1)


def _deposited_conditions(record_dir: Path) -> dict[tuple[str, str], str]:
    """(group, well) -> condition, from the deposited FCS names, e.g. `ALS Specimen_001_C2_C02 Peptide.fcs`."""
    found: dict[tuple[str, str], str] = {}
    for path in sorted(record_dir.glob("*.fcs")):
        group = path.name.split(" ", 1)[0]
        if group not in ("ALS", "HC"):
            raise ValueError(f"no group prefix in {path.name!r}")
        found[(group, _well(path.name))] = _condition(path.name)
    return found


def _group_of(label: str) -> str | None:
    if re.search(r"\bALS\b", label):
        return "ALS"
    if re.search(r"\b(HC|Healthy)\b", label):
        return "HC"
    return None


def _population(node: ET.Element, name_prefix: str) -> ET.Element:
    found = [
        p
        for sub in node.findall("Subpopulations")
        for p in sub.findall("Population")
        if p.attrib["name"].startswith(name_prefix)
    ]
    if len(found) != 1:
        raise ValueError(f"expected one gate starting {name_prefix!r}, found {len(found)}")
    return found[0]


def _find(node: ET.Element, name: str) -> ET.Element:
    for population in node.iter("Population"):
        if population.attrib["name"] == name:
            return population
    raise ValueError(f"no {name!r} gate")


def read_workspace(path: Path, record: str, deposited: dict[tuple[str, str], str]) -> list[Sample]:
    """Every sample of one FlowJo workspace, with its donor, condition and the counts of the authors' gates.

    The donor comes from the workspace's own "Donor ..." groups when it has them. Screens 3 and 4 have none; their
    wells form consecutive triplets (C1-C3, C4-C6, ...), each holding one vehicle, one peptide and one positive
    well, which is how screens 1 and 2 are grouped. Any donor without exactly those three wells is refused.
    """
    root = ET.parse(path).getroot()
    nodes = {}
    for sample in root.iter("Sample"):
        node = sample.find("SampleNode")
        uri = urllib.parse.unquote(sample.find("DataSet").attrib["uri"])
        nodes[node.attrib["sampleID"]] = (uri.rsplit("/", 1)[-1], node)
    donor_groups: dict[str, str] = {}
    for group in root.iter("GroupNode"):
        name = group.attrib.get("name", "")
        if re.match(r"(?i)donor\b", name):
            for ref in group.iter("SampleRef"):
                if ref.attrib["sampleID"] in nodes:
                    previous = donor_groups.setdefault(ref.attrib["sampleID"], name)
                    if previous != name:
                        raise ValueError(f"{path.name}: sample {ref.attrib['sampleID']} is in two donor groups")
    workspace_group = _group_of(path.stem)
    samples = []
    for sid, (file_name, node) in sorted(nodes.items(), key=lambda kv: int(kv[0])):
        well = _well(file_name)
        donor = donor_groups.get(sid) or f"triplet-{well[0]}{(int(well[1:]) - 1) // 3 + 1}"
        group = _group_of(donor) or workspace_group
        if group is None:
            raise ValueError(f"{path.name}: no ALS/HC group for sample {sid}")
        condition = deposited.get((group, well))
        if condition is None:
            raise ValueError(f"{path.name}: no deposited FCS file for {group} well {well}")
        cd4 = _find(node, CD4_GATE)
        count = int(cd4.attrib["count"])
        if count <= 0:
            raise ValueError(f"{path.name}: sample {sid} has no CD4+ events")
        positive = {k: int(_population(cd4, GATE_PREFIX[k]).attrib["count"]) for k in CYTOKINES}
        samples.append(Sample(record, group, donor, well, condition, count, positive))
    return samples


def read_deposit(cytometry: Path) -> list[Sample]:
    samples = []
    for record in ICS_RECORDS:
        record_dir = cytometry / record
        workspaces = sorted(record_dir.glob("*.wsp"))
        if not workspaces:
            raise ValueError(f"ICS record {record} has no FlowJo workspace under {cytometry}")
        deposited = _deposited_conditions(record_dir)
        for workspace in workspaces:
            samples += read_workspace(workspace, record, deposited)
    return samples


def read_source_data(xlsx: Path) -> dict[str, list[tuple[float, ...]]]:
    """Published CD4+ responses per group, one 4-tuple (IFNg, IL-4, IL-10, IL-17) per row of ED Fig. 4e."""
    with zipfile.ZipFile(xlsx) as book:
        strings = [
            "".join(t.text or "" for t in si.iter(_XLSX + "t"))
            for si in ET.fromstring(book.read("xl/sharedStrings.xml")).findall(_XLSX + "si")
        ]
        rels = {r.attrib["Id"]: r.attrib["Target"] for r in ET.fromstring(book.read("xl/_rels/workbook.xml.rels"))}
        sheets = {
            s.attrib["name"]: rels[s.attrib[_REL]]
            for s in ET.fromstring(book.read("xl/workbook.xml")).find(_XLSX + "sheets")
        }
        if SOURCE_SHEET not in sheets:
            raise ValueError(f"source data has no sheet {SOURCE_SHEET!r}")
        sheet = ET.fromstring(book.read("xl/" + sheets[SOURCE_SHEET].split("xl/")[-1]))
    cells: dict[tuple[str, int], str] = {}
    for cell in sheet.iter(_XLSX + "c"):
        value = cell.find(_XLSX + "v")
        if value is None:
            continue
        column, row = re.match(r"([A-Z]+)(\d+)", cell.attrib["r"]).groups()
        cells[(column, int(row))] = strings[int(value.text)] if cell.attrib.get("t") == "s" else value.text
    published: dict[str, list[tuple[float, ...]]] = {}
    for group in ("HC", "ALS"):
        columns = [SOURCE_COLUMNS[(group, k)] for k in CYTOKINES]
        rows = sorted({r for c, r in cells if c in columns and r > SOURCE_HEADER_ROWS})
        published[group] = [
            tuple(float(cells[(c, r)]) for c in columns) for r in rows if all((c, r) in cells for c in columns)
        ]
    return published


# --- computing -----------------------------------------------------------------------------------------------------


def responses(samples: list[Sample]) -> list[dict[str, Any]]:
    """Per donor: peptide minus vehicle frequency (% of CD4+), floored, rounded as the source data are."""
    by_donor: dict[tuple[str, str, str], dict[str, Sample]] = {}
    for s in samples:
        wells = by_donor.setdefault((s.record, s.group, s.donor), {})
        if s.condition in wells:
            raise ValueError(f"donor {s.donor} of {s.record} has two {s.condition} wells")
        wells[s.condition] = s
    donors = []
    for (record, group, donor), wells in sorted(by_donor.items()):
        if set(wells) != set(CONDITIONS):
            raise ValueError(f"donor {donor} of {record} has wells {sorted(wells)}, not {list(CONDITIONS)}")
        peptide, vehicle = wells["Peptide"], wells["Negative"]
        response = {
            k: round(max(FLOOR, 100 * peptide.positive[k] / peptide.cd4 - 100 * vehicle.positive[k] / vehicle.cd4), 2)
            for k in CYTOKINES
        }
        donors.append(
            {
                "record": record,
                "group": group,
                "donor": donor,
                "response": response,
                "cd4_events": {"peptide": peptide.cd4, "vehicle": vehicle.cd4},
                "skew_ratio": response["IFNg"] / (response["IL-4"] + response["IL-10"]),
            }
        )
    return donors


def reproduction(donors: list[dict[str, Any]], published: dict[str, list[tuple[float, ...]]]) -> dict[str, Any]:
    """Which donors' four responses equal a published row of their group, at two decimals, each row used once."""
    left = {g: Counter(tuple(round(v, 2) for v in row) for row in rows) for g, rows in published.items()}
    per_record: dict[str, dict[str, int]] = {}
    for donor in donors:
        key = tuple(donor["response"][k] for k in CYTOKINES)
        stats = per_record.setdefault(donor["record"], {"donors": 0, "reproduced": 0})
        stats["donors"] += 1
        if left[donor["group"]][key] > 0:
            left[donor["group"]][key] -= 1
            stats["reproduced"] += 1
            donor["reproduced"] = True
        else:
            donor["reproduced"] = False
    return {
        "per_record": per_record,
        "reproduced": sum(s["reproduced"] for s in per_record.values()),
        "donors": len(donors),
        "published_rows": {g: len(rows) for g, rows in published.items()},
    }


def mann_whitney_exact(x: list[float], y: list[float]) -> tuple[float, float]:
    """Two-sided exact Mann-Whitney test, (U of x, p), by enumerating rank sums over midranks.

    The null distribution is that of the rank sum of a random size-|x| subset of the pooled midranks, counted by
    dynamic programming; ties are therefore handled exactly rather than by a normal correction.
    """
    pooled = sorted(x + y)
    rank: dict[float, float] = {}
    i = 0
    while i < len(pooled):
        j = i
        while j < len(pooled) and pooled[j] == pooled[i]:
            j += 1
        rank[pooled[i]] = (i + 1 + j) / 2
        i = j
    doubled = [int(2 * rank[v]) for v in pooled]  # midranks are multiples of 0.5
    n, m = len(x), len(y)
    ways: list[dict[int, int]] = [dict() for _ in range(n + 1)]  # ways[k][s]: subsets of size k with sum s
    ways[0][0] = 1
    for value in doubled:
        for k in range(min(n, len(doubled)), 0, -1):
            for s, count in ways[k - 1].items():
                ways[k][s + value] = ways[k].get(s + value, 0) + count
    observed = sum(int(2 * rank[v]) for v in x)
    centre = n * (n + m + 1)  # twice the null mean of the rank sum
    total = sum(ways[n].values())
    extreme = sum(c for s, c in ways[n].items() if abs(s - centre) >= abs(observed - centre))
    u = observed / 2 - n * (n + 1) / 2
    return u, min(1.0, extreme / total)


def run(cytometry: Path, source_data: Path) -> dict[str, Any]:
    donors = responses(read_deposit(cytometry))
    replay = reproduction(donors, read_source_data(source_data))
    als = [d["skew_ratio"] for d in donors if d["group"] == "ALS"]
    hc = [d["skew_ratio"] for d in donors if d["group"] == "HC"]
    if not als or not hc:
        raise ValueError("both groups need at least one donor")
    u, p = mann_whitney_exact(als, hc)
    lower = median(als) < median(hc)
    met = lower and p < ALPHA
    return {
        "analysis_id": ANALYSIS_ID,
        "rule": {
            "endpoint": "IFNg / (IL-4 + IL-10), CD4+ T cells, peptide minus vehicle, % of CD4+",
            "floor": FLOOR,
            "test": "exact two-sided Mann-Whitney, ALS vs HC",
            "alpha": ALPHA,
            "replicated_when": "ALS median below HC median and p < alpha",
            "counts": "FlowJo event counts of the authors' gates, read from the deposited workspaces",
        },
        "donors": donors,
        "groups": {"ALS": len(als), "HC": len(hc)},
        "median_skew_ratio": {"ALS": median(als), "HC": median(hc)},
        "median_response": {
            g: {k: median(d["response"][k] for d in donors if d["group"] == g) for k in CYTOKINES}
            for g in ("ALS", "HC")
        },
        "mann_whitney_u": u,
        "p_value": p,
        "reproduction": replay,
        "outcome": "skew_replicated" if met else "skew_not_replicated",
        "negative_result": not met,
        "conclusion": (
            (
                "The CD4+ IFNg / (IL-4 + IL-10) ratio is lower in ALS than in controls "
                f"(exact Mann-Whitney p = {p:.4f}, n = {len(als)} vs {len(hc)})"
            )
            if met
            else (
                "The preregistered rule does not find a lower CD4+ IFNg / (IL-4 + IL-10) ratio in ALS "
                f"(exact Mann-Whitney p = {p:.4f}, n = {len(als)} vs {len(hc)})"
            )
        )
        + f"; {replay['reproduced']} of {replay['donors']} donors reproduce the published values.",
        "limitations": [
            "Counts are those FlowJo saved for the authors' gates; the FCS events were not re-gated.",
            "Responses follow two weeks of in vitro expansion: they are not ex vivo frequencies.",
            "Fifteen donors per group: only large differences are detectable.",
            "No public link from ICS donors to HLA type, C9orf72 status or clinical scores.",
            "Donors whose values do not reproduce the published source data weaken the replication.",
        ],
        "results": [
            {
                "key": "skew_ratio_mann_whitney_p",
                "value": round(p, 6),
                "n": len(als) + len(hc),
                "test": "exact two-sided Mann-Whitney, ALS vs HC",
            },
            {
                "key": "median_skew_ratio_als",
                "value": round(median(als), 4),
                "n": len(als),
                "test": "median of IFNg / (IL-4 + IL-10)",
            },
            {
                "key": "median_skew_ratio_hc",
                "value": round(median(hc), 4),
                "n": len(hc),
                "test": "median of IFNg / (IL-4 + IL-10)",
            },
            {
                "key": "donors_reproducing_source_data",
                "value": replay["reproduced"],
                "n": replay["donors"],
                "test": "exact match of four responses at two decimals",
            },
        ],
    }
