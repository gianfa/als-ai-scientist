#!/usr/bin/env bash
# Download the Michaelis et al. (Nature 2025) FCS/FlowJo data from Zenodo.
#
# Prints the Zenodo references, lists every file with its size, then (after
# confirmation) downloads them ONE FILE AT A TIME into a destination folder,
# verifying the md5 published by Zenodo. Files already present and valid are skipped.
#
# Third-party data: keep the destination outside version control (local-only, Release: NO).
# See LICENSES.md. The records are CC BY 4.0; cite the Zenodo DOIs when using them.
#
# Usage:
#   scripts/data/michaelis-data-download.sh [--dest DIR] [--records ID[,ID...]] [--max-bytes N]
#                                      [--list-only] [--yes]
#
# Records (default: the four ICS records):
#   16749283  Spectral data (Cytek Aurora)   ~1.6 GB  (large: opt in with --records)
#   16749334  ICS data Screen 1
#   16754553  ICS data Screen 2
#   16754591  ICS data Screen 3
#   16754609  ICS data Screen 4
set -euo pipefail

DEST="data/external/michaelis-2025-zenodo"
RECORDS="16749334,16754553,16754591,16754609"
MAX_BYTES=2000000000
LIST_ONLY=0
ASSUME_YES=0

usage() { sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
  case "$1" in
    --dest) DEST="${2:?--dest needs a value}"; shift 2 ;;
    --records) RECORDS="${2:?--records needs a value}"; shift 2 ;;
    --max-bytes) MAX_BYTES="${2:?--max-bytes needs a value}"; shift 2 ;;
    --list-only) LIST_ONLY=1; shift ;;
    --yes|-y) ASSUME_YES=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

for tool in curl python3; do
  command -v "$tool" >/dev/null || { echo "Missing required tool: $tool" >&2; exit 1; }
done

MANIFEST_DIR="$(mktemp -d)"
trap 'rm -rf "$MANIFEST_DIR"' EXIT

echo "Michaelis et al., Nature (2025), https://doi.org/10.1038/s41586-025-09588-6"
echo "Zenodo records (fetching metadata, nothing is downloaded yet):"
echo

# One JSON per record -> "key<TAB>size<TAB>md5" manifest, plus a human summary.
IFS=',' read -r -a IDS <<< "$RECORDS"
TOTAL=0
for id in "${IDS[@]}"; do
  json="$MANIFEST_DIR/$id.json"
  curl -sSfL --retry 3 -m 60 -o "$json" "https://zenodo.org/api/records/$id" \
    || { echo "Cannot fetch metadata for record $id" >&2; exit 1; }
  size=$(python3 - "$json" "$MANIFEST_DIR/$id.tsv" "$id" <<'PY'
import json, sys
json_path, tsv_path, rid = sys.argv[1:4]
d = json.load(open(json_path))
m = d["metadata"]
files = d["files"]
with open(tsv_path, "w") as out:
    for f in files:
        out.write(f"{f['key']}\t{f['size']}\t{f['checksum'].split(':', 1)[1]}\t{f['links']['self']}\n")
total = sum(f["size"] for f in files)
lic = m.get("license", {}).get("id", "unknown")
print(f"record {rid}: {m['title']}", file=sys.stderr)
print(f"  DOI https://doi.org/{d.get('doi', '10.5281/zenodo.' + rid)} | license {lic} | "
      f"{len(files)} files | {total / 1e6:.1f} MB", file=sys.stderr)
for f in sorted(files, key=lambda x: x["key"]):
    print(f"    {f['size'] / 1e6:9.2f} MB  {f['key']}", file=sys.stderr)
print(total)
PY
  )
  TOTAL=$((TOTAL + size))
  echo
done

echo "Total: $((TOTAL / 1000000)) MB across ${#IDS[@]} record(s); cap: $((MAX_BYTES / 1000000)) MB"
if [ "$TOTAL" -gt "$MAX_BYTES" ]; then
  echo "Total exceeds --max-bytes. Select fewer records with --records or raise --max-bytes." >&2
  exit 1
fi
echo "Destination: $DEST"

[ "$LIST_ONLY" -eq 1 ] && exit 0

if [ "$ASSUME_YES" -ne 1 ]; then
  read -r -p "Download these files to '$DEST'? [y/N] " answer
  case "$answer" in y|Y|yes|YES) ;; *) echo "Aborted."; exit 0 ;; esac
fi

FAILED=0
for id in "${IDS[@]}"; do
  mkdir -p "$DEST/$id"
  while IFS=$'\t' read -r key size md5 url; do
    target="$DEST/$id/$key"
    if [ -f "$target" ] && [ "$(md5sum "$target" | cut -d' ' -f1)" = "$md5" ]; then
      echo "skip  $id/$key"
      continue
    fi
    # Sequential, single connection. Zenodo may return transient 5xx: curl retries.
    if curl -sSfL --retry 5 --retry-delay 3 --retry-all-errors -o "$target" "$url" \
       && [ "$(md5sum "$target" | cut -d' ' -f1)" = "$md5" ]; then
      echo "ok    $id/$key"
    else
      echo "FAIL  $id/$key (download error or md5 mismatch)" >&2
      rm -f "$target"
      FAILED=$((FAILED + 1))
    fi
  done < "$MANIFEST_DIR/$id.tsv"
done

if [ "$FAILED" -gt 0 ]; then
  echo "$FAILED file(s) failed. Re-run the script to retry (valid files are skipped)." >&2
  exit 1
fi
echo "Done. Files are in $DEST (keep them out of Git)."
