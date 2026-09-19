#!/usr/bin/env bash
# Smoke-test the production stack (docker-compose.prod.yml) end to end.
#
# Builds the image, starts the whole stack with throwaway secrets, and checks
# the properties the production file exists to guarantee - without a language
# model, so it runs in CI where there is no Ollama. Embedding and answering
# are covered by the fake-based unit tests and by docs/demo.md.
#
# Safe to run on a machine that already runs ragbridge: it uses its own
# Compose project name, its own env file, and alternate host ports, and it
# removes everything it created (volumes included) when it finishes.
#
#   scripts/smoke-prod.sh
#
# Exits non-zero if any check fails.

set -uo pipefail
cd "$(dirname "$0")/.."

PROJECT=rbsmoke
ENV_FILE=.env.smoke
HTTP_PORT="${SMOKE_HTTP_PORT:-18080}"
HTTPS_PORT="${SMOKE_HTTPS_PORT:-18443}"
BASE="https://localhost:${HTTPS_PORT}"

dc() {
  docker compose -p "$PROJECT" --env-file "$ENV_FILE" -f docker-compose.prod.yml "$@"
}
export APP_ENV_FILE="$ENV_FILE"

passed=0
failed=0
ok()   { echo "  PASS  $1"; passed=$((passed + 1)); }
bad()  { echo "  FAIL  $1"; failed=$((failed + 1)); }

# expect_status WANT DESCRIPTION CURL_ARGS...   (-k: Caddy's local CA is untrusted)
expect_status() {
  local want=$1 desc=$2 got
  shift 2
  got=$(curl -sk -m 30 -o /dev/null -w '%{http_code}' "$@")
  if [ "$got" = "$want" ]; then ok "$desc"; else bad "$desc (want $want, got $got)"; fi
}

cleanup() {
  if [ "$failed" -gt 0 ]; then
    echo; echo "--- container logs (last lines) ---"
    dc logs --tail 40 2>&1 || true
  fi
  dc down -v --remove-orphans >/dev/null 2>&1 || true
  rm -f "$ENV_FILE"
}
trap cleanup EXIT

# Throwaway secrets. Hex, because they go inside URLs.
cat > "$ENV_FILE" <<EOF
POSTGRES_PASSWORD=$(openssl rand -hex 24)
REDIS_PASSWORD=$(openssl rand -hex 24)
SITE_ADDRESS=localhost
HTTP_PORT=${HTTP_PORT}
HTTPS_PORT=${HTTPS_PORT}
EOF

echo "== Refuses to start without its secrets"
sed '/^REDIS_PASSWORD=/d' "$ENV_FILE" > "$ENV_FILE.partial"
if docker compose -p "${PROJECT}x" --env-file "$ENV_FILE.partial" -f docker-compose.prod.yml config >/dev/null 2>&1; then
  bad "compose accepted a missing REDIS_PASSWORD"
else
  ok "compose refuses a missing REDIS_PASSWORD"
fi
rm -f "$ENV_FILE.partial"

echo "== Builds and starts"
if dc up -d --build --wait --wait-timeout 300 >/dev/null 2>&1; then
  ok "stack builds and every service becomes healthy"
else
  bad "stack failed to build or become healthy"
  dc ps -a 2>&1 | sed 's/^/        /'
fi

migrate_exit=$(docker inspect "${PROJECT}-migrate-1" --format '{{.State.ExitCode}}' 2>/dev/null || echo missing)
if [ "$migrate_exit" = "0" ]; then ok "migrations ran once and exited 0"; else bad "migrate exit code: $migrate_exit"; fi

echo "== Public surface"
expect_status 200 "GET /health/ready through Caddy over HTTPS" "$BASE/health/ready"
expect_status 404 "/docs is off"                               "$BASE/docs"
expect_status 404 "/openapi.json is off"                       "$BASE/openapi.json"
expect_status 404 "/playground is off"                          "$BASE/playground/"
expect_status 401 "GET /documents without a key"               "$BASE/documents"
expect_status 401 "POST /query without a key"   -X POST -H 'content-type: application/json' -d '{"question":"x"}' "$BASE/query"
expect_status 401 "POST /search without a key"  -X POST -H 'content-type: application/json' -d '{"query":"x"}'    "$BASE/search"
expect_status 401 "POST /agent without a key"   -X POST -H 'content-type: application/json' -d '{"question":"x"}' "$BASE/agent"
expect_status 401 "POST /mcp without a token"   -X POST -H 'content-type: application/json' -d '{}'              "$BASE/mcp"

echo "== Authenticated path (no model needed)"
KEY=$(dc exec -T app ragbridge-admin create-tenant --name smoke 2>/dev/null | sed -n 's/^API key: //p')
if [ -n "$KEY" ]; then ok "ragbridge-admin created a tenant key inside the container"; else bad "could not create a key"; fi
body=$(curl -sk -m 30 -H "Authorization: Bearer $KEY" "$BASE/documents")
if [ "$body" = "[]" ]; then ok "GET /documents with the key returns []"; else bad "GET /documents returned: $body"; fi
expect_status 401 "a wrong key is rejected" -H "Authorization: Bearer rb_definitelynotakey" "$BASE/documents"

echo "== Protections"
head -c 20000000 /dev/zero | tr '\0' 'a' > "$ENV_FILE.huge"
# The app also enforces MAX_UPLOAD_SIZE, and answers 413 with a JSON body - but
# only after reading the whole request into memory. The proxy limit exists so
# that never happens, so require the *proxy's* refusal: a 413 with no JSON
# body. (Checking the status alone passes even with the proxy limit removed.)
huge_out=$(curl -sk -m 60 -w '\n%{http_code}' -X POST -H "Authorization: Bearer $KEY" \
  -F "file=@$ENV_FILE.huge;type=text/plain" "$BASE/documents")
huge_status=${huge_out##*$'\n'}
huge_body=${huge_out%$'\n'*}
if [ "$huge_status" = "413" ] && ! echo "$huge_body" | grep -q '"detail"'; then
  ok "a 20 MB upload is refused by the proxy, before the app reads it"
elif [ "$huge_status" = "413" ]; then
  bad "a 20 MB upload reached the app (it answered 413 itself): the proxy limit is not working"
else
  bad "a 20 MB upload got HTTP $huge_status, expected 413"
fi
rm -f "$ENV_FILE.huge"

redis_out=$(dc exec -T -e REDISCLI_AUTH=wrong redis redis-cli ping 2>&1 || true)
if echo "$redis_out" | grep -q PONG; then bad "Redis answered with a wrong password"; else ok "Redis rejects a wrong password"; fi

published=""
for svc in postgres redis migrate app worker; do
  ports=$(docker inspect "${PROJECT}-${svc}-1" --format '{{range $k,$v := .NetworkSettings.Ports}}{{if $v}}{{$k}} {{end}}{{end}}' 2>/dev/null)
  [ -n "$ports" ] && published="$published $svc"
done
if [ -z "$published" ]; then ok "only Caddy publishes host ports"; else bad "these also publish ports:$published"; fi

for svc in app worker; do
  uid=$(docker exec "${PROJECT}-${svc}-1" id -u 2>/dev/null || echo unknown)
  if [ "$uid" = "10001" ]; then ok "$svc runs as an unprivileged user"; else bad "$svc runs as uid $uid"; fi
done

leaked=$(docker exec "${PROJECT}-app-1" sh -c 'ls -A /app | grep -E "^(CLAUDE|AGENTS|\.cursor|\.env|evaluation)" || true')
if [ -z "$leaked" ]; then ok "no private or dev files inside the image"; else bad "image contains: $leaked"; fi

echo "== Production mode refuses the published database credentials"
if docker run --rm -e ENVIRONMENT=production \
     -e DATABASE_URL='postgresql+psycopg://ragbridge:ragbridge@postgres:5432/ragbridge' \
     "${PROJECT}-app" python -c "from ragbridge.main import app" >/dev/null 2>&1; then
  bad "the app started with the shipped credentials in production"
else
  ok "the app refuses to start with the shipped credentials"
fi

echo
echo "$passed passed, $failed failed"
[ "$failed" -eq 0 ]
