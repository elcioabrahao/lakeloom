#!/bin/sh
# One-shot bootstrap for the Lakeloom dev stack (NFR-DEP-05, FR-OM-05, NFR-SEC-08).
#
# OpenMetadata authenticates with Keycloak SSO (ADR 0012). This script:
#   1. gets a token for `om-admin` (OM's admin principal) from Keycloak: password grant on the
#      public client `lakeloom-dev-cli` with the optional scope `openmetadata-audience`, so
#      the token's `aud` contains `openmetadata` (OM rejects tokens without it);
#   2. idempotently creates the OM users alice, bob and carol (OM does not provision unknown
#      Keycloak users on the API: it answers 404 "Cannot find user with provided name and
#      email");
#   3. ensures the reviewer role $OM_REVIEWER_ROLE (and its policy) exists and grants it to
#      $OM_REVIEWER_USERS only (alice; carol deliberately gets no OM reviewer permission);
#   4. reads the JWT of the `ingestion-bot` bot user and writes it to $OM_BOT_TOKEN_FILE
#      (named volume `om-secrets`, mounted read-only into Lakeloom; owner 10001:10001, mode 0440).
# Safe to re-run: every step is a lookup-then-create/patch.
#
# Endpoints checked against the committed spec
# src/lakeloom/om/openapi/openmetadata-2.1.0-rc1.json (see ADR 0003):
#   GET  /api/v1/users/loggedInUser          -> the caller (checks the om-admin token)
#   GET  /api/v1/users/name/{name}?fields=roles, POST /api/v1/users (CreateUser),
#   PATCH /api/v1/users/{id} (JSON Patch)
#   GET  /api/v1/policies/name/{name}, POST /api/v1/policies (CreatePolicy)
#   GET  /api/v1/roles/name/{name}, POST /api/v1/roles (CreateRole)
#   GET  /api/v1/bots/name/{name}            -> botUser.id
#   GET  /api/v1/users/auth-mechanism/{id}   -> config.JWTToken
#
# DEVELOPMENT ONLY: om-admin/om-admin is a fixed public dev credential from the dev realm.
# Never echoes a password or a token.
set -eu

OM_URL="${OM_URL:-http://openmetadata-server:8585}"
KC_TOKEN_URL="${KC_TOKEN_URL:-http://keycloak:8080/realms/lakeloom/protocol/openid-connect/token}"
KC_CLIENT_ID="${KC_CLIENT_ID:-lakeloom-dev-cli}"
OM_ADMIN_USER="${OM_ADMIN_USER:-om-admin}"
OM_ADMIN_PASSWORD="${OM_ADMIN_PASSWORD:-om-admin}"
OM_USER_DOMAIN="${OM_USER_DOMAIN:-example.invalid}"
OM_USERS="${OM_USERS:-alice bob carol}"
OM_REVIEWER_ROLE="${OM_REVIEWER_ROLE:-LakeloomOntologyReviewer}"
OM_REVIEWER_USERS="${OM_REVIEWER_USERS:-alice}"
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

# --- 1. om-admin token from Keycloak ------------------------------------------------------
i=0
admin_token=""
while [ -z "$admin_token" ]; do
  admin_token=$(curl -sS "$KC_TOKEN_URL" \
    --data-urlencode grant_type=password \
    --data-urlencode "client_id=$KC_CLIENT_ID" \
    --data-urlencode "username=$OM_ADMIN_USER" \
    --data-urlencode "password=$OM_ADMIN_PASSWORD" \
    --data-urlencode "scope=openid openmetadata-audience" 2>/dev/null \
    | jq -r '.access_token // empty' 2>/dev/null || true)
  if [ -z "$admin_token" ]; then
    i=$((i + 1))
    if [ "$i" -ge 30 ]; then log "no Keycloak token for $OM_ADMIN_USER"; exit 1; fi
    sleep 5
  fi
done
auth="Authorization: Bearer $admin_token"

om() { # om METHOD PATH [JSON body] [content type] -> body on stdout; fails on HTTP >= 400
  if [ $# -ge 3 ]; then
    printf '%s' "$3" | curl -fsS -X "$1" "$OM_URL/api$2" -H "$auth" \
      -H "Content-Type: ${4:-application/json}" -d @-
  else
    curl -fsS -X "$1" "$OM_URL/api$2" -H "$auth"
  fi
}
om_status() { # om_status PATH -> HTTP status of a GET
  curl -sS -o /dev/null -w '%{http_code}' "$OM_URL/api$1" -H "$auth"
}

me=$(om GET /v1/users/loggedInUser) || { log "$OM_ADMIN_USER token rejected by OpenMetadata"; exit 1; }
[ "$(printf '%s' "$me" | jq -r .isAdmin)" = "true" ] \
  || { log "$OM_ADMIN_USER is not an OpenMetadata admin (AUTHORIZER_ADMIN_PRINCIPALS)"; exit 1; }
log "authenticated to OpenMetadata as $(printf '%s' "$me" | jq -r .name) (admin)"

# --- 2. users -------------------------------------------------------------------------------
for u in $OM_USERS; do
  if [ "$(om_status "/v1/users/name/$u")" = "200" ]; then
    log "user $u exists"
  else
    body=$(jq -n --arg n "$u" --arg e "$u@$OM_USER_DOMAIN" '{name: $n, email: $e, displayName: $n}')
    om POST /v1/users "$body" >/dev/null
    log "created user $u"
  fi
done

# --- 3. reviewer role -----------------------------------------------------------------------
# Least privilege for applying ontology change sets and the glossary / native-ontology edits
# an apply performs on models the bot created (ADR 0012 § OM reviewer role).
policy="${OM_REVIEWER_ROLE}Policy"
if [ "$(om_status "/v1/policies/name/$policy")" != "200" ]; then
  body=$(jq -n --arg n "$policy" '{
    name: $n,
    displayName: "Lakeloom ontology reviewer (DEV)",
    description: "DEV ONLY (Lakeloom dev stack): lets a human reviewer apply ontology change sets.",
    rules: [{
      name: "LakeloomReviewerApplyChangeSets",
      effect: "allow",
      resources: ["ontologyChangeSet", "glossary", "glossaryTerm", "ontology",
                  "ontologyConcept", "ontologyProperty", "ontologyAxiom"],
      operations: ["ViewAll", "Create", "EditAll"]
    }]
  }')
  om POST /v1/policies "$body" >/dev/null
  log "created policy $policy"
fi
policy_id=$(om GET "/v1/policies/name/$policy" | jq -r .id)
if [ "$(om_status "/v1/roles/name/$OM_REVIEWER_ROLE")" != "200" ]; then
  body=$(jq -n --arg n "$OM_REVIEWER_ROLE" --arg p "$policy" '{
    name: $n,
    displayName: "Lakeloom ontology reviewer (DEV)",
    description: "DEV ONLY (Lakeloom dev stack): granted to alice, not to carol.",
    policies: [$p]
  }')
  om POST /v1/roles "$body" >/dev/null
  log "created role $OM_REVIEWER_ROLE"
fi
role_id=$(om GET "/v1/roles/name/$OM_REVIEWER_ROLE" | jq -r .id)
[ -n "$policy_id" ] && [ -n "$role_id" ] || { log "reviewer role lookup failed"; exit 1; }

for u in $OM_REVIEWER_USERS; do
  user=$(om GET "/v1/users/name/$u?fields=roles")
  if printf '%s' "$user" | jq -e --arg r "$role_id" '[.roles[]?.id] | index($r)' >/dev/null; then
    log "user $u already has role $OM_REVIEWER_ROLE"
  else
    uid=$(printf '%s' "$user" | jq -r .id)
    if printf '%s' "$user" | jq -e '.roles | length > 0' >/dev/null 2>&1; then
      patch=$(jq -n --arg r "$role_id" '[{op: "add", path: "/roles/-", value: {id: $r, type: "role"}}]')
    else
      patch=$(jq -n --arg r "$role_id" '[{op: "add", path: "/roles", value: [{id: $r, type: "role"}]}]')
    fi
    om PATCH "/v1/users/$uid" "$patch" application/json-patch+json >/dev/null
    log "granted role $OM_REVIEWER_ROLE to $u"
  fi
done

# --- 4. bot token ---------------------------------------------------------------------------
bot_user_id=$(om GET "/v1/bots/name/$OM_BOT_NAME" | jq -r '.botUser.id // empty')
[ -n "$bot_user_id" ] || { log "bot $OM_BOT_NAME not found"; exit 1; }

bot_token=$(om GET "/v1/users/auth-mechanism/$bot_user_id" | jq -r '.config.JWTToken // empty')
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
