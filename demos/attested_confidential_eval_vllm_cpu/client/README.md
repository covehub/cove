# Cove Attested Eval Client

Local UI for chatting with the RA-TLS vLLM endpoint and checking the Cove
certificate chain.

```bash
cd demos/attested_confidential_eval_vllm_cpu/client
PORT=5177 \
COVE_DEMO_ENDPOINT=https://c7a3d3893fefc09cb82cf6d8753d943808bdb60e-18443s.dstack-pha-prod5.phala.network \
COVE_WORKFLOW_ID=attested_confidential_eval_vllm \
node server.mjs
```

Open `http://127.0.0.1:5177`.

For a freshly published CPU workflow, set:

```bash
COVE_WORKFLOW_ID=attested_confidential_eval_vllm_cpu
```
