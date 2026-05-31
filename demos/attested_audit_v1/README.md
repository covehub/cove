# `attested_audit_v1`

`attested_audit_v1` is the fresh Cove demo for the attested audits use case. It
is implemented node-by-node, starting with `audit_serving_code` and
`compile_vllm`.

The first node runs a small audit model behind vLLM and a required audit-agent
runner. The runner reads the private serving patch, pristine serving source, and
eval-owner audit policy, then writes a schema-valid pass/fail audit result.

The second node applies the audited serving patch to the pristine vLLM source
and builds a dynamic compiled serving runtime bundle. Its preconditions require
the first node to pass and require the compile inputs to match the audited input
metadata in the `audit_serving_code` node certificate.

## Key Files

- `workflow/workflow.cove.yaml` - authored Cove workflow.
- `workflow/nodes/audit_serving_code.compose.yaml` - authored workload compose.
- `workflow/nodes/compile_vllm.compose.yaml` - authored compiler workload compose.
- `workflow/schemas/audit_result.v1.json` - certificate result schema.
- `workflow/schemas/compile_result.v1.json` - compiler result schema.
- `containers/vllm_server` - generic vLLM server that installs a supplied
  serving runtime bundle and model bundle.
- `containers/audit_agent_runner` - required audit-agent harness.
- `containers/vllm_compiler` - compiler that applies the serving patch and
  packages patched vLLM wheels.
- `runpod/audit-serving-code.compose.yaml` - RunPod-only harness without Cove
  sidecars.
- `runpod/compile-vllm.compose.yaml` - RunPod-only compiler harness without Cove
  sidecars.

## Artifact Notes

Small demo fixtures are checked in under `fixtures/`. Large artifacts are not:
model bundles, vLLM source tarballs, and serving runtime bundles should be
staged externally for RunPod and later published as CoveHub static artifacts for
Phala.

For the current Phala smoke path, the serving runtime bundle is split into
multiple eval-owner static artifacts plus a manifest because the current
`cove provision` implementation encrypts each artifact in one shot and cannot
handle a multi-GB payload. This split is a demo-level workaround. The proper
platform fix is first-class streaming/chunked artifact encryption, upload,
download, and verification in Cove.
