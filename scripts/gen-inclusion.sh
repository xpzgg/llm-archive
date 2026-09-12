#!/bin/bash
#
# Generate inclusion template for CVE backport
#
# Usage: gen-inclusion.sh <stable|mainline> -c <commit> -i <issue_number> -e <CVE>

set -euo pipefail

usage() {
    echo "Usage: $0 <stable|mainline> -p <repo_path> -c <commit> -i <issue_number>"
    exit 1
}

TYPE="${1:-}"
[[ -z "$TYPE" ]] && usage
shift

COMMIT=""
ISSUE=""
CVE=""

while getopts "c:i:e:" opt; do
    case $opt in
        c) COMMIT="$OPTARG" ;;
        i) ISSUE="$OPTARG" ;;
        e) CVE="$OPTARG" ;;
        *) usage ;;
    esac
done

[[ "$TYPE" != "stable" && "$TYPE" != "mainline" ]] && { echo "Error: must be 'stable' or 'mainline'" >&2; exit 1; }
[[ -z "$COMMIT" || -z "$ISSUE" || -z "$CVE" ]] && usage

GIT="git"

# Resolve full commit hash
FULL_COMMIT=$($GIT rev-parse "$COMMIT" 2>/dev/null) || { echo "Error: commit not found: $COMMIT" >&2; exit 1; }

# Get version tag containing this commit
VERSION=$($GIT describe --contains "$FULL_COMMIT" 2>/dev/null | sed 's/[~^].*//') || true

# Construct URLs
BUGZILLA="https://atomgit.com/src-openeuler/kernel/issues/${ISSUE}"

if [[ "$TYPE" == "mainline" ]]; then
    REF="https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/commit/?id=${FULL_COMMIT}"
else
    REF="https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git/commit/?id=${FULL_COMMIT}"
fi

# Output
echo "${TYPE} inclusion"
echo "from ${TYPE}-${VERSION:-<version>}"
echo "commit ${FULL_COMMIT}"
echo "category: bugfix"
echo "bugzilla: ${BUGZILLA}"
echo "CVE: ${CVE}"
echo ""
echo "Reference: ${REF}"
