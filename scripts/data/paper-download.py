#!/usr/bin/env python3
"""Download an openly licensed article to a local, never-committed path, stating its licence first.

Section 1 of `docs/data-acquisition.md` was the only acquisition step with no script, because an article is
not a dataset: whether a copy may be made at all depends on its licence. So this script does not start from a
URL. It starts from a DOI, reads what Crossref and Europe PMC state about that work — the licence, the
version, the open-access locations — prints it, and downloads only from a location one of them calls open. If
neither states an open licence, it refuses and says so; it never tries a publisher URL on its own, and it
circumvents nothing.

    scripts/data/paper-download.py --list-only     # print what the registries say, download nothing
    scripts/data/paper-download.py                 # print it, ask for confirmation, then fetch the full text
    scripts/data/paper-download.py --format pdf    # print the URL to open by hand, nothing else (see below)
    scripts/data/paper-download.py --yes --doi 10.xxxx/yyy --dest literature/inbox/other.xml

**The PDF is not downloaded by this script, and that is deliberate.** Both open-access PDF locations of the
default article refuse a programmatic client: Europe PMC's render endpoint answers 403, nature.com answers a
consent page, and PMC's PDF path redirects. Getting past that would mean presenting this script as a browser,
which is working around an access control the host put there on purpose — a different question from the
licence, and not one an open licence answers. So `--format pdf` prints the URL and asks you to open it; the
licence notice is printed by the other modes, and a DOI with no open licence is still refused.

What is scripted is the full text Europe PMC publishes for programmatic use (`fullTextXML`), when the article
is in its open-access subset. Defaults to Michaelis et al. (2025) and `literature/inbox/`, which `.gitignore` keeps out
of Git and where `ingest.py` looks for papers added by hand.

The notice this prints repeats registry metadata and this repository's own rules. It is not legal advice.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

CROSSREF = "https://api.crossref.org/works/"
EUROPE_PMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
DEFAULT_DOI = "10.1038/s41586-025-09588-6"
DEFAULT_DEST = {"xml": Path("literature/inbox/michaelis-2025.xml"), "pdf": Path("literature/inbox/michaelis-2025.pdf")}
EPMC_FULLTEXT = "https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"
TIMEOUT_S = 120.0
# Read off the licence URL, not assumed: these are the clauses of the CC codes this project meets.
CC_CLAUSES = {
    "by": "attribution: credit the authors and the source",
    "nc": "non-commercial use only",
    "nd": "no derivatives may be distributed (an adaptation may be made locally, not shared)",
    "sa": "share-alike: a distributed adaptation carries the same licence",
}


@dataclass
class Work:
    """What the registries state about one DOI. Empty fields mean 'not stated', never 'not restricted'."""

    doi: str
    title: str = ""
    container: str = ""
    issued: str = ""
    licences: list[dict[str, str]] = field(default_factory=list)  # {url, version, start, stated_by}
    locations: list[dict[str, str]] = field(default_factory=list)  # {url, kind, stated_by}
    pmcid: str = ""
    open_access: str = ""

    @property
    def open_licences(self) -> list[dict[str, str]]:
        return [l for l in self.licences if "creativecommons.org" in l["url"]]


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(
        urllib.request.Request(url, headers={"Accept": "application/json"}), timeout=TIMEOUT_S
    ) as resp:
        return json.loads(resp.read().decode())


def from_crossref(doi: str, work: Work) -> Work:
    message = _get_json(CROSSREF + urllib.parse.quote(doi)).get("message", {})
    work.title = (message.get("title") or [""])[0]
    work.container = (message.get("container-title") or [""])[0]
    parts = (message.get("issued") or {}).get("date-parts") or [[]]
    work.issued = "-".join(f"{p:02d}" if i else str(p) for i, p in enumerate(parts[0]))
    for lic in message.get("license") or []:
        work.licences.append(
            {
                "url": (lic.get("URL") or "").rstrip("/"),
                "version": lic.get("content-version") or "",
                "start": ((lic.get("start") or {}).get("date-time") or "")[:10],
                "stated_by": "Crossref",
            }
        )
    for link in message.get("link") or []:
        if (link.get("content-type") or "").endswith("pdf"):
            work.locations.append(
                {
                    "url": link["URL"],
                    "kind": f"publisher PDF ({link.get('content-version', '')})",
                    "stated_by": "Crossref",
                }
            )
    return work


def from_europe_pmc(doi: str, work: Work) -> Work:
    query = urllib.parse.urlencode({"query": f'DOI:"{doi}"', "resultType": "core", "format": "json"})
    results = _get_json(f"{EUROPE_PMC}?{query}").get("resultList", {}).get("result", [])
    if not results:
        return work
    record = results[0]
    work.pmcid = record.get("pmcid") or ""
    work.open_access = record.get("isOpenAccess") or ""
    if record.get("license"):
        work.licences.append(
            {
                "url": f"cc {record['license']}".replace("cc cc", "cc"),
                "version": "",
                "start": "",
                "stated_by": "Europe PMC",
            }
        )
    for url in record.get("fullTextUrlList", {}).get("fullTextUrl", []) or []:
        if url.get("availability") == "Open access" and url.get("documentStyle") == "pdf":
            work.locations.append({"url": url["url"], "kind": "Europe PMC open-access PDF", "stated_by": "Europe PMC"})
    return work


def clauses(licence_url: str) -> list[str]:
    """The clauses of a Creative Commons licence, decoded from its own code."""
    code = licence_url.lower()
    return [text for key, text in CC_CLAUSES.items() if f"/{key}" in code or f"cc {key}" in code or f"-{key}" in code]


def _preferred(locations: list[dict[str, str]]) -> list[dict[str, str]]:
    """De-duplicated by URL, with the locations a registry calls open access tried first.

    A publisher link Crossref offers for mining is legitimate, but it is the one most likely to answer a bot
    with a landing page; a repository that declares the copy open access is the safer first attempt.
    """
    unique: dict[str, dict[str, str]] = {}
    for loc in locations:
        unique.setdefault(loc["url"], loc)
    return sorted(unique.values(), key=lambda loc: 0 if "open-access" in loc["kind"] else 1)


def notice(work: Work) -> str:
    lines = ["You are about to download:", f"  {work.title}" if work.title else f"  DOI {work.doi}"]
    if work.container:
        lines.append(f"  {work.container}, {work.issued}  (DOI {work.doi})")
    lines += ["", "Licence, as the registries state it:"]
    if not work.licences:
        lines.append("  neither Crossref nor Europe PMC states a licence for this DOI")
    for lic in work.licences:
        version = f", applies to the {lic['version']} version" if lic["version"] else ""
        since = f", from {lic['start']}" if lic["start"] else ""
        lines.append(f"  {lic['url']}  (stated by {lic['stated_by']}{version}{since})")
    seen: set[str] = set()
    for lic in work.open_licences:
        for clause in clauses(lic["url"]):
            if clause not in seen:
                seen.add(clause)
                lines.append(f"    - {clause}")
    if work.pmcid:
        lines.append(f"  Europe PMC: {work.pmcid}, isOpenAccess={work.open_access or 'not stated'}")
    lines += ["", "Where it would come from (only locations a registry calls open or offers for mining):"]
    for loc in work.locations:
        lines.append(f"  {loc['kind']} — {loc['url']}  (via {loc['stated_by']})")
    lines += [
        "",
        "This repository's own rules for the copy you are about to make:",
        "  - it is local only: the destination is git-ignored, and the PDF is never committed",
        "  - a derivative of it (the Paper2Agent extraction) stays local too, which is what the",
        "    no-derivatives clause requires of anything shared (LICENSES.md, docs/licensing.md)",
        "  - cite the article in anything built on it",
        "",
        "The lines above repeat registry metadata and this repository's rules. Not legal advice.",
    ]
    return "\n".join(lines)


def fetch_full_text(work: Work, dest: Path) -> dict[str, str]:
    """Fetch the open-access full text Europe PMC serves for programmatic use, or raise saying why not."""
    if not work.pmcid:
        raise RuntimeError("Europe PMC lists no PMCID for this DOI, so it serves no full text for it")
    url = EPMC_FULLTEXT.format(pmcid=work.pmcid)
    request = urllib.request.Request(url, headers={"Accept": "application/xml"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as resp:
        body = resp.read()
        served = resp.headers.get("Content-Type", "")
    if b"<article" not in body[:4000]:
        raise RuntimeError(
            f"{url}: served {served or 'unknown type'}, not a JATS article "
            "(the article may not be in the open-access subset)"
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(body)
    return {"url": url, "bytes": str(len(body)), "sha256": hashlib.sha256(body).hexdigest()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--doi", default=DEFAULT_DOI)
    parser.add_argument(
        "--format",
        choices=("xml", "pdf"),
        default="xml",
        help="xml: fetch the open-access full text; pdf: print the URL to open by hand",
    )
    parser.add_argument(
        "--dest", type=Path, default=None, help=f"default {DEFAULT_DEST['xml']} for xml, {DEFAULT_DEST['pdf']} for pdf"
    )
    parser.add_argument("--list-only", action="store_true", help="print the notice and exit")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = parser.parse_args(argv)
    dest = args.dest or DEFAULT_DEST[args.format]

    work = from_europe_pmc(args.doi, from_crossref(args.doi, Work(doi=args.doi)))
    work.locations = _preferred(work.locations)
    if args.format == "xml" or args.list_only:  # the PDF is fetched by hand: it only needs the link
        print(notice(work))

    if not work.open_licences:
        print(
            "\nrefusing to download: no open licence is stated for this DOI. Obtain it through your "
            "institution instead, by hand.",
            file=sys.stderr,
        )
        return 1
    if not work.locations:
        print("\nrefusing to download: no registry offers a location for this DOI.", file=sys.stderr)
        return 1
    if args.list_only:
        return 0

    if args.format == "pdf":
        pdfs = [loc for loc in work.locations if "PDF" in loc["kind"] or "pdf" in loc["url"]]
        dest.parent.mkdir(parents=True, exist_ok=True)  # the folder is there to save into
        print(
            f"Please download the PDF:\n  {(pdfs or work.locations)[0]['url']}\n"
            f"and save it as:\n  {dest.resolve()}\n"
            "-------\n"
            "The hosts refuse scripted downloads, so you need to download it yourself."
        )
        return 0

    if not args.yes:
        answer = input(f"\nFetch the open-access full text to {dest}? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("not downloaded.")
            return 0

    written = fetch_full_text(work, dest)
    print(
        f"\nwritten: {dest} ({written['bytes']} bytes)\n  from:   {written['url']}\n" f"  sha256: {written['sha256']}"
    )
    print(
        "This is the JATS full text, not the PDF the Paper2Agent step reads: for that, run this script with "
        "--format pdf and open the URL it prints."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
