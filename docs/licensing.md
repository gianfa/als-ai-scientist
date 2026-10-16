# Licensing and release boundary

Original ALS AI Scientist code is licensed under Apache-2.0. This does not relicense third-party dependencies,
papers, datasets, models, generated extracts or assets. The audited inventory is in `LICENSES.md`; notices for
material actually shipped are in `THIRD_PARTY_NOTICES.md`.

## Scientific inputs

The Michaelis et al. article, local PDF and Paper2Agent extraction are not included in the repository or plugin
release. The article is published under CC BY-NC-ND 4.0. Users who need it must obtain it from the
[publisher's source](https://doi.org/10.1038/s41586-025-09588-6) and comply with its terms.

The project may distribute identifiers, source URLs, expected hashes, acquisition instructions and code that reads
user-provided inputs. Those records do not redistribute the source material and do not imply permission to publish
derived artifacts. Missing local inputs must produce a clear error; the tooling must not silently use an unofficial
mirror or substitute data.

## Preparing a release

Build archives from a reviewed commit using `git archive` or an equivalent process that consumes only Git-tracked
files. Never create a release by copying the working directory: ignored paths contain local papers, extracted
material, generated results, installed packages and environments.

Before bundling a dependency currently marked `Release: NO`, re-audit it, add required license texts/notices and
change its release decision explicitly. Treat generated computational artifacts the same way: retain input hashes
and methods, then make a separate redistribution decision.

This documentation records the project's compliance decisions; it is not legal advice.
