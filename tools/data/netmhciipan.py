"""Cache-first client for the IEDB MHC-II binding prediction API (NetMHCIIpan).

`predict` mirrors the API parameters (`method`, `sequence_text`, `allele`, `length`) but adds a
deterministic layer: every (method, length, allele, peptide) result is stored in an append-only
JSONL cache and later calls are served from it. The network is only touched for missing pairs and
can be forbidden altogether (`allow_network=False`), so a result set can be replayed offline.

Limits, stated rather than hidden:
- The API response carries no tool version. The cache records the retrieval time and the URL,
  not a model version; a service-side update would not be detected. `DEFAULT_METHOD` requests no
  version, so the service's default applies: NetMHCIIpan 4.1 for `netmhciipan_el`, as the API lists it
  (`curl --data method_version <API_URL>`).
- Each cached result is the first one retrieved. Nothing is overwritten; `refresh=True` appends
  a new record and the newest one is then used (older ones stay in the file for audit).
- Determinism of the service itself is not established (single request per pair so far).

References:
- NetMHCIIpan 4.1: Kaabinejadian S, Barra C, Alvarez B, Yari H, Hildebrand WH, Nielsen M. Accurate MHC
  motif deconvolution of immunopeptidomics data reveals a significant contribution of DRB3, 4 and 5 to the
  total DR immunopeptidome. Front Immunol. 2022;13. doi:10.3389/fimmu.2022.835454
- IEDB Analysis Resource: Dhanda SK, Mahajan S, Paul S, et al. IEDB-AR: immune epitope database-analysis
  resource in 2019. Nucleic Acids Res. 2019;47(W1):W502-W506. doi:10.1093/nar/gkz452
"""

from __future__ import annotations

import csv
import io
import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping

API_URL = "https://tools-cluster-interface.iedb.org/tools_api/mhcii/"
DEFAULT_METHOD = "netmhciipan_el"
DEFAULT_CACHE = Path("data/external/iedb-cache/netmhciipan.jsonl")
_COLUMNS = ("allele", "seq_num", "start", "end", "length", "core_peptide", "peptide", "score", "rank")

Transport = Callable[[Mapping[str, str]], str]


class CacheMiss(RuntimeError):
    """A result is not cached and the network is not allowed."""


class MalformedResponse(ValueError):
    """The service answered, but not with the expected table."""


@dataclass(frozen=True)
class Prediction:
    allele: str
    peptide: str
    core_peptide: str
    length: int
    score: float
    rank: float
    method: str


def http_transport(params: Mapping[str, str], url: str = API_URL, timeout: float = 120.0) -> str:
    """POST the form-encoded parameters and return the response body (raises on HTTP errors)."""
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=timeout) as resp:
        return resp.read().decode()


def _key(method: str, length: int, allele: str, peptide: str) -> str:
    return "|".join((method, str(length), allele, peptide))


def _load_cache(path: Path) -> dict[str, dict]:
    records: dict[str, dict] = {}
    if path.is_file():
        for line in path.read_text().splitlines():
            if line.strip():
                rec = json.loads(line)
                records[rec["key"]] = rec  # later lines win: newest record is used
    return records


def _append(path: Path, records: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        for rec in records:
            handle.write(json.dumps(rec, sort_keys=True) + "\n")


def parse_response(text: str) -> list[dict[str, str]]:
    """Parse the tab-separated table; fail loudly on anything else (e.g. an HTML error page)."""
    rows = list(csv.reader(io.StringIO(text.strip()), delimiter="\t"))
    if not rows or tuple(rows[0][: len(_COLUMNS)]) != _COLUMNS:
        raise MalformedResponse(f"unexpected response header: {text[:120]!r}")
    return [dict(zip(_COLUMNS, row)) for row in rows[1:] if row]


def predict(
    sequence_text: str | Iterable[str],
    allele: str | Iterable[str],
    *,
    length: int = 15,
    method: str = DEFAULT_METHOD,
    cache_path: Path | str = DEFAULT_CACHE,
    transport: Transport = http_transport,
    allow_network: bool = True,
    refresh: bool = False,
) -> list[Prediction]:
    """Return one `Prediction` per (peptide, allele) pair, in input order.

    `sequence_text` and `allele` accept one string (alleles comma-separated, as in the API) or an
    iterable. Peptides must already have the requested `length`: the API would otherwise slide a
    window over longer sequences, which this wrapper does not model.
    """
    peptides = _as_list(sequence_text, "\n")
    alleles = _as_list(allele, ",")
    bad = [p for p in peptides if len(p) != length]
    if bad:
        raise ValueError(f"peptides must have length {length}: {bad[:3]}")

    cache_path = Path(cache_path)
    cache = _load_cache(cache_path)
    new_records: list[dict] = []

    for pep in peptides:
        missing = [a for a in alleles if refresh or _key(method, length, a, pep) not in cache]
        if not missing:
            continue
        if not allow_network:
            raise CacheMiss(f"{pep} x {missing} not cached (method={method}, length={length})")
        params = {"method": method, "sequence_text": pep, "allele": ",".join(missing), "length": str(length)}
        rows = {r["allele"]: r for r in parse_response(transport(params))}
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for a in missing:
            if a not in rows:
                raise MalformedResponse(f"no row for allele {a!r} and peptide {pep}")
            rec = {
                "key": _key(method, length, a, pep),
                "request": params,
                "row": rows[a],
                "retrieved_at": stamp,
                "url": API_URL,
            }
            cache[rec["key"]] = rec
            new_records.append(rec)

    if new_records:
        _append(cache_path, new_records)

    out = []
    for pep in peptides:
        for a in alleles:
            row = cache[_key(method, length, a, pep)]["row"]
            out.append(
                Prediction(
                    a, pep, row["core_peptide"], int(row["length"]), float(row["score"]), float(row["rank"]), method
                )
            )
    return out


def _as_list(value: str | Iterable[str], sep: str) -> list[str]:
    items = value.split(sep) if isinstance(value, str) else list(value)
    cleaned = [i.strip() for i in items if i.strip()]
    if not cleaned:
        raise ValueError("empty peptide/allele list")
    return list(dict.fromkeys(cleaned))  # de-duplicate, keep order


__all__ = ["CacheMiss", "MalformedResponse", "Prediction", "predict", "parse_response"]
