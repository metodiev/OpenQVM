#!/usr/bin/env bash
# Download a small Alpine Linux "virt" ISO for the end-to-end test.
set -euo pipefail
destination="${1:-${TMPDIR:-/tmp}/openqvm-alpine-virt.iso}"
url="https://dl-cdn.alpinelinux.org/alpine/v3.24/releases/x86_64/alpine-virt-3.24.2-x86_64.iso"
echo "Downloading $url"
curl -L --fail --retry 2 -o "$destination" "$url"
echo "Saved to $destination"
echo "Run: OPENQVM_TEST_ISO='$destination' pytest tests/end_to_end -m e2e -q -s"
