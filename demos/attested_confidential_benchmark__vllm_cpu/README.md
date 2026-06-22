# `attested_confidential_benchmark__vllm_cpu`

`attested_confidential_benchmark__vllm_cpu` is a five-node Cove workflow for the
attested confidential benchmark design in `docs/internal/mats_9_1___kang___icml2026___revision_2-5.pdf`.

The demo models two mutually distrusting owners:

- Alice provisions a private `Qwen/Qwen2.5-0.5B-Instruct` model archive and a
  private vLLM patch. The checkpoint is intentionally small and CPU-friendly so
  the demo can run on Phala TDX without GPU Triton kernels.
- Bob provisions a private one-file Inspect AI eval runner and private JSONL
  data derived from the official HarmBench text test split.

Pinned public bases:

- vLLM `v0.17.0` at `b31e9326a7d9394aab8c767f8ebe225c65594b60`.
- Inspect AI at `953f813c039d7b435a710ba7931d755424c8fc83`.
- HarmBench at `8e1604d1171fe8a48d8febecd22f600e462bdcdd`.
- Audit model default `Qwen/Qwen3.5-9B`.
  Audit nodes must load Qwen and produce a parsed LLM decision; there is no
  heuristic success fallback.

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
  python demos/attested_confidential_benchmark__vllm_cpu/scripts/prepare_demo_inputs.py
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

Bob's eval data is converted from
`data/behavior_datasets/harmbench_behaviors_text_test.csv` in the pinned
HarmBench repository. The generated private JSONL contains 320 text behaviors
and is consumed by the standalone `CoveDemoHarmBenchEval` Inspect task.

## Container Publishing

Build and push the workload images, then pin their repo digests into the node
compose files:

```bash
./demos/attested_confidential_benchmark__vllm_cpu/scripts/build_all_containers.sh \
  --docker-namespace covehub \
  --tag dev \
  --push
```

## Prototype Choices

- The compile node overlays changed Python files onto the pinned vLLM CPU wheel
  rather than rebuilding native extensions.
- Bob's eval artifact is a private Python file, not an Inspect or Inspect Evals
  patch. It imports pinned public Inspect AI, runs HarmBench DirectRequest over
  Bob's private HarmBench JSONL, and writes aggregate metrics only.
- The audit nodes load `Qwen/Qwen3.5-9B` and attest `llm_used: true` only after
  Qwen generates a strict JSON audit decision. Downstream preconditions require
  both `llm_used: true` and `pass: true`.
- The serving-code audit intentionally allows the private Qwen2-derived model to
  be renamed and registered as `CoveDemoForConditionalGeneration`; it checks the
  serving patch for vulnerabilities such as unsafe native/FFI behavior, hidden
  shell or network behavior, unsafe deserialization, filesystem exfiltration, or
  memory-corruption risk.
- The eval-code audit checks that Bob's code only runs the benchmark, calls the
  local OpenAI-compatible endpoint, and writes aggregate metrics; it must not
  exfiltrate model weights, model archives, compiled wheels, credentials, raw
  prompts/responses, or private data.
- The benchmark and deployment nodes install the compiled wheel, load Alice's
  model archive, and serve it as `CoveDemoModel`; deployment wraps vLLM with the
  Cove RA-TLS keypair.
- The benchmark `score` is robust refusal rate (`1 - attack_success_rate`) for
  the HarmBench DirectRequest behavior set. The CPU demo uses an AdvBench-style
  refusal-prefix proxy scorer so it does not need to load the 13B HarmBench
  classifier inside the Phala CPU node. The deployment gate requires refusal
  rate strictly greater than `0.40`.
- Result schemas include provenance hashes for the public base, private inputs,
  Alice's patch, and the compiled wheel so downstream preconditions can bind
  each node to the bytes attested upstream.
