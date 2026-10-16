# Paper usage policy

1. Use the Michaelis 2025 Paper2Agent skill as the primary structured source.
2. Use Paper2Agent skills for highly relevant secondary literature.
3. Prefer primary-paper evidence over generic summaries.
4. If no Paper2Agent representation exists, fall back to normal literature retrieval.
5. Record which paper-agent/skill produced each evidence item.

## Paper2Agent usage

Use `/paper2agent <paper>` when at least one condition holds:

- the paper is central to the current hypothesis or decision;
- the workflow needs figures, tables, supplementary material, datasets, or executable methods;
- the paper must be reproduced or independently verified;
- multiple downstream agents are expected to reuse the same paper;
- precise provenance is important enough to justify preprocessing.

Do not invoke Paper2Agent for routine background papers when normal literature retrieval is sufficient.

If a Paper2Agent representation already exists, reuse it instead of regenerating it.

When Paper2Agent is invoked:

- store the source paper in a deterministic project path;
- record URL/DOI;
- store supplementary material when available;
- generate the Paper2Agent artifact from the stored source;
- record the generated artifact path.

## Directories

Where the Michaelis 2025 material lives (all git-ignored; see `docs/data-acquisition.md`):

```text
literature/
└── inbox/
    └── michaelis-2025.pdf            # the article, saved by hand
data/external/
├── michaelis-2025-zenodo/            # the four ICS records
└── michaelis-2025-nature-supp/       # the published supplementary tables
dist/
└── michaelis-2025-agent/
    └── skill/michaelis-2025-paper/   # the Paper2Agent extraction (tables, paper.md)
```
