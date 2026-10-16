# Drug lookup (MyChem.info)

## What it is

A small check that asks a public drug database whether a compound name proposed by the system is a real, identifiable
compound. The database is [MyChem.info](https://mychem.info), which gathers into one place the records of DrugBank,
ChEMBL, ChEBI, PubChem and UNII (the main public catalogues of drugs and chemicals).

## What it is for

The `intervene` agents propose candidate interventions from what they know and from the knowledge base. Nothing
else checks that a named molecule exists. The lookup adds that one check. It does **not** say the drug works, is
safe, or is a good idea: only that the name matches a catalogued compound, and under which identifiers.

## Input and output

| | |
|---|---|
| **In** | the `drug_name` an `intervene` agent put on a candidate (optional; the agent fills it only when sure it is a real compound) |
| **Out** | one of three outcomes per name, each stored in the knowledge base as a claim |

| Outcome | What the claim says |
|---|---|
| found | exactly one record matches the name; lists its identifiers (DrugBank, ChEMBL, ChEBI, PubChem, UNII) |
| no record | the database returned nothing; the name is not confirmed, which is not proof that the compound does not exist |
| ambiguous | several records came back and none matches the name uniquely; identity left unresolved |

The check never guesses. A synonym the database files under another name (for example *rapamycin*, filed as
*sirolimus*) reads as "ambiguous" or "no record", not as a silent best guess. Missing a real drug is accepted;
attaching the wrong identity is not.

## Who calls what

- **The orchestrator** makes the call, as plain code after `intervene`. No agent calls MyChem.info and there is no MCP
  tool for it: the agent only supplies the name.
- **One request to MyChem.info** reads five catalogues (DrugBank, ChEMBL, ChEBI, PubChem, UNII) through its copy of them;
  none of them is contacted directly. Nine fields are asked for: DrugBank id and name, ChEMBL id and preferred name, ChEBI id and name,
  PubChem CID, UNII code and display name.

## Value

A reader of the final proposal can see, for each named compound, a claim written by code (not by an agent) saying
whether it was found and under which catalogue identifiers. A candidate with a name that resolves to nothing is
visibly weaker than one that resolves.

## Where it runs and how to switch it on

It runs inside the pipeline, right after the `intervene` phase has stored its candidates, as plain code in the
orchestrator (not as a tool the agents can call). It is **off by default**, because it may contact the internet for a
name it has never seen:

```yaml
# configs/pipeline.yaml
phases:
  intervene:
    drug_lookup_enabled: true
```

Every answer is saved in a local file (`data/external/mychem-cache/pharmacology.jsonl`, not committed), so the same
name is never asked twice. If the service cannot be reached, nothing is stored for that name and the failure is
written in the phase report, so a missing answer is never mistaken for "no record".

## How to see the results

- **In the knowledge base:** claims written by the agent id `drug-lookup`, each tied to the candidate that named
  the compound by a `depends_on` relation.
- **In the run archive:** the `drug_lookup` entry of `experiments/<RUN_ID>/results/NN-intervene-report.json` lists
  each name, its outcome, the claim the lookup stored and the candidates that used it, or the error if the lookup
  failed. The claim ID is the one the store kept, so it can be read back; a name the store already held from an
  earlier run is reported under that earlier ID rather than the one this lookup proposed.
- **By hand, without a run:** `scripts/ops/pharmacology-smoke.py metformin imatinib rapamycin` queries the service
  and prints the outcome and identifiers (it needs the internet).

## Testing the whole step without a full run

A full run is paid and long, and the step is off by default, so neither the agent filling `drug_name` nor a real
query has ever happened inside a run. `pipeline/intervene_drug_lookup_probe.py` runs the `intervene` phase alone,
with the lookup on and its own empty cache, over a fixture knowledge base:

```bash
# PAID: one intervene session, plus one live MyChem.info query per compound the agent names
UV_PROJECT_ENVIRONMENT=.venv uv run --frozen python -m pipeline.intervene_drug_lookup_probe \
    tests/fixtures/intervene/cases/surviving_hypothesis.json
```

It prints the names the agent gave, the outcome per name, the claims stored and the session cost, and exits
non-zero when no name was looked up — including when the agent honestly named no compound, which the prompt
allows. `tests/test_intervene_drug_lookup_probe.py` runs the same probe with a mocked agent on every suite run,
and the live half only under `ALS_LIVE_INTERVENE_DRUG_LOOKUP=1`.

## Limits

- The service does not report a version, so a later query of the same name may differ from a cached one.
- The terms of the underlying catalogues (notably DrugBank) have not yet been read by a person: see `LICENSES.md`
  before a final proposal cites a specific identifier.
- No production run has used the lookup yet. It has been exercised end to end only by the probe above, on a
  fixture knowledge base; [the pipeline page](../pipeline.md) says the same.
