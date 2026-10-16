"""Local data sources of a run: declared in the config, resolved and hashed here, never guessed.

Before this module every analysis reached its input through a hardcoded path with an implicit default
(`analysis/runner.py` looked under `dist/michaelis-2025-agent/...`), so a run could not say where its data came
from, and moving a file changed the result silently. A phase that claims provenance cannot depend on a hidden
default: each source is declared in `configs/pipeline*.yaml`, resolved relative to the repository root, and
hashed. A declared source that is missing raises, because a missing input must fail explicitly rather than
produce a run with less evidence than it reports.

The manifest this builds is the provenance record the computed claims point at: a claim's `source_refs` carries
`dataset:<id>@<sha256-prefix>`, so re-running the analysis on a changed file yields a different reference.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

HASH_PREFIX = 12  # enough to tell two versions of one file apart in a citation
_CHUNK = 1 << 20


@dataclass(frozen=True)
class Acquisition:
    """How a user obtains a source, as the missing-source report prints it.

    Each step is `("run", command)` or `("by hand", instruction)`. A command is printed alone on its line so it can
    be pasted into a shell as is: a single prose string mixing commands, manual steps and a doc pointer gets pasted
    whole, and the shell runs its prose.
    """

    see: str  # the documented procedure, e.g. "docs/data-acquisition.md section 4"
    steps: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class LocalSource:
    """One declared input file or directory, with the licence note that governs it."""

    id: str
    path: Path
    kind: str  # table | sumstats | supplement | raw
    license_note: str
    required: bool = True
    acquire: Acquisition | None = None  # how a user obtains it

    def __post_init__(self) -> None:
        if not isinstance(self.path, Path):  # the config declares a string
            object.__setattr__(self, "path", Path(self.path))

    def exists(self) -> bool:
        return self.path.exists()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _directory_digest(path: Path) -> tuple[str, int, int]:
    """(digest, file count, bytes) of a directory: the hash of each file's relative path and content hash.

    Hashing the names together with the contents makes a renamed file a different dataset version, which is what
    a provenance reference must say. Hidden files are included: an input is what is on disk, not what we expect.
    """
    digest = hashlib.sha256()
    count = size = 0
    for file in sorted(p for p in path.rglob("*") if p.is_file()):
        digest.update(str(file.relative_to(path)).encode())
        digest.update(sha256_file(file).encode())
        count += 1
        size += file.stat().st_size
    return digest.hexdigest(), count, size


def sha256_path(path: Path) -> str:
    """The digest a manifest entry records for `path`: of the file, or of the directory as `describe` hashes it."""
    return _directory_digest(path)[0] if path.is_dir() else sha256_file(path)


def describe(source: LocalSource, root: Path) -> dict[str, Any]:
    """The manifest entry of one existing source: hash, size, and the mtime as the best available access date."""
    path = source.path
    if path.is_dir():
        digest, files, size = _directory_digest(path)
    else:
        digest, files, size = sha256_file(path), 1, path.stat().st_size
    try:  # a path inside the repository is recorded relative to it
        shown = str(path.relative_to(root))
    except ValueError:
        shown = str(path)
    entry = {
        "id": source.id,
        "kind": source.kind,
        "path": shown,
        "sha256": digest,
        "ref": f"dataset:{source.id}@{digest[:HASH_PREFIX]}",
        "files": files,
        "bytes": size,
        "is_directory": path.is_dir(),
        "modified_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
        "license_note": source.license_note,
    }
    shape = _shape(path)
    return entry | ({"shape": shape} if shape else {})


def _shape(path: Path) -> dict[str, Any] | None:
    """What the source *measures*, when that is readable from the files themselves.

    Size and a hash do not let a designer choose: a manifest that describes a deposit as "141 raw files, 2 GB"
    and nothing else lets an experiment rest on a marker the deposit never recorded. A flow-cytometry deposit can say what it
    recorded, cheaply and deterministically, so it does.
    """
    if not path.is_dir() or not next(path.rglob("*.fcs"), None):
        return None
    from .cytometry import read_panel

    return read_panel(path).as_dict()


def resolve(sources: Iterable[LocalSource], root: Path) -> list[LocalSource]:
    """Make every declared path absolute against the repository root, keeping the declaration order."""
    return [
        LocalSource(
            id=s.id,
            path=s.path if s.path.is_absolute() else root / s.path,
            kind=s.kind,
            license_note=s.license_note,
            required=s.required,
            acquire=s.acquire,
        )
        for s in sources
    ]


class MissingSource(FileNotFoundError):
    """A source the config declares as required is not on disk."""


def _missing_report(missing: list[LocalSource], root: Path) -> str:
    """One block per absent source: what is missing, where it was looked for, and how to obtain it.

    The acquisition procedure is read from the declaration, not from a table kept here: a new input is
    declared in one place and its absence explains itself. A source that declares none says so, which is a
    gap in the configuration and should read like one.
    """
    blocks = [f"declared data source(s) not found: {len(missing)}", ""]
    first_with: dict[Acquisition, str] = {}
    for source in missing:
        shown = source.path.relative_to(root) if source.path.is_relative_to(root) else source.path
        blocks.append(f"  {source.id}")
        blocks.append(f"    expected at: {shown}")
        if source.acquire is None:
            blocks.append("    to obtain it: no procedure declared for this source in the configuration")
        elif source.acquire in first_with:  # one procedure producing several files is printed once
            blocks.append(f"    to obtain it: same procedure as {first_with[source.acquire]}")
        else:
            first_with[source.acquire] = source.id
            blocks.append(f"    to obtain it ({source.acquire.see}):")
            blocks += [
                f"      $ {text}" if kind == "run" else f"      by hand: {text}" for kind, text in source.acquire.steps
            ]
    blocks += ["", "No third-party data ships with this repository. See docs/data-acquisition.md."]
    return "\n".join(blocks)


def manifest(sources: Iterable[LocalSource], root: Path) -> dict[str, Any]:
    """Resolve, check and hash every declared source.

    Raises `MissingSource` naming every required source that is absent, rather than the first one: a user who has
    to fetch three files should learn that in one run. An optional source that is absent is recorded as such, so
    the manifest states what the run did not see.
    """
    resolved = resolve(sources, root)
    missing = [s for s in resolved if s.required and not s.exists()]
    if missing:
        raise MissingSource(_missing_report(missing, root))
    entries = [describe(s, root) for s in resolved if s.exists()]
    absent = [{"id": s.id, "path": str(s.path), "required": False} for s in resolved if not s.exists()]
    return {
        "root": str(root),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sources": entries,
        "absent": absent,
        "bytes": sum(e["bytes"] for e in entries),
        "files": sum(e["files"] for e in entries),
    }


def by_id(manifest_data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The manifest's entries keyed by source id, for an analysis that needs one input by name."""
    return {entry["id"]: entry for entry in manifest_data["sources"]}


def path_of(manifest_data: dict[str, Any], source_id: str, root: Path) -> Path:
    """The absolute path of a source the manifest recorded; raises if the run never saw it."""
    entry = by_id(manifest_data).get(source_id)
    if entry is None:
        raise MissingSource(f"the manifest holds no source {source_id!r}")
    path = Path(entry["path"])
    return path if path.is_absolute() else root / path


__all__ = [
    "HASH_PREFIX",
    "Acquisition",
    "LocalSource",
    "MissingSource",
    "by_id",
    "describe",
    "manifest",
    "path_of",
    "resolve",
    "sha256_file",
    "sha256_path",
]
