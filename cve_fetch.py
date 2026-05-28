#!/usr/bin/env python3
"""Fetch CVE descriptions from NVD API."""
import argparse, json, re, sys, time
from urllib.request import urlopen, Request
from urllib.error import URLError

NVD_API = "https://services.nvd.nist.gov/rest/json/cves/2.0?cveId="


def fetch_one(cve_id: str, retries: int = 5) -> tuple[str, str]:
    url = NVD_API + cve_id
    for attempt in range(retries):
        try:
            req = Request(url, headers={"User-Agent": "cve_fetch/1.0"})
            with urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
            break
        except URLError:
            if attempt == retries - 1:
                return cve_id, "FETCH_FAILED"
            time.sleep(2)
    try:
        vulns = data["vulnerabilities"]
        if not vulns:
            return cve_id, "NOT_FOUND"
        desc = vulns[0]["cve"]["descriptions"][0]["value"]
    except (KeyError, IndexError):
        return cve_id, "PARSE_ERROR"
    # Strip the standard prefix
    m = re.search(r"resolved:\s*", desc)
    if m:
        desc = desc[m.end():]
    # Take first line (the actual patch subject)
    desc = desc.split("\n")[0].strip()
    # Remove "(cherry picked from commit ...)"
    desc = re.sub(r"\s*\(cherry picked from commit.*?\)", "", desc)
    # Extract top-level module (e.g. "drm/xe/xe_pagefault: ..." -> "drm")
    m4 = re.match(r'^(?:Revert\s+")?([^:/]+)', desc)
    module = m4.group(1).strip() if m4 else ""
    return cve_id, module, desc


def main():
    parser = argparse.ArgumentParser(
        description="Fetch CVE patch descriptions from NVD.\n"
                    "Reads CVE IDs from an input file, queries NVD API,\n"
                    "and overwrites the file with tab-separated results:\n"
                    "  CVE_ID<TAB>module<TAB>description",
        epilog="example:\n"
               "  %(prog)s cve.txt\n"
               "  %(prog)s          # defaults to cve.txt",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "input_file", nargs="?", default="cve.txt",
        help="input file containing one CVE ID per line "
             "(default: cve.txt). The file is overwritten in-place "
             "with fetched results.",
    )
    args = parser.parse_args()

    input_file = args.input_file
    with open(input_file) as f:
        lines = [l.rstrip("\n") for l in f if l.strip()]

    # Parse existing lines: one CVE per line (skip seq number if present)
    entries = []
    for line in lines:
        parts = line.split()
        cve_id = next((p for p in reversed(parts) if p.startswith("CVE-")), parts[-1])
        entries.append(cve_id)

    # Fetch sequentially with rate limiting
    results = {}
    for i, cve in enumerate(entries):
        cve_id, module, desc = fetch_one(cve)
        results[cve_id] = (module, desc)
        print(f"[{i+1}/{len(entries)}] {cve_id}: {module} | {desc[:60]}...")
        if i < len(entries) - 1:
            time.sleep(0.5)

    # Write output
    with open(input_file, "w") as f:
        for cve_id in entries:
            module, desc = results.get(cve_id, ("", ""))
            f.write(f"{cve_id}\t{module}\t{desc}\n")

    print(f"\nDone. Updated {len(entries)} entries in {input_file}")


if __name__ == "__main__":
    main()
