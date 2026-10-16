# Reading a mechanism out of the knowledge base

The knowledge base stores causal claims, not a mechanism. Each causal claim is one edge — a cause entity, an
effect entity (whose names carry the direction), a context and a mechanism text — and a set of edges is not a pathway. The project
statement asks for "a causal model of the C9orf72–immune–neurodegeneration **pathway**" (p. 5), and
`pipeline/section_gates.py::check_mechanism` enforces the difference: since ADR 0033 it requires that the cited proper
edges join and allows at most one unchecked edge beside them (before it, that some cited edge's effect be another cited
edge's cause, with one verified step). One production run failed exactly there — it cited eight causal claims that
shared no node — while its own knowledge base held a six-edge chain it did not cite.

These functions, all in `knowledge_base/status.py` and all pure, are the views that close that gap. Each
one is a *reading* of asserted claims. **None of them is a causal model identified from data**: there is no
confounder adjustment, no structure learning and no effect estimation anywhere in this repository.

| Function | Returns | Shape |
|---|---|---|
| `mechanism_graph` | every current causal claim | a set of edges |
| `causal_edges` | the same, filtered by the standing of each edge | a set of edges |
| `mechanism_path` | the longest directed chain | an ordered list of edges |
| `mechanism_subgraph` | that chain plus its one-hop neighbourhood | a backbone and its neighbours |

## `causal_edges` — the two axes a filter can use

```python
causal_edges(records, *, include_superseded=False, provenance=None, epistemic=None, aliases=None)
```

`mechanism_graph` already carries `provenance_status` and `epistemic_status` on every edge but selects on
neither, so every caller that wanted "the verified part of the mechanism" filtered the list by hand. The two
are different questions and both are parameters here:

- **`provenance`** — whether the claim's quote was found in its stored source: `verified`, `partial`,
  `unverified`. This is the axis that says how the claim was obtained.
- **`epistemic`** — what *other* claims say about it, computed from verified relations (see this module's
  header): `supported`, `contradicted`, `contested`, `unresolved`. This is the axis that says how the claim
  stands in the base.

They do not substitute for one another. A verified quote nobody has contradicted is `verified` and
`unresolved`; an agent's guess two verified claims support is `unverified` and `supported`.

`None` on either axis means no filter. An **empty collection means nothing passes**, and that is honoured
rather than widened to "everything": an empty selection is a selection, and silently inverting it would be the
kind of helpfulness that invalidates a measurement.

`aliases` maps an entity id to the id standing for its group. It is how entity deduplication
(`knowledge_base/dedup.py::resolve`) reaches the graph **without rewriting the append-only store**: the edges
are relabelled on the way out and the records keep their own ids, so a merge is a recorded correspondence, not
an overwrite. An edge whose two ends merge into one node is **dropped**, because it would assert that a thing
causes itself and a self-loop would satisfy "the edges join" on its own.

## `mechanism_path` — the backbone

```python
mechanism_path(records, *, provenance=None, epistemic=None, aliases=None,
               require_verified=True, min_edges=2, include_superseded=False)
```

The longest directed chain through the filtered graph, as an ordered edge list, or `[]` if there is none. A
caller that cites this cites a path by construction, which is what the gate asks for.

- **`require_verified`** (default `True`) is a condition, not a preference: only a chain holding at least one
  `verified` edge is a candidate, and the longest of those wins. The gate requires both the join and one
  verified step, so returning a longer chain every step of which is an unchecked agent assertion would hand a
  caller a path the gate then rejects. Set it to `False` to ask for the longest chain outright.
- **`min_edges`** (default 2) is the floor below which there is no chain at all; 2 is the smallest join.
- Paths are **simple** — no entity is visited twice — so a cycle among asserted claims cannot produce an
  unbounded walk. The longest chain through a cycle of *n* nodes therefore has *n*−1 edges, not *n*.
- Ties go to the chain whose edges sort first by claim id, so two calls on one base agree.

## `mechanism_subgraph` — the backbone and what hangs off it

```python
mechanism_subgraph(records, *, ...,                      # every mechanism_path parameter, for the backbone
                   neighbour_provenance=HYPOTHESISED,    # ("unverified", "partial")
                   neighbour_epistemic=None)
```

Returns a `MechanismSubgraph` with `backbone` (ordered: it is the path) and `neighbourhood` (the adjacent
edges that passed the neighbour filter, in claim-id order). The two lists never share an edge, so a reader
counting edges does not count a backbone step twice; `edges` concatenates them and `node_ids` gives the spine
in path order followed by the neighbours' outer ends.

**Why the neighbourhood exists.** The path alone reads as a claim about *the* mechanism, which is more than
the base supports. Around every step sit adjacent causal claims nobody has checked, and those are what make
the chain one reading among several rather than a conclusion. The neighbourhood is therefore drawn with its
own, deliberately **looser** filters: a subgraph whose two parts were held to the same standard would show
nothing the path does not already show.

**What "hypothesis or supposition" means here.** The default `HYPOTHESISED = ("unverified", "partial")`
applies to `neighbour_provenance`. The knowledge base has no epistemic status meaning "hypothesised", because
status is computed from what other claims say about a claim, never from how confidently it was stated. An
agent's unchecked causal assertion is `provenance_status == "unverified"`. `neighbour_epistemic` is `None` by
default, so a *contested* neighbour is shown rather than hidden — contested neighbours are the most
informative ones.

An edge between two backbone nodes that the path did **not** take is a neighbour, not a backbone step: it is
a competing adjacency, and hiding it would conceal the alternative reading it represents.

## `proper_mechanism` — the view the gate and the tool share (ADR 0033)

```python
proper_mechanism(records, *, aliases=None, min_edges=1, include_superseded=False)
```

The longest connected chain of **proper** edges (`is_proper`: shown by the view, and either its quote verified against its
source or supported by an active verified relation), as `backbone`; and, as `neighbourhood`, the **improper** edges
(`is_improper`: shown, not proper) that touch a backbone node. A contradicted or contested edge is in neither list: the
records keep it and the `contradicts` relations that make it so, but the view does not read it. `min_edges` defaults to
1; the longest chain wins, so a chain of two or more edges is returned whenever one exists. `check_mechanism` accepts a
section that cites proper edges which join, plus at most one improper edge adjacent to them.

It is built on `mechanism_subgraph` with two new callables, `edge_filter` (backbone) and `neighbour_filter`
(neighbourhood), which `causal_edges` and `mechanism_path` also take: "verified *or* supported" cannot be written as the
AND of a `provenance` set and an `epistemic` set.

### What an agent sees

Both views are served to every phase session by the read-only KB server, which is how they reach a run:

| Tool | Answers with |
|---|---|
| `mcp__kb__kb_mechanism_graph(entity, limit)` | a set of causal edges, possibly disconnected |
| `mcp__kb__kb_mechanism_path(limit)` | `backbone` (ordered, connected, proper edges only) and `neighbourhood` (unchecked edges touching it); since ADR 0033 |

The `synthesize` prompt names the path tool in the rule for section A, and the shared tool block
(`workflows/phases/src/lib/phase.js::kbBlock`) states the difference to every phase: one answers with a set of
edges, the other with a chain, and a set of causal claims that do not join is a list of causes rather than a
pathway. Section A had **no** stated citation rule at all before 2026-10-08 — the prompt listed the checks on
B, D, E and G and said nothing about A, while `check_mechanism` was checking it. That is the other half of why
that run cited eight disjoint edges: the knowledge base held a chain, the agent had a tool that could
find one, and nothing asked it to.

### This is the view to hand a reviewer

A reviewer's first two questions are *is there anything here worth investigating* and *which of these claims
is actually founded*. The subgraph answers both on one picture, where the path alone answers neither: the
backbone carries each step's provenance, and its neighbours show what competes with it. Measured over
`knowledgebase/` on 2026-10-08, with the dedup assignment passed as `aliases`:

| Selection | backbone | neighbourhood | nodes |
|---|---|---|---|
| that run only | 6 | 11 | 14 |
| every run | 10 | 43 | 44 |

That run's backbone runs from `allele-selective C9orf72 repeat-targeting antisense oligonucleotide` to
`ALS disease progression rate` through `polyGR dipeptide-repeat protein toxicity`, `motor neuron damage`,
`C9orf72 epitope presentation`, `C9orf72 epitope T cell reactivity` and the IL-10-mediated T cell response,
with one verified step (C-1147) — enough for both of the gate's requirements. Its neighbourhood is the part
a reviewer should read first: seven unverified claims converge on `ALS disease progression rate` alone, two of
which (C-1198, C-1202) assert the same edge `IL-5 → ALS disease progression rate` twice. The backbone cites
one of them. That is a duplicate claim and a crowded effect node, and neither is visible in the path.

## The longest path is one selection rule, not the only one

`mechanism_path` maximises **length**, which is a proxy for coverage and nothing more. It is the rule
implemented first because the gate's failure was a connectivity failure, but the backbone a reader should see
depends on the question, and several other rules are at least as defensible on this base. None of the
following is implemented; they are recorded here so the choice stays visible rather than becoming an
assumption.

- **Endpoint-anchored path.** The statement names the pathway's ends — C9orf72 and neurodegeneration — so the
  shortest, or every, directed path between two *named* entities is closer to what was asked than whatever
  chain happens to be longest. This is the most likely next addition.
- **Most-verified path**, or the path maximising the fraction of verified steps rather than the count of
  steps. A three-edge chain with three verified steps supports more than a ten-edge chain with one.
- **Minimum-assumption path**: fewest `unverified` steps, which is the same objective read as a cost.
- **Support-weighted path**, scoring edges by `epistemic_status` (`supported` > `unresolved` > `contested`),
  which is the only one of these rules that uses the relation graph at all.
- **No path at all**: the whole weakly connected component containing the verified edges, which makes no
  selection and so cannot mislead by selecting — at the cost of being unreadable past about forty nodes
  (`tools/graph/`'s circular layout, see [pipeline.md](pipeline.md#drawing-the-graph)).
- **Where to spend an experiment**: the nodes most chains pass through, or the single unverified edge whose
  verification would connect the most otherwise-disjoint components. This answers a different question from
  all of the above — not *what is the mechanism* but *what should be measured next* — and it is the one a
  falsification phase would want.

Adding a rule means adding a parameter or a sibling function to `mechanism_path`, not changing it: which
chain to show is a modelling choice, and the pattern this repository already uses for such a choice is a
versioned selection over claim ids that owns no content (`pipeline/prior_graph.py`, and
[pipeline.md](pipeline.md#two-causal-graphs-and-why-they-are-not-the-same-object)).

## Limits

- Longest simple path is NP-hard in general. The implementation is an exhaustive depth-first walk, which is
  adequate only because these graphs are small (338 causal edges over the whole base, 39 for one run) and
  would need replacing before it met a dense one.
- Tie-breaking by claim id is reproducible but arbitrary: where two chains are equally long, the one shown is
  not thereby the better one.
- The views inherit whatever the entity identity gets wrong. Without `aliases` a duplicated node breaks a
  chain; with them, a wrong merge invents one. Containment and similarity never merge on
  their own, and stage 2 of deduplication is not reproducible and so proposes rather than decides.
- `epistemic_status` is defined for claims only. A hypothesis is a composite of claims and has no aggregated
  status, so no view here can be filtered by the standing of a hypothesis.
- A neighbour is *adjacent* to the backbone, which is a statement about the graph and not about relevance.
