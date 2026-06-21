# `attested_confidential_eval_vllm_cpu`

`attested_confidential_eval_vllm_cpu` is a five-node Cove workflow for the
attested confidential eval design in `docs/internal/mats_9_1___kang___icml2026___revision_2-5.pdf`.

The demo models two mutually distrusting owners:

- Alice provisions a private `Qwen/Qwen2.5-0.5B-Instruct` model archive and a
  private vLLM patch. The checkpoint is intentionally small and CPU-friendly so
  the demo can run on Phala TDX without GPU Triton kernels.
- Bob provisions a private one-file Inspect eval runner and private JSONL eval
  data.

Pinned public bases:

- vLLM `v0.17.0` at `b31e9326a7d9394aab8c767f8ebe225c65594b60`.
- Inspect AI at `953f813c039d7b435a710ba7931d755424c8fc83`.
- Audit model default `Qwen/Qwen3.5-9B`.

The workflow DAG is:

- `audit_serving_code`
- `compile_serving_code`
- `audit_eval_code`
- `model_benchmark`
- `model_deployment`

The checked-in node compose files use digest-pinned workload images from the
working CPU demo run. Rebuild and push the images only when changing container
code.

## Demo Layout

- `workflow/workflow.cove.yaml` - authored workflow with static artifact hash
  placeholders.
- `workflow/nodes/*.compose.yaml` - node compose files.
- `workflow/schemas/*.json` - custom certificate result schemas.
- `client/` - local browser UI for chat plus certificate-chain verification.
- `containers/` - workload container definitions for the five-node workflow.
- `containers/common/` - helper code copied into workload images.
- `scripts/prepare_demo_inputs.py` - prepares Alice and Bob private artifacts.
- `scripts/render_workflow.py` - refreshes workflow static artifact hashes.
- `scripts/build_all_containers.sh` - builds/pushes workload images and pins
  pushed repo digests into node compose files.

## Input Preparation

Prepare the demo inputs locally:

```bash
uv run --with huggingface_hub --with pyyaml \
  python demos/attested_confidential_eval_vllm_cpu/scripts/prepare_demo_inputs.py
```

This creates:

- `runtime_inputs/alice_private_model.tar`
- `runtime_inputs/alice_private_serving_patch.diff`
- `runtime_inputs/bob_private_eval_code.py`
- `runtime_inputs/bob_private_eval_data.jsonl`

The model archive rewrites `config.json` to use
`CoveDemoForConditionalGeneration`. The private vLLM patch adds that
architecture to a pristine vLLM checkout while reusing the Qwen2-family weight
loading path under CoveDemo class names.

## Container Publishing

Build and push the workload images, then pin their repo digests into the node
compose files:

```bash
./demos/attested_confidential_eval_vllm_cpu/scripts/build_all_containers.sh \
  --docker-namespace hpmv \
  --tag dev \
  --push
```

## Prototype Choices

- The compile node overlays changed Python files onto the pinned vLLM CPU wheel
  rather than rebuilding native extensions.
- Bob's eval artifact is a private Python file, not an Inspect or Inspect Evals
  patch. It imports pinned public Inspect AI and writes aggregate metrics only.
- The benchmark and deployment nodes install the compiled wheel, load Alice's
  model archive, and serve it as `CoveDemoModel`; deployment wraps vLLM with the
  Cove RA-TLS keypair.
- The sample benchmark threshold is `0.75`, matching three successful aggregate
  safety/utility checks out of the four private JSONL prompts.
- Result schemas include provenance hashes for the public base, private inputs,
  Alice's patch, and the compiled wheel so downstream preconditions can bind
  each node to the bytes attested upstream.
