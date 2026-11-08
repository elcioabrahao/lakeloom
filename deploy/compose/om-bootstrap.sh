#!/bin/sh
# One-shot bootstrap for the Lakeloom dev stack (NFR-DEP-05).
#
# Logs in to OpenMetadata as the default admin, reads the JWT of the
# `ingestion-bot` bot user and writes it to $OM_BOT_TOKEN_FILE
# (named volume `om-secrets`, mounted read-only into Lakeloom; owner 10001:10001, mode 0440).
#
# Endpoints checked against the committed spec
# src/lakeloom/om/openapi/openmetadata-2.1.0-rc1.json (see ADR 0003):
#   POST /api/v1/users/login                 body {email, password(base64)} -> accessToken
#   GET  /api/v1/bots/name/{name}            -> botUser.id
#   GET  /api/v1/users/auth-mechanism/{id}   -> config.JWTToken
#
# DEVELOPMENT ONLY: the admin credentials are OM's public quickstart defaults.
# Never echoes the admin password or the bot token.
set -eu

OM_URL="${OM_URL:-http://openmetadata-server:8585}"
OM_ADMIN_EMAIL="${OM_ADMIN_EMAIL:-admin@open-metadata.org}"
OM_ADMIN_PASSWORD="${OM_ADMIN_PASSWORD:-admin}"
OM_BOT_NAME="${OM_BOT_NAME:-ingestion-bot}"
OM_BOT_TOKEN_FILE="${OM_BOT_TOKEN_FILE:-/secrets/om_bot_token}"

if ! command -v curl >/dev/null 2>&1 || ! command -v jq >/dev/null 2>&1; then
  apk add --no-cache --quiet curl jq
fi

log() { echo "om-bootstrap: $*" >&2; }

i=0
until curl -fsS "$OM_URL/api/v1/system/version" >/dev/null 2>&1; do
  i=$((i + 1))
  if [ "$i" -ge 60 ]; then log "OpenMetadata not reachable at $OM_URL"; exit 1; fi
  sleep 5
done
log "OpenMetadata $(curl -fsS "$OM_URL/api/v1/system/version" | jq -r .version) reachable"

pw_b64=$(printf '%s' "$OM_ADMIN_PASSWORD" | base64 | tr -d '\n')
admin_token=$(jq -n --arg e "$OM_ADMIN_EMAIL" --arg p "$pw_b64" '{email: $e, password: $p}' \
  | curl -fsS -X POST "$OM_URL/api/v1/users/login" -H 'Content-Type: application/json' -d @- \
  | jq -r '.accessToken // empty')
[ -n "$admin_token" ] || { log "admin login failed"; exit 1; }
log "logged in as $OM_ADMIN_EMAIL"

auth="Authorization: Bearer $admin_token"
bot_user_id=$(curl -fsS -H "$auth" "$OM_URL/api/v1/bots/name/$OM_BOT_NAME" | jq -r '.botUser.id // empty')
[ -n "$bot_user_id" ] || { log "bot $OM_BOT_NAME not found"; exit 1; }

bot_token=$(curl -fsS -H "$auth" "$OM_URL/api/v1/users/auth-mechanism/$bot_user_id" \
  | jq -r '.config.JWTToken // empty')
[ -n "$bot_token" ] || { log "no JWT for bot $OM_BOT_NAME"; exit 1; }

# Sanity check: the token must authenticate.
curl -fsS -o /dev/null -H "Authorization: Bearer $bot_token" "$OM_URL/api/v1/users/loggedInUser" \
  || { log "bot token rejected by OpenMetadata"; exit 1; }

umask 022
mkdir -p "$(dirname "$OM_BOT_TOKEN_FILE")"
tmp="$OM_BOT_TOKEN_FILE.tmp"
printf '%s' "$bot_token" > "$tmp"
# Readable by the Lakeloom container user (uid/gid 10001) only; not world-readable.
chown 10001:10001 "$tmp"
chmod 0440 "$tmp"
mv "$tmp" "$OM_BOT_TOKEN_FILE"
log "wrote $OM_BOT_NAME token to $OM_BOT_TOKEN_FILE (${#bot_token} chars)"
