# literature/

The papers the system reads.
How the tools work: [docs/tools/literature.md](../docs/tools/literature.md).

| Path | Tracked in git | What it holds |
|---|---|---|
| `README.md` | yes | this page |
| `reading-lists/` | yes | JSON lists of papers to fetch (DOI, PMID or PMCID), the inputs of `scripts/data/literature_fetch.py`; they make a fetch reproducible: `michaelis-2025.json` (the primary paper) and `als-c9orf72-candidates.json` (28 open-access papers cited by earlier runs, for testing the quote-check tool; relevance not assessed) |
| `sources.csv` | yes | your list of the files in `inbox/`: `file,identifier,title,year,alias,note` (metadata only, no texts) |
| `inbox/` | **no** (git-ignored, except `.gitkeep`) | papers you drop in by hand (`.pdf`, `.txt`, JATS `.xml`), e.g. paywalled ones |
| `store/` | **no** (git-ignored) | the extracted texts and their index (`index.jsonl`, `INDEX.md`), written by the tools |

**Papers and their texts are never committed**: they are copyrighted and their licences differ (the store records the
licence Europe PMC reports, which is recorded, not verified). Do not remove the ignore rules in `.gitignore`.

## Fetch open-access papers

From the repository root:

```sh
uv run python scripts/data/literature_fetch.py --reading-list literature/reading-lists/michaelis-2025.json --max-new 5
```

The default store is `literature/store/`. A reading list is a JSON list of identifiers, or an object with a
`reading_list` list whose entries are identifiers or `{"identifier": ..., "title": ...}`. At most `--max-new` papers are
downloaded per run (default 5); every entry ends in one reported outcome. Only papers with an open-access full text on
Europe PMC can be fetched.

## Add your own papers

Put the file in `inbox/`, add a row to `sources.csv` saying which paper it is (only `file` is required; `identifier` is a
DOI, PMID or PMCID), and run, from the repository root:

```sh
uv run python scripts/data/literature_ingest.py
```

Without a row, the tool looks for a DOI in the text and accepts it only if Europe PMC resolves it and the title matches;
otherwise the file stays pending and is reported with the reason. Your files are never moved or deleted. Details:
[docs/tools/literature.md](../docs/tools/literature.md).

