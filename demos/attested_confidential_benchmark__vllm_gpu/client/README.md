# Cove Attested Benchmark Client

Local UI for chatting with the RA-TLS vLLM endpoint and checking the Cove
workflow/certificate chain. The local Node backend verifies the signed workflow
bundle, embedded dependency certificates, Phala quote report data, RTMR3 compose
binding, live TLS DER, and per-response `cove_receipt` before chat output is
shown.

```bash
cd demos/attested_confidential_benchmark__vllm_gpu/client
PORT=5177 \
COVE_DEMO_ENDPOINT=https://c7a3d3893fefc09cb82cf6d8753d943808bdb60e-18443s.dstack-pha-prod5.phala.network \
COVE_PUBLISHER=orion-demo-carol.covehub.io \
COVE_API_BASE=https://orion-api.covehub.io \
COVE_WORKFLOW_ID=attested_confidential_benchmark__vllm_gpu \
node server.mjs
```

Open `http://127.0.0.1:5177`.

Use the **Verify** button before chatting. Chat requests require the recent
verification id returned by `/api/verify`; direct `curl -k` calls to the model
endpoint are only reachability debugging and do not perform Cove verification.

For a freshly published GPU workflow, set the publisher, workflow ID, and
endpoint for that deployment. `COVE_API_BASE` defaults to
`https://api.covehub.io`; use `https://orion-api.covehub.io` for Orion
development deployments.

```bash
COVE_PUBLISHER=orion-demo-carol.covehub.io
COVE_WORKFLOW_ID=attested_confidential_benchmark__vllm_gpu
COVE_API_BASE=https://orion-api.covehub.io
```
