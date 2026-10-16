"""Does a sentence of the Paper2Agent extraction verify against the literature store?

A `paper` MCP server would serve passages of `dist/.../references/paper.md`, but a
quotation an agent lifts from such a passage is verified by `mcp__literature__check_quote`, which searches the
store's rendering of the same paper. The two texts are not the same text — 84 799 characters against 56 147 —
so the question is how often a served sentence would come back `exact`.

Read-only, offline, deterministic. It uses `assess_quote`, the function behind `check_quote`, so the verdicts
are the ones an agent would actually get, not an approximation of them.

    PYTHONPATH=. uv run python scripts/dev/measure_extraction_agreement.py

The normalisation lives in `tools.paper.normalise`, which the future `paper` server will serve its passages
through, so the measurement and the server cannot drift apart. It is not cosmetic and is this measurement's main
finding: the extraction marks
citations as `<sup>7,8</sup>` and also writes `CD4<sup>+</sup>`. Dropping every superscript costs 22 points of
exact agreement, because `CD4+ T cells` becomes `CD4 T cells`. A superscript of digits, commas and dashes is a
citation marker and must go; anything else belongs to the word and must stay. A server that serves raw markdown
would inherit the lower number.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from pipeline.config import load_config
from tools.literature.quotes import assess_quote
from tools.literature.store import LiteratureStore
from tools.paper.normalise import normalise_passage

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
EXTRACTION = PACKAGE_ROOT.parent / "dist/michaelis-2025-agent/skill/michaelis-2025-paper/references/paper.md"
TITLE = "Autoimmune response to C9orf72 protein in amyotrophic lateral sclerosis."
MIN_WORDS = 8
SENTENCE_SPLIT = re.compile(
    r"(?<![A-Z])(?<!Fig)(?<!ref)(?<!refs)(?<!no)(?<!No)(?<!vs)(?<!e\.g)(?<!i\.e)(?<!approx)(?<!et al)"
    r"(?<=[.!?])\s+(?=[A-Z(])"
)
AFFILIATION = re.compile(r"^\d+[A-Z]?[A-Za-z ]*(Center|Department|Institute|University|Hospital|School)")


def region(line: str) -> str:
    """Which part of the paper a line belongs to; only `text` is a candidate for quotation."""
    stripped = line.strip()
    if not stripped:
        return "blank"
    if stripped.startswith(("|", "<a id=", "![", ">")):
        return "markup"
    if stripped.startswith("#"):
        return "heading"
    if AFFILIATION.match(stripped):
        return "frontmatter"
    if re.match(r"^-?\s*\[?\d+\]?\.\s", stripped) and len(stripped) > 40 and re.search(r"_[A-Z]", stripped):
        return "reference"
    return "text"


def main() -> int:
    if not EXTRACTION.is_file():
        print(f"missing extraction: {EXTRACTION}", file=sys.stderr)
        return 1
    store = LiteratureStore(load_config().store_path())
    record = next((r for r in store.records() if r.title == TITLE), None)
    if record is None or not record.text_path:
        print(f"no stored full text for {TITLE!r}", file=sys.stderr)
        return 1
    main_text = store.resolve(record.text_path).read_text(encoding="utf-8")
    extras = store.companion(record, "extras") or ""
    refs = store.companion(record, "refs") or ""
    completeness = "full_jats" if record.extractor == "jats" else "partial" if record.extractor else "unknown"

    markdown = EXTRACTION.read_text(encoding="utf-8")
    sentences: list[tuple[int, str, str]] = []
    for number, line in enumerate(markdown.splitlines(), 1):
        where = region(line)
        if where in ("blank", "markup", "heading", "frontmatter"):
            continue
        for sentence in SENTENCE_SPLIT.split(normalise_passage(line)):
            if len(sentence.split()) >= MIN_WORDS:
                sentences.append((number, where, sentence))

    verdicts: dict[tuple[str, str], int] = {}
    for _, where, sentence in sentences:
        assessed = assess_quote(sentence, main_text, extras, refs, completeness=completeness)
        verdict = (
            assessed.check.verdict
            if assessed.check.verdict == "exact"
            else assessed.assessment or assessed.check.verdict
        )
        verdicts[(where, verdict)] = verdicts.get((where, verdict), 0) + 1

    print(
        f"extraction {len(markdown)} chars / {len(markdown.splitlines())} lines; "
        f"store main {len(main_text)} chars, extras {len(extras)}, refs {len(refs)} "
        f"(extractor {record.extractor!r}, completeness {completeness})"
    )
    print(f"sentences of >= {MIN_WORDS} words: {len(sentences)}")
    for where in sorted({w for w, _ in verdicts}):
        rows = {v: n for (w, v), n in verdicts.items() if w == where}
        total, exact = sum(rows.values()), rows.get("exact", 0)
        outside = rows.get("in_paper_outside_main_text", 0)
        body = total - outside
        share = f"{exact / body:.1%} of the {body}" if body else "no sentence"
        print(
            f"{where:10s} n={total:4d}  exact={exact:4d} ({exact / total:.1%} of all, "
            f"{share} not located outside the main text)  {rows}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
