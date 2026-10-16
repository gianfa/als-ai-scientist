"""What a flow-cytometry deposit actually measures, read from the files with the standard library.

Why this exists. Size and file count (`michaelis-zenodo-cytometry` is 141 raw files and 2 GB) are everything
except the one thing a designer needs: **which markers were measured**. Without it an experiment can be bought
that rests on a marker (e.g. IL-5) the deposit has no channel for, and nothing in the pipeline could object,
because the choice would be made on a dataset's name.

What it reads, and what it cannot. An FCS file's TEXT segment carries the channel names (`$PnN`:
`BV785-A`, `FITC-A`, …) and, in this deposit, **empty** `$PnS` stain labels — so the files say which
fluorochromes were recorded and not which antibody each carried. The antibody is recoverable only from the
FlowJo workspace, where a gate is named after it (`IL-17 BV785 +`). This module reports both and keeps them
apart: `channels` is measured fact, `markers` is read from a human's gate names and is therefore evidence about
the panel, not about any cell.

Deliberately not a cytometry library: no compensation, no gating, no event data. One seek and a few kilobytes per
file, a regex over the workspaces. Reproducing a gated readout is a different and larger problem, and this
module exists so that a proposal can be **refused** before anyone attempts it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

HEADER: Final = 58  # FCS 3.x: the fixed-width header, offsets in bytes 10..26
GATE: Final = re.compile(r'<Population\s+name="([^"]+)"')
# A gate name is written by a human: "IL-17 BV785 +", "CD4+", "Q1: IL-10 APC+ , IFNy FITC+". The marker is the
# leading token before the fluorochrome or the sign; anything shorter than two characters is not a marker.
MARKER: Final = re.compile(
    r"\b(IL-?\d+[A-Za-z]?|IFN[a-zγ]?|TNF[a-zα]?|CD\d+[a-z]?|FoxP3|Ki-?67|HLA-[A-Z]+)\b", re.IGNORECASE
)
GROUP_NODE: Final = re.compile(r'<GroupNode\s+name="([^"]+)"')
DONOR_GROUP: Final = re.compile(r"(?i)\bdonor\b")

# Controlled vocabulary for `metadata_fields` (`Panel.metadata_fields`): short, stable keys a designer can name
# verbatim in `required_metadata`, instead of the free prose `assess()` can never exactly match against a
# machine-generated inventory. Each is a structural fact about the deposit's own files, not a biological claim.
METADATA_FIELDS: Final[dict[str, str]] = {
    "fcs_file_name_group_label": "the deposited FCS file names carry two or more distinct leading labels "
    "(e.g. a disease/control group), not just a per-file identifier",
    "fcs_file_name_condition_label": "within a leading label, the deposited FCS file names carry two or more "
    "distinct trailing labels (e.g. a stimulation condition)",
    "flowjo_donor_group": "at least one FlowJo workspace groups samples under a group named 'donor' "
    "(case-insensitive), linking wells to one identity",
}


@dataclass
class Panel:
    """What one deposit measures. `channels` is read from the files, `markers` from the workspaces' gate names."""

    channels: list[str] = field(default_factory=list)
    markers: list[str] = field(default_factory=list)
    fcs_files: int = 0
    workspaces: int = 0
    events: int = 0
    cytometers: list[str] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)
    metadata_fields: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "channels": self.channels,
            "markers": self.markers,
            "fcs_files": self.fcs_files,
            "workspaces": self.workspaces,
            "events": self.events,
            "cytometers": self.cytometers,
            "unreadable": self.unreadable,
            "metadata_fields": self.metadata_fields,
            "note": "`channels` are the recorded fluorochromes (`$PnN`); `markers` are read from the gate "
            "names of the FlowJo workspaces, which is how the antibody panel is recorded in this "
            "deposit. A marker absent here was not measured: an experiment that needs it cannot be "
            "run on this data. `metadata_fields` names which structural facts (group/condition "
            "labels in file names, a FlowJo donor grouping) this deposit's own files carry — "
            "declare `required_metadata` using these exact keys, as for markers; a key absent here "
            "was not found structurally, which does not prove the fact itself is false.",
        }


def keywords(path: Path) -> dict[str, str]:
    """The TEXT segment of one FCS file as a key/value map. Raises nothing the caller cannot handle: a file that
    is not FCS 3.x returns an empty map rather than a guess."""
    with path.open("rb") as handle:
        head = handle.read(HEADER).decode("ascii", "replace")
        if not head.startswith("FCS"):
            return {}
        try:
            begin, end = int(head[10:18]), int(head[18:26])
        except ValueError:
            return {}
        if not 0 < begin <= end:
            return {}
        handle.seek(begin)
        text = handle.read(end - begin + 1).decode("ascii", "replace")
    if not text:
        return {}
    parts = text[1:].split(text[0])  # the first byte is the delimiter the file declares
    return dict(zip(parts[0::2], parts[1::2]))


def channels_of(path: Path) -> tuple[list[str], int, str | None]:
    """The channel names, the event count and the cytometer of one FCS file."""
    kv = keywords(path)
    if not kv:
        return [], 0, None
    try:
        count = int(kv.get("$PAR", "0"))
    except ValueError:
        count = 0
    names = [kv.get(f"$P{i}N", "").strip() for i in range(1, count + 1)]
    try:
        events = int(kv.get("$TOT", "0"))
    except ValueError:
        events = 0
    return [n for n in names if n], events, (kv.get("$CYT") or None)


def markers_of(workspace: Path) -> list[str]:
    """The markers a FlowJo workspace gates on, from its population names. Read as evidence about the panel."""
    try:
        text = workspace.read_text(errors="replace")
    except OSError:
        return []
    found: dict[str, None] = {}
    for name in GATE.findall(text):
        for marker in MARKER.findall(name):
            found.setdefault(canonical_marker(marker), None)
    return list(found)


def canonical_marker(marker: str) -> str:
    """`il17`, `IL-17` and `Il-17` are one marker. Case and the hyphen are the only normalisation here: deciding
    that `IL-17` and `IL-17A` are the same thing is a biological judgement this module does not make.

    Public because any consumer comparing a declared marker name with a stored inventory must compare on the
    same key, as `pipeline/experiment_feasibility.py` does.
    """
    upper = marker.upper()
    return re.sub(r"^(IL|IFN|TNF|CD)-?", lambda m: m.group(1) + "-" if m.group(1) == "IL" else m.group(1), upper)


def marker_family(marker: str) -> str:
    """The key `measures` compares on. It drops the way an interferon or a TNF was spelled — `IFNy`, `IFNG`,
    `IFN-gamma` and `TNFa` name one molecule each in these panels, and a question about "IFN" must not come back
    False because a human typed the Greek letter in a gate name.

    **It is deliberately lossy and must not be used to answer "is this marker present?" on its own**: it maps every
    interferon and every TNF to one key, so `IFN-alpha` and `IFN-gamma` collapse together. A caller that would
    otherwise report a false negative may use it to say "this resembles something in the panel", never to say
    the panel measures it.
    """
    key = canonical_marker(marker)
    return re.sub(r"^(IFN|TNF).*$", r"\1", key)


def metadata_fields_of(root: Path) -> list[str]:
    """Which keys of `METADATA_FIELDS` this deposit's own file names and workspaces structurally support.

    Reads only file names (never FCS content) and workspace `GroupNode` names — the same two sources
    `analysis/experiment_ics_cytokines.py` already parses to find each donor's group, well and condition,
    generalised here to any deposit rather than that one module's fixed naming convention.
    """
    names = [p.name for p in root.rglob("*.fcs")]
    # "<LABEL> <rest>": the leading space-separated token, when the deposit uses one consistently.
    leading = {n.split(" ", 1)[0] for n in names if " " in n}
    found: list[str] = []
    if len(leading) >= 2:
        found.append("fcs_file_name_group_label")
        # Within one leading label, do the trailing tokens (before ".fcs") also vary?
        trailing_by_label: dict[str, set[str]] = {}
        for n in names:
            if " " not in n:
                continue
            label, rest = n.split(" ", 1)
            trailing_by_label.setdefault(label, set()).add(rest.rsplit(" ", 1)[-1].removesuffix(".fcs"))
        if any(len(t) >= 2 for t in trailing_by_label.values()):
            found.append("fcs_file_name_condition_label")
    for workspace in root.rglob("*.wsp"):
        try:
            text = workspace.read_text(errors="replace")
        except OSError:
            continue
        if any(DONOR_GROUP.search(name) for name in GROUP_NODE.findall(text)):
            found.append("flowjo_donor_group")
            break
    return found


def read_panel(root: Path, max_files: int | None = None) -> Panel:
    """Every FCS file and workspace under `root`. `max_files` caps the FCS reads for a quick look."""
    panel = Panel()
    panel.metadata_fields = metadata_fields_of(root)
    channels: dict[str, None] = {}
    markers: dict[str, None] = {}
    cytometers: dict[str, None] = {}
    for path in sorted(root.rglob("*.fcs")):
        if max_files is not None and panel.fcs_files >= max_files:
            break
        try:
            names, events, cytometer = channels_of(path)
        except OSError as err:
            panel.unreadable.append(f"{path.name}: {type(err).__name__}")
            continue
        if not names:
            panel.unreadable.append(f"{path.name}: not readable as FCS 3.x")
            continue
        panel.fcs_files += 1
        panel.events += events
        for name in names:
            channels.setdefault(name, None)
        if cytometer:
            cytometers.setdefault(cytometer, None)
    for path in sorted(root.rglob("*.wsp")):
        panel.workspaces += 1
        for marker in markers_of(path):
            markers.setdefault(marker, None)
    panel.channels, panel.markers = list(channels), list(markers)
    panel.cytometers = list(cytometers)
    return panel


def measures(panel: Panel, marker: str) -> bool:
    """Does the deposit measure this marker? The question `design` should be able to ask before proposing."""
    return marker_family(marker) in {marker_family(m) for m in panel.markers}
