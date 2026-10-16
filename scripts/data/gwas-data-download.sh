#!/usr/bin/env bash
#
# ALS GWAS summary statistics (GWAS Catalog GCST90027164) — the input of the A3 analysis.
#
# The file is the European-ancestries meta-analysis of van Rheenen et al. (2021), served by the
# GWAS Catalog FTP as submitted: its own metadata says `file_type: pre-GWAS-SSF`,
# `is_harmonised: false`, `genome_assembly: GRCh37`. The study directory also holds a `harmonised/`
# subdirectory, which this project does not use; `analysis/a3_gwas_loci.py` reads the GRCh37 file
# and re-lifts nothing.
#
# Licence: GWAS Catalog summary statistics are open access under the EMBL-EBI terms of use; cite the
# study when using them. LICENSES.md carries no row for this input yet, which is a gap in the
# inventory, not a permission: the file is public and its terms are the catalogue's.
#
#   scripts/data/gwas-data-download.sh --list-only      # show the published size and checksum, download nothing
#   scripts/data/gwas-data-download.sh                  # download to data/external/gwas-als/ and verify the md5
#   scripts/data/gwas-data-download.sh --dest /data/als # same, elsewhere
#
# The download is ~188 MB. An interrupted transfer is resumed (`curl -C -`), and the file is kept
# only if its md5 matches the checksum published beside it.

set -euo pipefail

BASE="https://ftp.ebi.ac.uk/pub/databases/gwas/summary_statistics/GCST90027001-GCST90028000/GCST90027164"
FILE="GCST90027164_buildGRCh37.tsv.gz"
DEST="data/external/gwas-als"
LIST_ONLY=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dest) DEST="${2:?--dest needs a directory}"; shift 2 ;;
        --list-only) LIST_ONLY=1; shift ;;
        -h|--help) sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "$(basename "$0"): unknown argument '$1'" >&2; exit 2 ;;
    esac
done

command -v curl >/dev/null || { echo "error: curl not found" >&2; exit 2; }

# The checksum is read from the catalogue, not hard-coded here: a hard-coded digest would silently
# become a claim about a file nobody checked again.
published_md5="$(curl -fsS "$BASE/md5sum.txt" | awk -v f="$FILE" '$2 == f {print $1}')"
[[ -n "$published_md5" ]] || { echo "error: $FILE is not listed in $BASE/md5sum.txt" >&2; exit 1; }
size="$(curl -fsSI "$BASE/$FILE" | awk 'tolower($1) == "content-length:" {gsub("\r", "", $2); print $2}')"

echo "study:     GCST90027164 (van Rheenen et al. 2021, European ancestries, GRCh37)"
echo "url:       $BASE/$FILE"
echo "size:      ${size:-unknown} bytes"
echo "md5:       $published_md5 (published)"
echo "metadata:  $BASE/${FILE}-meta.yaml"

if [[ "$LIST_ONLY" -eq 1 ]]; then
    exit 0
fi

mkdir -p "$DEST"
target="$DEST/$FILE"

if [[ -f "$target" ]] && [[ "$(md5sum "$target" | cut -d' ' -f1)" == "$published_md5" ]]; then
    echo "already present and verified: $target"
    exit 0
fi

echo "downloading to $target ..."
curl -fSL -C - -o "$target" "$BASE/$FILE"

local_md5="$(md5sum "$target" | cut -d' ' -f1)"
if [[ "$local_md5" != "$published_md5" ]]; then
    echo "error: md5 mismatch (local $local_md5, published $published_md5); the file is NOT usable" >&2
    exit 1
fi
echo "verified: $target ($local_md5)"
