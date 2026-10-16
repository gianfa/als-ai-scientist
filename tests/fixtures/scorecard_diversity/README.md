# Real-archive fixture for `hypothesis_diversity` (EVAL-AI4)

Not synthetic: this is the exact `pl-prod-005` subset of the shared knowledge base that produced
`diversity: 0.9583` when read live on 2026-10-09 — 3 hypotheses, their causal claims (linked by `part_of`),
and any `supersedes` relation touching them. Frozen as a regression fixture, independent of the shared
`knowledgebase/` (which keeps growing and is not something a test should read live).

Extracted with:

```python
from knowledge_base.store import read_records
from knowledge_base.schemas import Hypothesis, Claim, Relation

records = read_records(Path("knowledgebase"))
hyps = [r for r in records.values() if isinstance(r, Hypothesis) and r.run_id == "pl-prod-005"]
hyp_ids = {h.id for h in hyps}
part_of = [r for r in records.values() if isinstance(r, Relation) and r.type == "part_of"
           and r.target_id in hyp_ids]
claim_ids = {r.source_id for r in part_of}
claims = [records[cid] for cid in claim_ids if cid in records and isinstance(records[cid], Claim)]
```

`tests/test_evaluation_scorecard.py::test_a_real_archived_run_reproduces_its_known_diversity_value` reads this
fixture with `knowledge_base.store.read_records` and asserts the known value — a regression check against real
pipeline output, alongside the synthetic `kb_factories`-built cases that cover the function's individual rules.
