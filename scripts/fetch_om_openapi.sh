#!/usr/bin/env bash
# Fetch the OpenMetadata OpenAPI spec from a running server (REQUIREMENTS §0.4, FR-OM-08).
#
# Usage: scripts/fetch_om_openapi.sh <om-url> <out.json>
#   e.g. scripts/fetch_om_openapi.sh http://localhost:8585 \
#          src/lakeloom/om/openapi/openmetadata-2.1.0-rc1.json
#
# Tries /swagger.json, /api/swagger.json, /openapi.json in that order and writes
# the first valid JSON document, key-sorted with `jq -S .` for stable diffs.
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: $0 <om-url> <out.json>" >&2
  exit 2
fi
om_url="${1%/}"
out="$2"
command -v jq >/dev/null || { echo "jq is required" >&2; exit 1; }

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

for path in /swagger.json /api/swagger.json /openapi.json; do
  if curl -fsS -o "$tmp" "$om_url$path" 2>/dev/null && jq -e '.paths | length > 0' "$tmp" >/dev/null 2>&1; then
    mkdir -p "$(dirname "$out")"
    jq -S . "$tmp" > "$out"
    echo "fetched $om_url$path -> $out ($(jq '.paths | length' "$out") paths)" >&2
    exit 0
  fi
  echo "no spec at $om_url$path" >&2
done
echo "no OpenAPI spec found at $om_url" >&2
exit 1
