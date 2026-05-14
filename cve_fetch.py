#!/usr/bin/env python3
"""Fetch CVE descriptions from Debian security tracker."""
import argparse, re, sys, time
from urllib.request import urlopen
from urllib.error import URLError

URL_PREFIX = "https://security-tracker.debian.org/tracker/"


def fetch_one(cve_id: str, retries: int = 5) -> tuple[str, str]:
    url = URL_PREFIX + cve_id
    for attempt in range(retries):
        try:
            with urlopen(url, timeout=30) as resp:
                text = resp.read().decode()
            break
        except URLError:
            if attempt == retries - 1:
                return cve_id, "FETCH_FAILED"
            time.sleep(1)
    # Extract description from the table
    m = re.search(r"Description.*?</td>\s*<td>(.*?)</td>", text, re.S)
    if not m:
        return cve_id, "DESCRIPTION_NOT_FOUND"
    desc = re.sub(r"<.*?>", "", m.group(1)).strip()
    # Extract the "module: short desc" part after "resolved: "
    m2 = re.search(r"resolved:\s*(.*)", desc)
    if m2:
        desc = m2.group(1).strip()
    # Remove "(cherry picked from commit ...)" and similar trailing bits
    desc = re.sub(r"\s*\(cherry picked from commit.*?\)", "", desc)
    # The title is everything before "  " (two+ spaces) or the first sentence
    # Format: "module/subsys: Title  Detail paragraph..."
    m3 = re.match(r"^(.+?(?:\.\s*|  ))", desc)
    if m3:
        title = m3.group(1).strip()
        # If title ends with double-space separator, strip it
        title = title.rstrip()
        desc = title
    else:
        desc = desc.split("\n")[0].strip()
    # Extract top-level module (e.g. "drm/xe/xe_pagefault: ..." -> "drm")
    # Skip leading Revert/Revert" prefix if present
    m4 = re.match(r'^(?:Revert\s+")?([^:/]+)', desc)
    module = m4.group(1).strip() if m4 else ""
    return cve_id, module, desc


def main():
    parser = argparse.ArgumentParser(
        description="Fetch CVE patch descriptions from Debian security tracker.\n"
                    "Reads CVE IDs from an input file, queries security-tracker.debian.org,\n"
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
        # Take the last field that looks like a CVE ID
        cve_id = next((p for p in reversed(parts) if p.startswith("CVE-")), parts[-1])
        entries.append(cve_id)

    # Fetch sequentially with rate limiting
    results = {}
    for i, cve in enumerate(entries):
        cve_id, module, desc = fetch_one(cve)
        results[cve_id] = (module, desc)
        print(f"[{i+1}/{len(entries)}] {cve_id}: {module} | {desc[:50]}...")
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
