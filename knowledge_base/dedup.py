"""Entity deduplication: a deterministic first stage, and a second stage that is a parameter.

Why this exists. `mechanism_graph` builds output A from `Claim.cause_ref`/`effect_ref`, so two spellings of one
concept are two nodes and the chain they should form breaks at the seam. Measured on one knowledge base:
899 entities, 853 distinct `entity_key`s, and an earlier run's integrity gate reporting output A as
"a list of causes, not a chain" while the same run's base held directed chains six edges long. The missing links
were duplicates: `MHC class II antigen-presentation pathway expression` (N-892) beside
`MHC class II antigen-presentation pathway` (N-893), in one run.

Two stages, because one of them cannot be trusted with the whole job:

1. **Deterministic** (this module). String rules only, each named, each explaining its own verdict. It merges
   spellings and refuses everything else, so a wrong merge can be traced to the rule that made it.
2. **Review** (`ReviewStage`, a parameter). Whatever decides the pairs stage 1 returns as `review`. The default
   is `identity_review`, which decides nothing: the pairs stay distinct and are reported as open. An LLM-backed
   stage lives in `knowledge_base/dedup_review.py` and is off unless asked for.

**What stage 1 will not do.** Containment never merges. Measured over the 853 keys, ordered containment would
have merged 1021 pairs, among them `antigen presenting cell` into `activated myeloid antigen presenting cell`
and `t cell` into `cd8+ t cell`. The shorter name is the more general entity, and collapsing a genus into a
species does not repair a mechanism, it fabricates one. Containment therefore proposes a review and nothing
more. The same holds for the Jaccard band: similarity alone never merges.

`entity_key` (`schemas.py`) is reused rather than reimplemented, with one correction. Its `parenthetical` rule
deletes `(...)` wholesale, which is right for `regulatory T cells (Treg)` and wrong for
`motor neuron damage (reduced via c6)`, where the parenthetical reverses the sign. So a parenthetical merges
only when it reads as an alias — short, and free of the modifier words listed in `MODIFIERS`.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field, replace
from typing import Callable, Collection, Iterable, Literal, Mapping, Sequence

from knowledge_base.schemas import Entity, entity_key, normalize

Decision = Literal["merge", "review", "distinct"]

#: Words that make a parenthetical a modifier rather than an alias: they change sign, scope, route or
#: delivery, and the entity they qualify is not the entity they are attached to.
MODIFIERS: frozenset[str] = frozenset(
    {
        "reduced",
        "increased",
        "impaired",
        "restored",
        "restoration",
        "loss",
        "lost",
        "gain",
        "preservation",
        "preserved",
        "suppression",
        "suppressed",
        "enhanced",
        "enhancement",
        "depletion",
        "depleted",
        "overexpression",
        "knockdown",
        "knockout",
        "deficient",
        "deficiency",
        "transfer",
        "infusion",
        "treatment",
        "therapy",
        "immunotherapy",
        "vaccination",
        "administration",
        "chronic",
        "acute",
        "passive",
        "active",
        "with",
        "without",
        "plus",
        "via",
        "non",
        "no",
        "not",
        "absence",
        "presence",
        "after",
        "before",
        "during",
    }
)

#: The band in which two names are similar enough to be worth a human or a second stage, and never similar
#: enough to merge on their own.
REVIEW_FLOOR = 0.8

_PARENTHETICAL = re.compile(r"\(([^)]*)\)")
_PUNCTUATION = re.compile(r"[^\w\s+-]")


@dataclass(frozen=True)
class Forms:
    """The normalisation ladder, kept so a verdict can show what it compared.

    One field per stage of the ladder, in order. `lexical` is `entity_key`, the versioned dictionary of safe
    equivalences (`ENTITY_RULES`), and `tokens` is what the set rules operate on.
    """

    raw: str
    unicode: str
    case: str
    punctuation: str
    lexical: str
    tokens: frozenset[str]

    @property
    def parentheticals(self) -> tuple[str, ...]:
        return tuple(match.group(1).strip() for match in _PARENTHETICAL.finditer(self.case))


def forms(name: str) -> Forms:
    """Build the ladder for one name. Linear in its length."""
    unicode_form = unicodedata.normalize("NFKC", name)
    case = unicode_form.casefold()
    punctuation = re.sub(r"\s+", " ", _PUNCTUATION.sub(" ", case)).strip()
    lexical = entity_key(name)
    return Forms(
        raw=name,
        unicode=unicode_form,
        case=case,
        punctuation=punctuation,
        lexical=lexical,
        tokens=frozenset(lexical.split()),
    )


@dataclass(frozen=True)
class Verdict:
    """What the stage decided about one pair, and on what grounds.

    `rule` names the rule that fired, `score` is its own measure (1.0 for an equality, the Jaccard index for
    the band, the token-length ratio for a containment) and `left`/`right` are the forms compared, so a
    surprising verdict is read rather than guessed at.

    `proposed_by` is the stage-1 rule that sent the pair to a second stage, kept because a second stage
    overwrites `rule` with its own answer and the band a pair came from is the only thing that says whether
    its verdict is worth anything. Empty for a stage-1 verdict, where `rule` already is it.
    """

    decision: Decision
    rule: str
    score: float
    left: str
    right: str
    stage: str = "deterministic"
    proposed_by: str = ""


def _alias_parenthetical(content: str) -> bool:
    """True when a `(...)` reads as another name for the same thing rather than a qualification of it."""
    words = content.split()
    if not words or len(words) > 2:
        return False
    return not any(word.strip("-+,.").casefold() in MODIFIERS for word in words)


def _parentheticals_agree(left: Forms, right: Forms) -> bool:
    """Whether dropping the parentheticals, as `entity_key` does, is safe for this pair."""
    extra = set(left.parentheticals) ^ set(right.parentheticals)
    return all(_alias_parenthetical(content) for content in extra)


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def _contained(short: str, long: str) -> bool:
    """Whether `short` appears in `long` as a whole run of tokens. Linear: `str.find` is."""
    return f" {short} " in f" {long} "


def compare(left_name: str, right_name: str) -> Verdict:
    """The deterministic stage over one pair. First rule to say `merge` wins; nothing after it runs.

    Order, with the only two rules that may merge first:

    1. `exact` — the same string.
    2. `normalized` — equal after NFKC, case folding and whitespace, i.e. `schemas.normalize`.
    3. `lexical` — equal `entity_key`. Merges when the parentheticals agree (`_alias_parenthetical`),
       otherwise proposes a review: the parenthetical may carry the sign.
    4. `token_set` — the same tokens in another order.
    5. `jaccard` — inside `REVIEW_FLOOR`..1: review.
    6. `containment` / `token_subset` — one name inside the other, ordered or not: review.
    7. `distinct`.
    """
    left, right = forms(left_name), forms(right_name)

    if left.raw == right.raw:
        return Verdict("merge", "exact", 1.0, left.raw, right.raw)
    if normalize(left.raw) == normalize(right.raw):
        return Verdict("merge", "normalized", 1.0, normalize(left.raw), normalize(right.raw))
    if left.lexical == right.lexical:
        if _parentheticals_agree(left, right):
            return Verdict("merge", "lexical", 1.0, left.lexical, right.lexical)
        return Verdict("review", "lexical_parenthetical", 1.0, left.case, right.case)
    if left.tokens == right.tokens:
        return Verdict("merge", "token_set", 1.0, left.lexical, right.lexical)

    score = _jaccard(left.tokens, right.tokens)
    short, long = sorted((left.lexical, right.lexical), key=lambda text: len(text.split()))
    if REVIEW_FLOOR < score < 1:
        return Verdict("review", "jaccard", score, short, long)
    if _contained(short, long):
        return Verdict("review", "containment", score, short, long)
    if left.tokens < right.tokens or right.tokens < left.tokens:
        return Verdict("review", "token_subset", score, short, long)
    return Verdict("distinct", "distinct", score, short, long)


# A second stage: it is given the pairs stage 1 returned as `review` and returns one verdict per pair, in
# order. It may say `merge`, `distinct` or `review` again; `review` means still open.
ReviewStage = Callable[[Sequence[tuple[str, str]]], Sequence[Verdict]]

#: The stage-1 rules whose pairs are worth a second stage by default. `jaccard` and `lexical_parenthetical`
#: are near-misses of an equality; `containment` and `token_subset` are mostly a general name inside a
#: specific one, and over the whole base they outnumber the near-misses by two orders of magnitude. Passing
#: them all to a paid stage spends most of the money on the pairs least likely to merge.
NEAR_MISS_RULES: frozenset[str] = frozenset({"jaccard", "lexical_parenthetical"})


def identity_review(pairs: Sequence[tuple[str, str]]) -> list[Verdict]:
    """The default second stage: decides nothing, so a review pair stays open and is reported as such."""
    return [Verdict("review", "identity", 0.0, left, right, stage="review") for left, right in pairs]


@dataclass(frozen=True)
class Merge:
    """One recorded correspondence: `source_id` is folded into `target_id`, by `rule`, at `stage`."""

    source_id: str
    target_id: str
    source_name: str
    target_name: str
    rule: str
    score: float
    stage: str


@dataclass
class Resolution:
    """The result of a pass over a set of entities.

    `assignment` maps every entity id to the id that represents it — itself, when nothing merged it. It is the
    map `mechanism_graph` and `build` would have to apply; this module does not apply it.
    """

    assignment: dict[str, str] = field(default_factory=dict)
    merges: list[Merge] = field(default_factory=list)
    open_reviews: list[tuple[str, str, Verdict]] = field(default_factory=list)

    @property
    def groups(self) -> dict[str, list[str]]:
        grouped: dict[str, list[str]] = {}
        for source, target in sorted(self.assignment.items()):
            grouped.setdefault(target, []).append(source)
        return {target: members for target, members in grouped.items() if len(members) > 1}


def _canonical_id(ids: Iterable[str]) -> str:
    """The surviving id of a group: the lowest record number, so the oldest entity keeps its identity."""

    def order(identifier: str) -> tuple[int, str]:
        digits = identifier.rsplit("-", 1)[-1]
        return (int(digits) if digits.isdigit() else 2**31, identifier)

    return min(ids, key=order)


def resolve(
    entities: Mapping[str, Entity],
    review: ReviewStage = identity_review,
    review_rules: Collection[str] | None = NEAR_MISS_RULES,
) -> Resolution:
    """Run both stages over every pair of entities and record the correspondences.

    Deterministic and order-independent: pairs are visited in sorted id order, and a group's survivor is its
    lowest record number rather than whichever entity was seen first.

    `review_rules` selects which of stage 1's review bands reach `review`; the rest stay open with their own
    verdict, and `None` sends them all. Every open review keeps `proposed_by`, the rule that proposed it, so
    the pile can be read by band rather than as one undifferentiated number.
    """
    ids = sorted(entities)
    names = {identifier: entities[identifier].canonical_name for identifier in ids}
    verdicts: dict[tuple[str, str], Verdict] = {}
    reviews: list[tuple[str, str]] = []

    selected = set(review_rules) if review_rules is not None else None
    for index, left in enumerate(ids):
        for right in ids[index + 1 :]:
            verdict = compare(names[left], names[right])
            verdicts[(left, right)] = replace(verdict, proposed_by=verdict.rule)
            if verdict.decision == "review" and (selected is None or verdict.rule in selected):
                reviews.append((left, right))

    for (left, right), verdict in zip(reviews, review([(names[a], names[b]) for a, b in reviews])):
        verdicts[(left, right)] = replace(verdict, proposed_by=verdicts[(left, right)].proposed_by)

    parent = {identifier: identifier for identifier in ids}

    def find(identifier: str) -> str:
        while parent[identifier] != identifier:
            parent[identifier] = parent[parent[identifier]]
            identifier = parent[identifier]
        return identifier

    merged: dict[tuple[str, str], Verdict] = {}
    for (left, right), verdict in verdicts.items():
        if verdict.decision != "merge":
            continue
        merged[(left, right)] = verdict
        a, b = find(left), find(right)
        if a != b:
            keep, drop = (a, b) if _canonical_id((a, b)) == a else (b, a)
            parent[drop] = keep

    resolution = Resolution(assignment={identifier: find(identifier) for identifier in ids})
    for (left, right), verdict in sorted(merged.items()):
        # The pair's own loser, folded into the survivor of the group the pair ended up in. Both are recorded:
        # the rule fired on this pair, while the id a claim must be rewritten to is the group's.
        source = right if _canonical_id((left, right)) == left else left
        target = find(source)
        resolution.merges.append(
            Merge(
                source_id=source,
                target_id=target,
                source_name=names[source],
                target_name=names[target],
                rule=verdict.rule,
                score=verdict.score,
                stage=verdict.stage,
            )
        )
    resolution.open_reviews = [
        (left, right, verdicts[(left, right)])
        for left, right in reviews
        if verdicts[(left, right)].decision == "review"
    ]
    return resolution
