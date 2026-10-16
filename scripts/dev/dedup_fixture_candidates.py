"""Candidate entity pairs drawn from the live knowledge base, for the dedup fixture.

The knowledge base is git-ignored, so the fixture `tests/data/entity-pairs.csv` is committed and
this script is how it was produced. It only proposes pairs; the `duplicate` column is human-labelled.

Bands, so the fixture covers every branch of the deterministic stage rather than the easy cases only:

  A  entity_key collision   every pair inside each group (the merges the current code would already make)
  B  Jaccard == 1           same token set, different order
  C  0.8 < Jaccard < 1      the review band
  D1 containment, 1-2 extra tokens
  D2 containment, >= 4 extra tokens
"""

from __future__ import annotations

import argparse
import csv
import itertools
import json
import random
from collections import defaultdict
from pathlib import Path

from knowledge_base.schemas import entity_key

FIELDS = ("pair_id", "band", "a_id", "a_name", "b_id", "b_name", "duplicate", "reason")


def _load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def candidates(entities: list[dict], seed: int = 7, d1: int = 25, d2: int = 10) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for entity in entities:
        groups[entity_key(entity["canonical_name"])].append(entity)

    rows: list[dict] = []
    for key in sorted(k for k, v in groups.items() if len(v) > 1):
        for left, right in itertools.combinations(sorted(groups[key], key=lambda e: e["id"]), 2):
            rows.append(
                {
                    "band": "A",
                    "a_id": left["id"],
                    "a_name": left["canonical_name"],
                    "b_id": right["id"],
                    "b_name": right["canonical_name"],
                }
            )

    first = {k: sorted(v, key=lambda e: e["id"])[0] for k, v in groups.items()}
    keys = sorted(first)
    bands: dict[str, list[tuple[str, str]]] = {"B": [], "C": [], "D1": [], "D2": []}
    for one, two in itertools.combinations(keys, 2):
        left, right = (one, two) if len(one.split()) <= len(two.split()) else (two, one)
        lt, rt = left.split(), right.split()
        if f" {left} " in f" {right} ":
            extra = len(rt) - len(lt)
            if extra <= 2:
                bands["D1"].append((left, right))
            elif extra >= 4:
                bands["D2"].append((left, right))
            continue
        shared, union = set(lt) & set(rt), set(lt) | set(rt)
        jaccard = len(shared) / len(union)
        if jaccard == 1:
            bands["B"].append((left, right))
        elif 0.8 < jaccard < 1:
            bands["C"].append((left, right))

    rng = random.Random(seed)
    for band, pairs in (
        ("B", bands["B"]),
        ("C", bands["C"]),
        ("D1", rng.sample(sorted(bands["D1"]), min(d1, len(bands["D1"])))),
        ("D2", rng.sample(sorted(bands["D2"]), min(d2, len(bands["D2"])))),
    ):
        for left, right in pairs:
            a, b = first[left], first[right]
            rows.append(
                {
                    "band": band,
                    "a_id": a["id"],
                    "a_name": a["canonical_name"],
                    "b_id": b["id"],
                    "b_name": b["canonical_name"],
                }
            )

    for index, row in enumerate(rows, start=1):
        row["pair_id"] = f"P-{index:03d}"
        row.setdefault("duplicate", "")
        row.setdefault("reason", "")
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entities", type=Path, default=Path("knowledgebase/entities.jsonl"))
    parser.add_argument("--out", type=Path, default=Path("tests/data/entity-pairs.csv"))
    args = parser.parse_args()

    rows = candidates(_load(args.entities))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"{len(rows)} candidate pairs -> {args.out}")


if __name__ == "__main__":
    main()
