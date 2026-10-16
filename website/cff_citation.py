"""Render CITATION.cff as the BibTeX block of the project page, so the page cites what CITATION.cff says.

Called by website/build.sh, which writes the output to the build tree as `_citation.md`; index.qmd includes it.
CITATION.cff is the source of truth for authorship (AGENTS.md): the page has no citation text of its own.

    python cff_citation.py <CITATION.cff>   # prints a Markdown fenced bibtex block

The work itself becomes a @software entry; each entry of `references` follows, a `data` one as @dataset. A missing
title, author or year is a failure: a half-filled citation would be published without anyone noticing.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

ENTRY_TYPES = {"software": "software", "data": "dataset"}


def fail(message: str) -> None:
    print(f"[fail] cff_citation: {message}", file=sys.stderr)
    sys.exit(1)


def bibtex_value(text: str) -> str:
    return re.sub(r"([&%$#_])", r"\\\1", str(text))


def author_name(author: dict) -> str:
    if "family-names" in author:
        given = author.get("given-names")
        return f"{author['family-names']}, {given}" if given else str(author["family-names"])
    if "name" in author:
        return "{" + str(author["name"]) + "}"  # an entity: braces keep BibTeX from splitting it
    fail(f"an author has neither family-names nor name: {author}")
    return ""


def year_of(work: dict) -> str:
    if "year" in work:
        return str(work["year"])
    if "date-released" in work:
        return str(work["date-released"])[:4]
    fail(f"'{work.get('title')}' has no year and no date-released")
    return ""


def entry(work: dict, kind: str) -> str:
    for field in ("title", "authors"):
        if not work.get(field):
            fail(f"a {kind} entry has no {field}")
    first = work["authors"][0]
    family = str(first.get("family-names") or first.get("name") or "")
    words = re.findall(r"[a-z0-9]+", work["title"].lower())
    key = re.sub(r"[^a-z0-9]", "", family.lower()) + year_of(work) + "".join(words[:3])
    fields = [
        ("author", " and ".join(author_name(a) for a in work["authors"])),
        ("title", "{" + bibtex_value(work["title"]) + "}"),
        ("version", work.get("version")),
        ("year", year_of(work)),
        ("doi", work.get("doi")),
        ("url", work.get("repository-code") or work.get("url")),
    ]
    present = [(name, str(value)) for name, value in fields if value]
    width = max(len(name) for name, _ in present)
    body = ",\n".join(f"  {name.ljust(width)} = {{{value}}}" for name, value in present)
    return f"@{ENTRY_TYPES.get(kind, 'misc')}{{{key},\n{body}\n}}"


def render(cff: dict) -> str:
    entries = [entry(cff, "software")]
    for reference in cff.get("references") or []:
        entries.append(entry(reference, reference.get("type", "misc")))
    return "```bibtex\n" + "\n\n".join(entries) + "\n```\n"


def main() -> None:
    if len(sys.argv) != 2:
        fail("usage: cff_citation.py <CITATION.cff>")
    cff = yaml.safe_load(Path(sys.argv[1]).read_text())
    if not isinstance(cff, dict):
        fail("CITATION.cff is not a mapping")
    print(render(cff), end="")


if __name__ == "__main__":
    main()
