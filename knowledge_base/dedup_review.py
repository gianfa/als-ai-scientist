"""The second deduplication stage, backed by a Claude session. Off unless asked for.

It is a `dedup.ReviewStage`: it receives the pairs the deterministic stage could not decide and returns one
verdict each. The call is **paid**, so nothing imports it by default and no test runs it without
`ALS_DEDUP_REVIEW=claude` (`tests/test_entity_dedup.py`).

The session is deliberately minimal — no MCP server, no tools, one prompt, a budget cap — and it is invoked the
way every other paid session in this repository is, `claude -p` through `subprocess` (`pipeline/launcher.py`).
There is no Anthropic SDK in `pyproject.toml` and this does not add one.

What the model is asked is narrow on purpose: *do these two names denote the same node of a causal graph*. It
is told that a wrong merge fabricates a mechanism, that polarity, specificity and delivery distinguish nodes,
and that `unclear` is an available answer. It is not asked to score similarity, which is what the
deterministic stage already measured and what no amount of similarity can settle.
"""

from __future__ import annotations

import csv
import io
import json
import os
import subprocess
from dataclasses import dataclass, field
from typing import Sequence

from knowledge_base.dedup import Decision, Verdict


@dataclass(frozen=True)
class Spend:
    """What one session cost, as the CLI reported it."""

    cost_usd: float
    duration_ms: int
    session_id: str
    model: str


PROMPT = """You are deduplicating entity names from the knowledge base of a scientific discovery pipeline on \
ALS and C9orf72. Each entity is a node of a causal graph: claims point at them as cause and effect.

For each row of the CSV below, decide whether `a_name` and `b_name` denote THE SAME node.

Rules:
- A wrong merge fabricates a mechanism that nobody asserted. When in doubt, do not merge.
- These are different nodes: a thing and a change in it (`IL-10` vs `IL-10 suppression`); a thing and an \
intervention delivering it (`IL-10` vs `IL-10 (AAV spinal cord overexpression)`); a population and a subset of \
it (`CD4+ T cells` vs `C9orf72-specific CD4+ T cells`); a state and its reversal (`motor neuron damage` vs \
`motor neuron damage (reduced via c6)`).
- These are the same node: spelling, plural, hyphen and underscore variants; a parenthetical abbreviation or \
sequence (`myelin basic protein (MBP)`); the same words in another order.
- Answer `unclear` when the decision needs a domain expert, not more string reasoning.

Reply with ONLY a JSON array, one object per row, in the same order:
[{"pair_id": "...", "decision": "merge" | "distinct" | "unclear", "reason": "<= 12 words"}]

CSV:
"""


@dataclass(frozen=True)
class ClaudeReview:
    """A `ReviewStage` that asks one Claude session about every pair in one call.

    `model` and `max_cost_usd` are explicit because the call is paid; `timeout_s` bounds it the way the
    launcher bounds a phase. `pairs_per_call` keeps a prompt from growing past what a session answers
    reliably — the pairs are independent, so splitting them changes nothing but the prompt size.
    """

    model: str = "claude-sonnet-5-5"
    max_cost_usd: float = 0.50
    timeout_s: int = 300
    pairs_per_call: int = 60
    #: What the sessions of the last call actually cost, in USD, as the CLI reports it per session. A paid
    #: stage that does not say what it spent cannot be budgeted, and `--max-budget-usd` is a cap, not a bill.
    spend: list[Spend] = field(default_factory=list)

    def __call__(self, pairs: Sequence[tuple[str, str]]) -> list[Verdict]:
        self.spend.clear()
        verdicts: list[Verdict] = []
        for start in range(0, len(pairs), self.pairs_per_call):
            verdicts.extend(self._batch(pairs[start : start + self.pairs_per_call], start))
        return verdicts

    @property
    def total_cost_usd(self) -> float:
        return sum(session.cost_usd for session in self.spend)

    def _batch(self, pairs: Sequence[tuple[str, str]], offset: int) -> list[Verdict]:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(("pair_id", "a_name", "b_name"))
        for index, (left, right) in enumerate(pairs):
            writer.writerow((f"R-{offset + index:03d}", left, right))

        answer = self._ask(PROMPT + buffer.getvalue())
        decided = {item.get("pair_id"): item for item in answer if isinstance(item, dict)}
        out: list[Verdict] = []
        for index, (left, right) in enumerate(pairs):
            item = decided.get(f"R-{offset + index:03d}", {})
            decision = item.get("decision")
            # An answer that is not one of the three is not a verdict: the pair stays open rather than
            # being merged on a malformed reply.
            resolved: Decision = decision if decision in ("merge", "distinct") else "review"
            out.append(
                Verdict(resolved, f"claude:{item.get('reason', 'no answer')}"[:120], 0.0, left, right, stage="claude")
            )
        return out

    def _ask(self, prompt: str) -> list[dict]:
        """One session. Its reported cost and duration are recorded in `spend` before the reply is parsed,
        so a call that spent money and then returned something unusable still says what it spent."""
        command = [
            "timeout",
            "--kill-after=5s",
            str(self.timeout_s),
            "claude",
            "-p",
            prompt,
            "--model",
            self.model,
            "--output-format",
            "json",
            "--max-budget-usd",
            f"{self.max_cost_usd:.2f}",
            "--disallowedTools",
            "Bash Edit Write Read Glob Grep WebSearch WebFetch",
        ]
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            env={**os.environ, "UV_PROJECT_ENVIRONMENT": ".venv", "VIRTUAL_ENV": ""},
        )
        if result.returncode != 0:
            raise RuntimeError(f"claude -p failed ({result.returncode}): {result.stderr.strip()[:400]}")
        payload = json.loads(result.stdout)
        self.spend.append(
            Spend(
                cost_usd=float(payload.get("total_cost_usd") or 0.0),
                duration_ms=int(payload.get("duration_ms") or 0),
                session_id=str(payload.get("session_id") or ""),
                model=self.model,
            )
        )
        return _parse(payload.get("result", ""))


def _parse(text: str) -> list[dict]:
    """The JSON array out of a reply that may be wrapped in prose or a fenced block."""
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end < start:
        raise ValueError(f"no JSON array in the reply: {text[:200]!r}")
    parsed = json.loads(text[start : end + 1])
    if not isinstance(parsed, list):
        raise ValueError("the reply's JSON is not an array")
    return parsed
