# Data acquisition (user-provided inputs)

The repository does not ship third-party data. The user must obtain them from the original sources
(see `LICENSES.md`); the system must fail explicitly if a required input is missing.

## 1. Michaelis et al. article (assisted, the PDF by hand)

```sh
scripts/data/paper-download.py --list-only     # what Crossref and Europe PMC state: licence, version, locations
scripts/data/paper-download.py                 # fetch the open-access full text (JATS XML) to literature/inbox/
scripts/data/paper-download.py --format pdf    # print the PDF URL to open in a browser
```

The script starts from the DOI, not from a URL: it reads the licence and the open-access locations from
Crossref and Europe PMC, prints them, and fetches only what one of them calls open — for
<https://doi.org/10.1038/s41586-025-09588-6> that is CC BY-NC-ND 4.0, stated by both, with Europe PMC
`PMC12657233` marked `isOpenAccess: Y`. If a DOI has no open licence stated, it refuses and says so.

**The PDF stays a manual step.** Both open-access PDF locations refuse a programmatic client: Europe PMC's
render endpoint answers 403, nature.com answers a consent page, PMC's PDF path redirects. Presenting a script
as a browser works around an access control, which an open licence does not authorise, so `--format pdf`
prints the URL for you to open, and the path to save it as: `literature/inbox/michaelis-2025.pdf`, which `.gitignore`
keeps out of the repository. That is the one place the rest of this document assumes. The folder is also where
`tools/literature/ingest.py` reads papers you add to the *literature store*: a file saved here is registered there
only when you run that script (see [Literature tools](tools/literature.md#adding-your-own-papers-ingestpy-literatureinbox-literaturesourcescsv)).

The full text the script does fetch is the JATS XML Europe PMC publishes for programmatic use. It is not what
the Paper2Agent step reads; it is a citable, licence-checked local copy of the text.

### 1.1 Extracting it with Paper2Agent

The analyses do not read the PDF. They read two CSV tables and a Markdown rendering produced by
[Paper2Agent](https://github.com/jmiao24/Paper2Agent), which the devcontainer installs as a Claude Code
skill (`~/.claude/skills/paper2agent`). Run it from the repository root, in Claude Code:

```text
/paper2agent Convert the paper PDF literature/inbox/michaelis-2025.pdf. Use
  --name michaelis-2025-paper,
  --work dist/paper-review/michaelis-2025,
  and build the final package with --output dist/michaelis-2025-agent/skill/michaelis-2025-paper.
  Keep the default table asset names (table-pPPPP-NNN); do not rename them.
```

**Two parts of that are requirements, not preferences**, because the configuration and the code address the
result by path:

- `--output dist/michaelis-2025-agent/skill/michaelis-2025-paper`. It is the `path` of the two table sources in every `configs/pipeline*.yaml`,
  the default of `analysis/cli.py --source-root`, and where `pipeline/launcher.py` looks for
  `references/paper.md`.
- **The default table asset names.** The two sources name `assets/table/table-p0006-001.csv` (Table 1,
  epitopes) and `assets/table/table-p0006-007.csv` (Table 2, HLA). Paper2Agent writes
  `table-p<page>-<nnn>.csv` by default; renamed or renumbered, the `data` phase stops with the file missing,
  and a re-extraction that moves a table to another page changes the names. If that happens, fix the `path`
  values in the configuration rather than renaming the files, so the manifest records what was actually read.

Then verify, which checks both the skill and the extraction:

```sh
scripts/setup/check-paper2agent.sh
```

The extraction is a derivative of a CC BY-NC-ND 4.0 article: keep it local (`dist/` is git-ignored) and do
not redistribute it.

## 2. Michaelis Zenodo FCS / FlowJo data (scripted)

Raw cytometry files cited in the article's Data availability section. All records are CC BY 4.0
(license read from the Zenodo API on 2026-09-29); cite the DOIs when using them.

| Record | Content | Size |
|---|---|---|
| [10.5281/zenodo.16749283](https://doi.org/10.5281/zenodo.16749283) | Spectral FCS (Cytek Aurora), 44 files | 1641 MB |
| [10.5281/zenodo.16749334](https://doi.org/10.5281/zenodo.16749334) | ICS Screen 1, 26 files | 152 MB |
| [10.5281/zenodo.16754553](https://doi.org/10.5281/zenodo.16754553) | ICS Screen 2, 19 files | 45 MB |
| [10.5281/zenodo.16754591](https://doi.org/10.5281/zenodo.16754591) | ICS Screen 3, 26 files | 58 MB |
| [10.5281/zenodo.16754609](https://doi.org/10.5281/zenodo.16754609) | ICS Screen 4, 26 files | 139 MB |

```sh
scripts/data/michaelis-data-download.sh --list-only          # references + file sizes, no download
scripts/data/michaelis-data-download.sh                      # 4 ICS records (~393 MB) -> data/external/michaelis-2025-zenodo/
scripts/data/michaelis-data-download.sh --dest /data/m25 --records 16749283   # spectral only, custom folder
```

The script downloads one file at a time on a single connection, verifies the md5 published by Zenodo,
skips valid files on re-run, and refuses to exceed `--max-bytes` (default 2 GB). Keep the destination
out of Git (`data/external/` is git-ignored). These records contain no donor HLA typing or source data (see section 3).

## 3. Supplementary files and source data (scripted with curl)

Supplementary Information (includes Supplementary Table S2, HLA typing), a second supplementary PDF and the
figure source data are hosted by Nature, not Zenodo. They are treated as local-only material of the
CC BY-NC-ND 4.0 article (their licence was not verified separately). Expected sha256 are in `LICENSES.md`.

```sh
D=data/external/michaelis-2025-nature-supp; mkdir -p "$D"
B="https://media.springernature.com/original/springer-static/esm/art%3A10.1038%2Fs41586-025-09588-6/MediaObjects/41586_2025_9588_MOESM"
for n in 1_ESM.pdf 2_ESM.pdf 3_ESM.xlsx; do curl -fL -o "$D/41586_2025_9588_MOESM$n" "${B}${n}"; sleep 1; done
sha256sum "$D"/*
```

curl identifies itself as curl: the files are served to it as they are (checked 2026-10-07), so nothing here
presents the script as a browser, which nature.com's terms of use (clause 6.8, version of 15 December 2025) would
not allow. One request per second is the rate Springer Nature's text-and-data-mining policy asks of individual
researchers downloading from its platforms.

If the links change, use the "Supplementary information" and "Source data" links on the article page.

## 4. ALS GWAS summary statistics (scripted)

The input of the `a3_gwas_loci` analysis: GWAS Catalog study `GCST90027164`, the European-ancestries
meta-analysis of van Rheenen et al. (2021), ~188 MB gzipped.

```sh
scripts/data/gwas-data-download.sh --list-only      # published size and md5, download nothing
scripts/data/gwas-data-download.sh                  # -> data/external/gwas-als/, md5 verified
```

The script reads the checksum from the catalogue's own `md5sum.txt` rather than carrying a hard-coded
digest, resumes an interrupted transfer and keeps the file only if the md5 matches. The copy used by the
runs in this repository is `705fa6657d75163d313138798ce9a579` (197291290 bytes), which is the digest the
catalogue publishes.

**What this file is, and is not.** The study's own metadata
(`GCST90027164_buildGRCh37.tsv.gz-meta.yaml`) states `genome_assembly: GRCh37`, `file_type: pre-GWAS-SSF`
and `is_harmonised: false`. The study directory also offers a `harmonised/` subdirectory, which this
project does not use. The `license_note` in `configs/pipeline*.yaml` calls the file "harmonised", which
the catalogue's metadata contradicts; the analysis itself re-lifts and re-harmonises nothing
(`analysis/a3_gwas_loci.py`).

Licence: open access under the EMBL-EBI terms; cite the study. `LICENSES.md` has no row for this input
yet — a gap in the inventory, not a restriction.

## 5. IEDB / NetMHCIIpan prediction cache (scripted)

The input of the `a4_hla_binding` analysis. A4 is deliberately offline: it reads an append-only cache and
never calls the service. The cache is produced once by

```sh
scripts/data/iedb-cache-acquire.py --offline   # check an existing cache covers the grid; no network
scripts/data/iedb-cache-acquire.py            # request the missing pairs -> data/external/iedb-cache/netmhciipan.jsonl
```

The grid is not chosen by the script: the peptides are Table 1 of Michaelis et al. and the alleles those of
its Table 2, read through the same loaders and the same allele naming the analysis uses, so a pair A4 looks
up is a pair that was requested. Table 1's 21 distinct 15-mers against Table 2's 4 alleles make **84**
pairs, sent as one request per peptide. `DQB1*05:01` is paired with the assumed alpha chain
`DQA1*01:01`, an assumption the analysis tests against the paper's own printed ranks.

**Licence — read before running it.** The NetMHC component is served under the DTU academic licence: free
for a non-profit academic or research institution only, a commercial licence otherwise, and benchmark
results of the predictor may not be published (`LICENSES.md`). That the licence written for the
downloaded tool governs the API is an **inference**, recorded as such. The cache is git-ignored and never
redistributed.

## 6. Declaring an input to the pipeline

Acquiring a file is not enough: the phase pipeline uses only what the configuration declares. Add an entry to
`phases.data.sources` in `configs/pipeline*.yaml` with an `id`, the `path` (relative to the
repository root), a `kind` (`table`, `sumstats`, `supplement`, `raw`), a `license_note`, an `acquire` string
naming the script or the section of this page that produces it, and `required: false` if
the run may proceed without it. `acquire` is what the `data` phase prints when the file is absent, so write
it as an instruction to a user who has just cloned the repository. The `data` phase then resolves and hashes it into `data-manifest.json` and gives it
a citable `dataset:<id>@<hash>` reference; a missing required source stops the run, naming every absent source at
once. Nothing is auto-discovered.

Agents see the manifest, never the files: `mcp__data__data_list` and `mcp__data__data_describe` serve metadata only
(docs/pipeline.md, "How an agent reaches the data"). A dataset that no analysis reads answers `analysed_by: []`,
which is how an unused input stays visible instead of looking like evidence.
