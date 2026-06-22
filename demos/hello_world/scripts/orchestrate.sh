#!/bin/sh
set -eu

ROLE="${1:?usage: orchestrate.sh alice|bob|carol}"
DEMO_ROOT=/workspace/demos/hello_world
COORDINATION_DIR=/coordination
WORKFLOW_REF_ID=hello_world

stage() {
  printf '\n===== STAGE: %s =====\n' "$1"
}

mark() {
  stage "complete: $1"
  date -u +"%Y-%m-%dT%H:%M:%SZ" > "${COORDINATION_DIR}/$1.done"
}

wait_for() {
  stage "waiting for: $1"
  while [ ! -f "${COORDINATION_DIR}/$1.done" ]; do
    sleep 2
  done
}

require_env() {
  name="$1"
  eval "value=\${$name:-}"
  if [ -z "$value" ]; then
    echo "missing required environment variable: ${name}" >&2
    exit 1
  fi
}

wait_url() {
  label="$1"
  url="$2"
  stage "waiting for ${label}: ${url}"
  attempts=0
  while [ "$attempts" -lt "${WAIT_ATTEMPTS:-180}" ]; do
    if python - "$url" <<'PY'
import sys
import urllib.request

request = urllib.request.Request(
    sys.argv[1],
    headers={"User-Agent": "cove-runtime/0.0.1"},
)
with urllib.request.urlopen(request, timeout=5) as response:
    if response.status >= 400:
        raise SystemExit(1)
PY
    then
      return 0
    fi
    attempts=$((attempts + 1))
    sleep 2
  done
  echo "timed out waiting for ${label}: ${url}" >&2
  exit 1
}

domain_from_url() {
  python - "$1" <<'PY'
import sys
from urllib.parse import urlparse

print(urlparse(sys.argv[1]).netloc)
PY
}

init_owner() {
  cove_home="$1"
  owner_url="$2"
  stage "${ROLE}: cove init"
  rm -rf "$cove_home"
  mkdir -p "$cove_home"
  printf '\n%s\n%s\n\n' "$COVEHUB_API_URL" "$owner_url" |
    cove --cove-home "$cove_home" init
}

init_publisher() {
  cove_home="$1"
  stage "carol: cove init with Phala and Docker Hub credentials"
  rm -rf "$cove_home"
  mkdir -p "$cove_home"
  printf '\n%s\n%s\n%s\n%s\n%s\n%s\n' \
    "$COVEHUB_API_URL" \
    "$CAROL_URL" \
    "$PHALA_CLOUD_API_KEY" \
    "$DOCKERHUB_USERNAME" \
    "$DOCKERHUB_API_KEY" \
    "${DOCKERHUB_REGISTRY:-}" |
    cove --cove-home "$cove_home" init
}

start_owner_server() {
  cove_home="$1"
  public_url="$2"
  ready_stage="$3"
  stage "${ROLE}: start owner provisioning server"
  cove --cove-home "$cove_home" start 9000 &
  SERVER_PID="$!"
  trap 'kill "$SERVER_PID" 2>/dev/null || true' EXIT INT TERM
  wait_url "${ROLE} public identity" "${public_url%/}/identity"
  mark "$ready_stage"
}

approve_workflow() {
  cove_home="$1"
  carol_domain="$(domain_from_url "$CAROL_URL")"
  stage "${ROLE}: inspect and approve ${carol_domain}/${WORKFLOW_REF_ID}"
  while :; do echo y; done |
    cove --cove-home "$cove_home" provision inspect "${carol_domain}/${WORKFLOW_REF_ID}"
}

prepare_workflow_copy() {
  stage "carol: prepare workflow copy with .env owner URLs"
  rm -rf /tmp/hello-world-workflow
  mkdir -p /tmp/hello-world-workflow
  cp -a "${DEMO_ROOT}/workflow/." /tmp/hello-world-workflow/
  python - <<'PY'
import os
from pathlib import Path

import yaml

workflow_path = Path("/tmp/hello-world-workflow/workflow.cove.yaml")
payload = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
payload["owners"] = {
    "alice": os.environ["ALICE_URL"],
    "bob": os.environ["BOB_URL"],
}
workflow_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
PY
}

write_client_proxy_remote_url() {
  deploy_json="$1"
  node_id="final_server"
  remote_url="$(python - "$deploy_json" "$node_id" <<'PY'
import json
import sys
from pathlib import Path

PHALA_GATEWAY_BASE_DOMAIN = "dstack-pha-prod5.phala.network"

deploy_json, node_id = sys.argv[1:]
payload = json.loads(Path(deploy_json).read_text(encoding="utf-8"))
deployments = payload.get("deployments")
if not isinstance(deployments, list):
    raise SystemExit("deploy JSON is missing deployments")
deployment = next(
    (
        item for item in deployments
        if isinstance(item, dict) and item.get("node_id") == node_id
    ),
    None,
)
if deployment is None:
    raise SystemExit(f"could not find deployment node {node_id!r} in deploy JSON")
app_id = deployment.get("app_id")
if not isinstance(app_id, str) or not app_id:
    raise SystemExit(f"deploy JSON is missing app_id for node {node_id!r}")
print(f"https://{app_id}-18443s.{PHALA_GATEWAY_BASE_DOMAIN}")
PY
)"
  stage "carol: client proxy remote endpoint ${remote_url}"
  printf '%s\n' "$remote_url" > "${COORDINATION_DIR}/client-proxy-remote-url"
}

run_alice() {
  require_env ALICE_URL
  require_env CAROL_URL
  require_env COVEHUB_API_URL

  cove_home=/cove-homes/alice
  init_owner "$cove_home" "$ALICE_URL"
  start_owner_server "$cove_home" "$ALICE_URL" alice-server-ready

  wait_for covehub-public
  stage "alice: provision alice_secret_word"
  cove --cove-home "$cove_home" provision --overwrite \
    alice_secret_word "${DEMO_ROOT}/fixtures/alice_secret_word.txt"
  mark alice-provisioned

  wait_for carol-pushed
  approve_workflow "$cove_home"
  mark alice-approved

  stage "alice: provisioning server remains online"
  wait "$SERVER_PID"
}

run_bob() {
  require_env BOB_URL
  require_env CAROL_URL
  require_env COVEHUB_API_URL

  cove_home=/cove-homes/bob
  init_owner "$cove_home" "$BOB_URL"
  start_owner_server "$cove_home" "$BOB_URL" bob-server-ready

  wait_for covehub-public
  stage "bob: provision bob_secret_word"
  cove --cove-home "$cove_home" provision --overwrite \
    bob_secret_word "${DEMO_ROOT}/fixtures/bob_secret_word.txt"
  mark bob-provisioned

  wait_for carol-pushed
  approve_workflow "$cove_home"
  mark bob-approved

  stage "bob: provisioning server remains online"
  wait "$SERVER_PID"
}

run_carol() {
  require_env ALICE_URL
  require_env BOB_URL
  require_env CAROL_URL
  require_env COVEHUB_API_URL
  require_env PHALA_CLOUD_API_KEY
  require_env DOCKERHUB_USERNAME
  require_env DOCKERHUB_API_KEY

  cove_home=/cove-homes/carol
  init_publisher "$cove_home"
  start_owner_server "$cove_home" "$CAROL_URL" carol-server-ready

  wait_url "covehub public API" "${COVEHUB_API_URL%/}/healthz"
  mark covehub-public

  wait_for alice-provisioned
  wait_for bob-provisioned

  prepare_workflow_copy
  stage "carol: check workflow"
  cove --cove-home "$cove_home" check /tmp/hello-world-workflow/workflow.cove.yaml
  stage "carol: compile workflow"
  cove --cove-home "$cove_home" compile /tmp/hello-world-workflow/workflow.cove.yaml
  stage "carol: push workflow"
  cove --cove-home "$cove_home" push --overwrite /tmp/hello-world-workflow/workflow.cove.yaml
  mark carol-pushed

  wait_for alice-approved
  wait_for bob-approved

  carol_domain="$(domain_from_url "$CAROL_URL")"
  stage "carol: deploy ${carol_domain}/${WORKFLOW_REF_ID} on Phala"
  deploy_json="${COORDINATION_DIR}/carol-deploy.json"
  deploy_err="${COORDINATION_DIR}/carol-deploy.stderr"
  if cove --cove-home "$cove_home" deploy "${carol_domain}/${WORKFLOW_REF_ID}" \
      --phala-instance-type "${PHALA_INSTANCE_TYPE:-tdx.medium}" \
      --phala-disk-size-gb "${PHALA_DISK_SIZE_GB:-40}" \
      --phala-public-logs \
      --phala-public-sysinfo > "$deploy_json" 2> "$deploy_err"; then
    cat "$deploy_json"
    if [ -s "$deploy_err" ]; then
      cat "$deploy_err" >&2
    fi
  else
    cat "$deploy_json" >&2 || true
    cat "$deploy_err" >&2 || true
    exit 1
  fi
  write_client_proxy_remote_url "$deploy_json"
  mark carol-deployed

  stage "carol: publisher server remains online"
  wait "$SERVER_PID"
}

case "$ROLE" in
  alice)
    run_alice
    ;;
  bob)
    run_bob
    ;;
  carol)
    run_carol
    ;;
  *)
    echo "unknown role: ${ROLE}" >&2
    exit 2
    ;;
esac
