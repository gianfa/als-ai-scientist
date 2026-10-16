"""The extraction read once, indexed by heading, and served in bounded normalised passages.

Everything here is deterministic and read-only: a `PaperDocument` is built from one file,
holds its `sha256`, and answers with line ranges that a reader can check against that file.

Two properties are the point, not conveniences.

**Bounded.** `passage` refuses a request wider than `MAX_PASSAGE_LINES` rather than truncating it, because a
silently shortened answer is one an agent cannot tell from a complete one. The bound is the extraction's own
(`SKILL.md` prescribes reads of at most 60 lines).

**Located.** Every answer says whether its lines lie outside the paper's main text — a figure caption, a table,
a reference entry, the front matter, a section that is not argument. A quotation taken from such a passage is
real but can never come back `exact` from `mcp__literature__check_quote`, which searches the main text only, and
62 of the extraction's 360 sentences are captions. The flag is what lets an agent paraphrase, or cite
the manifest dataset ref for a table, instead of quoting something that will be refused.

**`outside_main_text` is a lower bound, by construction.** In this extraction a caption runs across a blank
line — line 23 opens `**Fig. 1 | ...`, line 24 is empty, line 25 continues the same legend — so a caption has
no structural end and a continuation paragraph is indistinguishable from prose without guessing at its content.
What is detected with certainty is the caption's opening line, tables, reference entries, front matter and
whole non-argument sections. A continuation can therefore be served with the flag unset. This is a deliberate
limit, not an oversight: the gate is `check_quote`, which refuses the quotation anyway, and the flag only spares an agent the wasted attempt. Narrowing it by guessing would trade a
visible limit for an invisible one.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from tools.paper.normalise import normalise_passage

MAX_PASSAGE_LINES: Final = 60
# `# **Donor characteristics**`, `## **Methods**`: the extraction writes every heading in bold.
_HEADING: Final = re.compile(r"^(#+)\s*(.*?)\s*$")
_AFFILIATION: Final = re.compile(r"^\d+[A-Z]?[A-Za-z ]*(Center|Department|Institute|University|Hospital|School)")
_CAPTION: Final = re.compile(r"^\*{0,2}(Fig\.|Figure|Table|Extended Data)\s", re.IGNORECASE)
_REFERENCE: Final = re.compile(r"^-?\s*\[?\d+\]?\.\s")

# Headings whose whole section is provably not the paper's prose. Kept deliberately short: every other
# heading is left to the per-line rules, because guessing from a section's name is how a flag starts lying.
# `Conversion notes` is the converter's own commentary (it is listed as such in the extraction's `index.md`).
_OUTSIDE_SECTIONS: Final = ("references", "methods references", "extended data table", "conversion notes")


def _region(line: str, heading: str) -> str:
    """Which part of the paper a line belongs to. Only `text` can yield a quotation that verifies `exact`."""
    stripped = line.strip()
    if not stripped:
        return "blank"
    if stripped.startswith("#"):
        return "heading"
    if stripped.startswith(("|", "<a id=", "![", ">")):
        return "markup"
    if _CAPTION.match(stripped):
        return "caption"
    if _AFFILIATION.match(stripped):
        return "frontmatter"
    if any(heading.lower().startswith(name) for name in _OUTSIDE_SECTIONS):
        return "outside_section"
    if _REFERENCE.match(stripped) and len(stripped) > 40 and re.search(r"_[A-Z]", stripped):
        return "reference"
    return "text"


@dataclass(frozen=True)
class Section:
    heading: str
    level: int
    start: int  # 1-based, the heading's own line
    end: int  # 1-based, inclusive
    characters: int
    outside_main_text: bool


class PaperDocument:
    """One extraction file, indexed. Built once per server process; never written to."""

    def __init__(self, path: Path) -> None:
        self.path = path
        raw = path.read_bytes()
        self.sha256 = hashlib.sha256(raw).hexdigest()
        self.lines = raw.decode("utf-8").splitlines()
        self.sections = self._index()

    def _index(self) -> list[Section]:
        starts: list[tuple[int, int, str]] = []
        for number, line in enumerate(self.lines, 1):
            match = _HEADING.match(line)
            if match and line.startswith("#"):
                starts.append((number, len(match.group(1)), normalise_passage(match.group(2))))
        sections: list[Section] = []
        for index, (start, level, heading) in enumerate(starts):
            end = starts[index + 1][0] - 1 if index + 1 < len(starts) else len(self.lines)
            body = self.lines[start:end]
            regions = {_region(line, heading) for line in body}
            # A heading that only introduces subsections has no content of its own: it is not "outside the main
            # text", there is simply nothing in it to quote.
            quotable = regions - {"blank", "heading", "markup"}
            sections.append(
                Section(
                    heading=heading,
                    level=level,
                    start=start,
                    end=end,
                    characters=sum(len(line) for line in body),
                    outside_main_text=bool(quotable) and "text" not in quotable,
                )
            )
        return sections

    def find(self, heading: str) -> Section | None:
        """Exact heading first, then a unique case-insensitive prefix; ambiguity is not resolved by guessing."""
        for section in self.sections:
            if section.heading == heading:
                return section
        wanted = heading.strip().lower()
        matches = [s for s in self.sections if s.heading.lower().startswith(wanted)] if wanted else []
        return matches[0] if len(matches) == 1 else None

    def navigate(self) -> dict[str, object]:
        return {
            "source": self.path.name,
            "sha256": self.sha256,
            "lines": len(self.lines),
            "max_passage_lines": MAX_PASSAGE_LINES,
            "sections": [
                {
                    "heading": s.heading,
                    "level": s.level,
                    "lines": [s.start, s.end],
                    "characters": s.characters,
                    "outside_main_text": s.outside_main_text,
                }
                for s in self.sections
            ],
        }

    def passage(
        self, heading: str | None = None, start: int | None = None, end: int | None = None
    ) -> dict[str, object]:
        """A normalised passage, by heading or by line range. Refuses rather than truncates."""
        if heading is not None:
            section = self.find(heading)
            if section is None:
                return self._refused(
                    "heading_not_found", f"no single section matches {heading!r}; call paper_navigate for the list"
                )
            start, end = section.start, section.end
        if start is None or end is None:
            return self._refused("no_target", "give a heading, or both start and end line numbers")
        if start < 1 or end > len(self.lines) or start > end:
            return self._refused("out_of_range", f"lines {start}-{end} are not inside 1-{len(self.lines)}")
        if end - start + 1 > MAX_PASSAGE_LINES:
            return self._refused(
                "too_wide",
                f"{end - start + 1} lines requested, at most {MAX_PASSAGE_LINES} are served; "
                "ask for a narrower range or a subsection",
            )
        body = self.lines[start - 1 : end]
        enclosing = self._enclosing_heading(start)
        regions = [_region(line, enclosing) for line in body]
        quotable = set(regions) - {"blank", "heading", "markup"}
        # Heading lines are dropped: the heading is already a field of the answer, and `# Methods` is not text
        # the store contains, so leaving it in would only produce quotations that cannot verify.
        text = "\n".join(
            p for line, where in zip(body, regions) if where != "heading" and (p := normalise_passage(line))
        )
        return {
            "status": "served",
            "source": self.path.name,
            "sha256": self.sha256,
            "lines": [start, end],
            "heading": enclosing,
            "text": text,
            "outside_main_text": bool(quotable) and "text" not in quotable,
            "note": (
                "this passage is a caption, table, reference or front matter: a quotation from it is "
                "real but check_quote cannot return 'exact' for it, because it searches the main text. "
                "Paraphrase, or cite the manifest dataset ref for a table."
                if quotable and "text" not in quotable
                else None
            ),
        }

    def _enclosing_heading(self, line: int) -> str:
        enclosing = ""
        for section in self.sections:
            if section.start <= line <= section.end:
                enclosing = section.heading
        return enclosing

    def _refused(self, reason: str, message: str) -> dict[str, object]:
        return {
            "status": "refused",
            "reason": reason,
            "message": message,
            "source": self.path.name,
            "sha256": self.sha256,
        }
