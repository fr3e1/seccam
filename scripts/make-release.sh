#!/usr/bin/env bash
set -euo pipefail

# Build the release tarball and checksum file for the web updater.
# Usage: make-release.sh VERSION OUTDIR   (run inside the git repository; packages HEAD)
# The tarball's top directory is seccam-VERSION, which the updater requires.

if [[ $# -ne 2 ]]; then
    echo "Usage: $0 VERSION OUTDIR" >&2
    exit 2
fi
VERSION="$1"
OUTDIR="$2"

if ! [[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "VERSION must look like 1.2.3" >&2
    exit 2
fi
FILE_VERSION="$(tr -d '[:space:]' < VERSION)"
if [[ "$FILE_VERSION" != "$VERSION" ]]; then
    echo "VERSION argument ($VERSION) does not match the VERSION file ($FILE_VERSION)" >&2
    exit 1
fi

mkdir -p "$OUTDIR"
TARBALL="seccam-${VERSION}.tar.gz"
git archive --format=tar.gz --prefix="seccam-${VERSION}/" -o "$OUTDIR/$TARBALL" HEAD
(cd "$OUTDIR" && sha256sum "$TARBALL" > SHA256SUMS)
echo "Wrote $OUTDIR/$TARBALL and $OUTDIR/SHA256SUMS"
