# data/

Raw and input data for the project.

- `external/` — third-party inputs acquired by the user, **git-ignored, never committed**: Michaelis et al. Zenodo FCS/FlowJo
  files and Nature supplements, ALS GWAS summary statistics, cached prediction responses. How to obtain them:
  [`docs/data-acquisition.md`](../docs/data-acquisition.md); licences and hashes: [`LICENSES.md`](../LICENSES.md).
  Download with `scripts/data/michaelis-data-download.sh` (`--list-only` first).

Datasets derived by us (tables built from these inputs) belong in `datasets/`.
