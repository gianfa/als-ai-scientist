"""Typed configuration of the pipeline (`configs/pipeline.yaml`). Unknown keys are rejected."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PACKAGE_ROOT / "configs" / "pipeline.yaml"

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LauncherConfig(_Strict):
    """The `claude -p` session that launches one phase. It has no budget of its own: `pipeline/launcher.py` hands it
    what is left of `run_max_cost_usd`, because `--max-budget-usd` stops the workflow's agents and a smaller
    per-phase cap only kills a phase the run could still afford."""

    model: Text
    timeout_s: Annotated[int, Field(gt=0)]


class WebBudget(_Strict):
    """Web use allowed to each agent of a phase. Stated in the prompt (the agent stops searching and answers with
    what it has) and enforced per session by a hook. 0 removes the tool from the phase's session."""

    searches: Annotated[int, Field(ge=0, le=30)]
    fetches: Annotated[int, Field(ge=0, le=30)]


class AcquireStep(_Strict):
    """One step of an acquisition procedure: a command a user can paste as is, or something done by hand."""

    run: Text | None = None
    by_hand: Text | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> "AcquireStep":
        if (self.run is None) == (self.by_hand is None):
            raise ValueError("an acquisition step is either `run` (a command) or `by_hand`, not both or neither")
        return self


class AcquireConfig(_Strict):
    """How a user obtains a declared source; the `data` phase prints it when the source is missing."""

    see: Text  # the documented procedure, e.g. "docs/data-acquisition.md section 4"
    steps: list[AcquireStep] = Field(min_length=1)


class DataSourceConfig(_Strict):
    """One declared local input. `path` is relative to the repository root unless absolute; nothing is guessed."""

    id: Text
    path: Text
    kind: Text
    license_note: Text
    required: bool = True
    acquire: AcquireConfig | None = None


class DataPhase(_Strict):
    """The `data` phase runs no agent: it resolves and hashes the declared sources."""

    sources: list[DataSourceConfig] = Field(min_length=1)


class ComputePhase(_Strict):
    """The `compute` phase runs no agent either: the analyses are Python, their artifacts are digest-verified."""

    analyses: list[Text] = Field(min_length=1)
    seed: Annotated[int, Field(ge=0)] = 20260928


class LiteraturePhase(_Strict):
    model: Text | None = None
    web: WebBudget = WebBudget(searches=6, fetches=6)
    max_claims: Annotated[int, Field(gt=0, le=30)] = 8
    questions: list[Text] = Field(min_length=1)
    # Papers this phase's agents may bring into the store themselves (`fetch_paper`). Named
    # apart from the top-level `fetch_max_new`, which is the **host's** cap: the two must not read as
    # one. 0 does not register the tool at all, which is also the grant (`pipeline/launcher.py`).
    agent_fetch_max_new: Annotated[int, Field(ge=0)] = 0


class GeneratePhase(_Strict):
    model: Text | None = None
    context_limit: Annotated[int, Field(gt=0)] = 40
    perspectives: list[Text] = Field(min_length=1)


class ReviewPhase(_Strict):
    model: Text | None = None
    web: WebBudget = WebBudget(searches=4, fetches=4)
    reviewers: Annotated[int, Field(ge=2, le=5)] = 2  # independent reviewers per hypothesis
    hops: Annotated[int, Field(ge=1, le=2)] = 2
    # Papers this phase's agents may bring into the store themselves (`fetch_paper`). Named
    # apart from the top-level `fetch_max_new`, which is the **host's** cap: the two must not read as
    # one. 0 does not register the tool at all, which is also the grant (`pipeline/launcher.py`).
    agent_fetch_max_new: Annotated[int, Field(ge=0)] = 0


class VerifyPhase(_Strict):
    model: Text | None = None
    batch_size: Annotated[int, Field(ge=1, le=30)] = 10  # relations per verifier agent
    # Offer the verifier only the relations THIS run wrote, instead of every unverified one in the store.
    # Default and recommendation: `False` -- see the field's documentation in `configs/pipeline.yaml` for
    # what `True` gives up.
    only_on_run_kb: bool = False


class DesignPhase(_Strict):
    model: Text | None = None
    hops: Annotated[int, Field(ge=1, le=2)] = 2


class ArbitratePhase(_Strict):
    model: Text | None = None
    hops: Annotated[int, Field(ge=1, le=2)] = 2


class ExecutePhase(_Strict):
    model: Text | None = None
    # How many bought, eligible EXPs one run may execute. A bound on cost and wall time, not a scientific rule:
    # the arbiter decides what is worth testing, and an EXP left out by this cap says so in the phase report's
    # `skipped`. Four is what an earlier run bought, so today's archives fit under it.
    max_experiments: Annotated[int, Field(ge=1)] = 4


class ReassessPhase(_Strict):
    model: Text | None = None


class IntervenePhase(_Strict):
    model: Text | None = None
    hops: Annotated[int, Field(ge=1, le=2)] = 2
    drug_lookup_enabled: bool = False  # opt-in: may query MyChem.info live for a name never seen before
    drug_lookup_cache: Path = Path("data/external/mychem-cache/pharmacology.jsonl")


class NoveltyPhase(_Strict):
    """The after-the-run novelty stage (`python -m pipeline.cli novelty`). It is not one of `PHASES`: `./run.sh`
    launches it after `synthesize` unless given `--no-novelty` (the switch is on the command line, not here). Its search
    tool is granted by `agent_search_max`, as
    `agent_fetch_max_new` grants `fetch_paper`."""

    model: Text | None = None
    hops: Annotated[int, Field(ge=1, le=2)] = 1
    agent_search_max: Annotated[int, Field(ge=1)] = (
        8  # OpenAlex searches per hypothesis agent, granted as one total for the stage; cache hits free
    )
    # Full texts one hypothesis's agent may open, besides the abstracts every search returns. Granted as one total
    # for the stage (the server is shared) and spent per different paper; 0 leaves the stage on abstracts alone.
    # A paper is fetched into the store first, so a paywalled one cannot be read and is said to be unread.
    max_papers_read: Annotated[int, Field(ge=0)] = 3

    @property
    def agent_fetch_max_new(self) -> int:
        """`fetch_paper` is granted as far as the agent may read: a paper must be in the store to be opened."""
        return self.max_papers_read


class SynthesizePhase(_Strict):
    model: Text | None = None
    hops: Annotated[int, Field(ge=1, le=2)] = 1


class Phases(_Strict):
    data: DataPhase
    compute: ComputePhase
    literature: LiteraturePhase
    generate: GeneratePhase
    review: ReviewPhase
    verify: VerifyPhase
    design: DesignPhase
    arbitrate: ArbitratePhase
    execute: ExecutePhase = Field(default_factory=ExecutePhase)
    reassess: ReassessPhase = Field(default_factory=ReassessPhase)
    intervene: IntervenePhase
    synthesize: SynthesizePhase
    novelty: NoveltyPhase = Field(default_factory=NoveltyPhase)


class PipelineConfig(_Strict):
    version: str
    objective: Text
    launcher: LauncherConfig
    # The only spend cap: enforced twice, by `claude --max-budget-usd` per session (the CLI's own figures, minus
    # what the run already cost) and by `pipeline/cost_watch.py` (an estimate, across resumes).
    run_max_cost_usd: Annotated[float, Field(gt=0)]
    literature_store: Path
    fetch_max_new: Annotated[int, Field(ge=0)]
    phases: Phases
    config_hash: str = ""

    def web_budget(self, phase: str) -> WebBudget:
        """Phases without a `web` setting (generate, verify) get no web tools at all."""
        return getattr(getattr(self.phases, phase), "web", WebBudget(searches=0, fetches=0))

    def agent_fetch_budget(self, phase: str) -> int:
        """How many papers this phase's agents may fetch; 0 for every phase without the setting.

        Same shape as `web_budget`: a phase that does not declare it gets none, so adding the tool to a phase
        is a config change and not a code change.
        """
        return int(getattr(getattr(self.phases, phase), "agent_fetch_max_new", 0))

    def agent_search_budget(self, phase: str) -> int:
        """How many OpenAlex searches this phase's agents may make; 0 for every phase without the setting."""
        return int(getattr(getattr(self.phases, phase), "agent_search_max", 0))

    def agent_read_budget(self, phase: str) -> int:
        """How many different full texts this phase's agents may open; 0 for every phase without the setting."""
        return int(getattr(getattr(self.phases, phase), "max_papers_read", 0))

    def store_path(self) -> Path:
        return self.literature_store if self.literature_store.is_absolute() else PACKAGE_ROOT / self.literature_store


def load_config(path: Path = DEFAULT_CONFIG) -> PipelineConfig:
    raw = path.read_bytes()
    data = yaml.safe_load(raw)
    if not isinstance(data, dict) or data.get("version") != "1":
        raise ValueError(f"{path}: expected a pipeline config of version 1")
    return PipelineConfig(**data, config_hash=hashlib.sha256(raw).hexdigest())
