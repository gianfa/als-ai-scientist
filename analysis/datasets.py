import csv
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

_RESPONSE = re.compile(r"^(\d+)/(\d+) \((\d+)%\)$")
# Table 1's footnote "a" marks a phosphorylated peptide ("<sup>a</sup> Phosphorylated peptides", legend of the
# table). It is data, not formatting: it becomes `Epitope.phospho`, and in two rows it sits inside the sequence.
# Two Paper2Agent extractions of the same PDF wrote it as HTML and as the Unicode modifier letter, so both are
# recognised rather than one being treated as malformed.
_PHOSPHO = r"(?:<sup>a</sup>|ᵃ)"
# The form the label takes in `Epitope.residue_range`, whichever form the extraction wrote.
_PHOSPHO_LABEL = "<sup>a</sup>"
_RANGE = re.compile(rf"^(\d+)–(\d+)({_PHOSPHO})?$")
# A peptide is written in upper-case letters. This refuses a leftover footnote marker, punctuation, a digit or a
# lower-case character; it does not check that each letter is one of the twenty amino acids, because the test
# fixtures of this repository use placeholder sequences.
_SEQUENCE = re.compile(r"^[A-Z]+$")


@dataclass(frozen=True)
class Epitope:
    residue_range: str
    start: int
    sequence: str
    k: int
    n: int
    printed_pct: int
    phospho: bool


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _clean(value):
    return value.replace("**", "").strip()


_MARKUP = re.compile(r"<sup>.*?</sup>|\*\*|(?<!\w)_|_(?!\w)")
TABLE1_COLUMNS = ("Peptide residue", "Peptide sequence", "Response frequency (%)", "Average magnitude")


def column_name(header: str) -> str:
    """A header as the paper prints it, without the markup an extraction adds around it.

    Two Paper2Agent extractions of the same PDF wrote Table 1's first header as `**Peptide**\n**residue**` and as
    `Peptide residue`: same column, different formatting. The schema check compares the printed words, so a
    re-extraction passes and a genuinely different table still fails.
    """
    return " ".join(_MARKUP.sub("", header).split())


def load_table1(path: Path):
    rows = []
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if tuple(column_name(h) for h in reader.fieldnames or ()) != TABLE1_COLUMNS:
            raise ValueError(f"table1 schema drift: {reader.fieldnames!r}, expected the columns {TABLE1_COLUMNS!r}")
        for line, raw in enumerate(reader, 2):
            match = _RANGE.fullmatch(_clean(raw[reader.fieldnames[0]]))
            if not match:
                raise ValueError(f"table1 line {line}: malformed residue range")
            # The label is rebuilt from the parsed values, so both extractions give the same string for the same row.
            residue = f"{match.group(1)}–{match.group(2)}" + (_PHOSPHO_LABEL if match.group(3) else "")
            response = _clean(raw[reader.fieldnames[2]])
            parsed = _RESPONSE.fullmatch(response)
            if not parsed:
                raise ValueError(f"table1 line {line}: malformed response {response!r}")
            sequence = re.sub(_PHOSPHO, "", _clean(raw[reader.fieldnames[1]]))
            if not _SEQUENCE.fullmatch(sequence):  # a marker left in would corrupt the peptide silently
                raise ValueError(f"table1 line {line}: malformed peptide sequence {sequence!r}")
            rows.append(
                Epitope(
                    residue,
                    int(match.group(1)),
                    sequence,
                    int(parsed.group(1)),
                    int(parsed.group(2)),
                    int(parsed.group(3)),
                    bool(match.group(3)),
                )
            )
    return rows


def load_table2(path: Path):
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if len(reader.fieldnames or ()) != 4:
            raise ValueError(f"table2 schema drift: {reader.fieldnames!r}")
        return list(reader)
