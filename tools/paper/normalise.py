"""The extraction's markdown reduced to the paper's words, so a quotation taken from it can be verified.

A passage served to an agent is quoted back by that agent and checked by `mcp__literature__check_quote`, which
searches the literature store's rendering of the same paper. The two renderings differ in markup, and markup is
what decides whether a faithful quotation comes back `exact`.

**The rule that earns its place** (measured with `scripts/dev/measure_extraction_agreement.py`): the
extraction writes citation markers as `<sup>7,8</sup>` and also writes `CD4<sup>+</sup>`. Dropping every
superscript turns `CD4+ T cells` into `CD4 T cells`, which the store does not contain, and costs **22 points**
of exact agreement — 52.9 % against 74.7 % of all main-text sentences. So a superscript whose content is only
digits, commas, spaces and dashes is a citation marker and goes; any other superscript or subscript belongs to
the word and stays. With the rule in place, 90.3 % of the main-text sentences not located in a caption verify
`exact`.

Everything here is a pure function over a string: no I/O, no state, no knowledge of where the text came from.
"""

from __future__ import annotations

import re
from typing import Final

# A superscript or subscript that is only digits, commas, spaces and dashes: a reference marker, not a word.
CITATION_MARKER: Final = re.compile(r"[\d,\s–—-]+")
_SCRIPT_TAG: Final = re.compile(r"<(sup|sub)>([^<]*)</\1>")
# `<a id="p0001"></a>` anchors, and any other bare inline tag the conversion left behind.
_INLINE_TAG: Final = re.compile(r"<a id=[^>]*>|</?[a-z][a-z0-9]*\s*/?>")
# `[text](<url>)` and `[text](url)`: the link text is the paper's, the target is the conversion's.
_LINK: Final = re.compile(r"\[([^\]]*)\]\(<?[^)]*>?\)")
# Markdown emphasis. An underscore inside a word (`IL_6`, `snake_case`) is left alone.
_EMPHASIS: Final = re.compile(r"\*\*|__|\*|(?<![A-Za-z0-9])_|_(?![A-Za-z0-9])")
_ENTITIES: Final = (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&nbsp;", " "))
_WHITESPACE: Final = re.compile(r"\s+")


def _script(match: re.Match[str]) -> str:
    content = match.group(2)
    if not content.strip():
        # `a value<sup> </sup>here`: the tag carries nothing, but its space is the only one between two words.
        # Dropping it outright glues them into one token and the quotation stops matching.
        return " "
    return "" if CITATION_MARKER.fullmatch(content) else content


def normalise_passage(text: str) -> str:
    """The paper's words, with the extraction's markup removed and its meaning-bearing scripts kept.

    Order matters: scripts resolve before tags, so `</sup>` is not eaten as a bare tag; entities resolve after
    tags, so an escaped `&lt;i&gt;` does not become a tag to strip; whitespace collapses last.
    """
    text = _SCRIPT_TAG.sub(_script, text)
    text = _INLINE_TAG.sub("", text)
    text = _LINK.sub(r"\1", text)
    text = _EMPHASIS.sub("", text)
    text = text.replace("\\", "")
    for entity, char in _ENTITIES:
        text = text.replace(entity, char)
    return _WHITESPACE.sub(" ", text).strip()
