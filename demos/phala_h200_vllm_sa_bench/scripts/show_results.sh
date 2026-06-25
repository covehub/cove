#!/usr/bin/env bash
set -euo pipefail

CVM_NAME="${CVM_NAME:-gpu-tee-45vfi}"
SERVICE_NAME="${SERVICE_NAME:-}"

echo "Recent benchmark logs:"
if [[ -n "${SERVICE_NAME}" ]]; then
    npx --yes phala logs --cvm-id "${CVM_NAME}" "${SERVICE_NAME}" -n "${LOG_LINES:-240}" || true
else
    if ! npx --yes phala logs --cvm-id "${CVM_NAME}" dstack-h200-bench-1 -n "${LOG_LINES:-240}"; then
        npx --yes phala logs --cvm-id "${CVM_NAME}" h200-bench -n "${LOG_LINES:-240}" || true
    fi
fi

echo
echo "Current CVM endpoints:"
INFO_JSON="$(npx --yes phala cvms get "${CVM_NAME}" --json)"
export INFO_JSON
python3 - <<'PY'
import json
import os

payload = json.loads(os.environ["INFO_JSON"])

seen = set()

def print_result_urls(name: str, url: str) -> None:
    if not url or url in seen:
        return
    seen.add(url)
    print(f"{name}: {url}")
    print(f"  health: {url.rstrip('/')}/healthz")
    print(f"  rollup: {url.rstrip('/')}/results/benchmark-rollup.json")
    print(f"  csv:    {url.rstrip('/')}/results/benchmark-rollup.csv")

for endpoint in payload.get("endpoints") or []:
    for name, url in endpoint.items():
        print_result_urls(name, url)

base_domain = (payload.get("gateway") or {}).get("base_domain")
if base_domain:
    app_id = payload.get("app_id")
    instance_id = payload.get("instance_id")
    if app_id:
        print_result_urls("app:8080", f"https://{app_id}-8080.{base_domain}")
    if instance_id:
        print_result_urls("instance:8080", f"https://{instance_id}-8080.{base_domain}")
PY
