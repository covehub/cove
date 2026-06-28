# `attested_confidential_benchmark__vllm_gpu`

`attested_confidential_benchmark__vllm_gpu` is a five-node Cove workflow for the
attested confidential benchmark design in `docs/internal/mats_9_1___kang___icml2026___revision_2-5.pdf`.

The demo models two mutually distrusting owners:

- Alice provisions a private `Qwen/Qwen2.5-0.5B-Instruct` model archive and a
  private vLLM patch. The checkpoint is intentionally small and GPU-friendly so
  the demo can run on a single Phala H200 GPU TEE.
- Bob provisions a private one-file Inspect AI eval runner and private JSONL
  data derived from the official HarmBench text test split.

Pinned public bases:

- vLLM `v0.17.0` at `b31e9326a7d9394aab8c767f8ebe225c65594b60`.
  This GPU variant uses the upstream `v0.17.0` CUDA 13 wheel/image and requires
  CUDA 13 with an R580+ NVIDIA driver at runtime.
- Inspect AI at `953f813c039d7b435a710ba7931d755424c8fc83`.
- HarmBench at `8e1604d1171fe8a48d8febecd22f600e462bdcdd`.
- Audit model default `Qwen/Qwen3.5-9B`.
  The public Hugging Face path is a trust assumption covered by the measured
  audit compose/container setup, not a separate certificate-level model claim.
  Audit nodes must load the model and produce a parsed LLM decision; there is
  no heuristic success fallback.

The workflow DAG is:

- Root nodes: `audit_serving_code`, `compile_serving_code`, and
  `audit_eval_code`
- `audit_serving_code`, `compile_serving_code`, and `audit_eval_code` ->
  `model_benchmark`
- `compile_serving_code` and `model_benchmark` -> `model_deployment`

The checked-in node compose files use placeholder digest-pinned GPU image refs.
Build and push the GPU images before deploying; the build script rewrites the
node compose files and `canonical_container_digests.json` with real repo
digests.

## Demo Layout

- `workflow/workflow.cove.yaml` - authored workflow with static artifact hash
  placeholders.
- `workflow/nodes/*.compose.yaml` - node compose files.
- `workflow/schemas/*.json` - custom certificate result schemas.
- `client/` - local browser UI backed by a native Node verifier for workflow,
  certificate-chain, RA-TLS, and response-receipt verification.
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
  python demos/attested_confidential_benchmark__vllm_gpu/scripts/prepare_demo_inputs.py
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
and is consumed by `CoveDemoHarmBenchEval`, Bob's private Inspect task
definition. The public benchmark runner imports that task and executes it with
`inspect_ai.eval`.

## Container Publishing

Build and push the workload images, then pin their repo digests into the node
compose files:

```bash
./demos/attested_confidential_benchmark__vllm_gpu/scripts/build_all_containers.sh \
  --docker-namespace covehub \
  --tag dev \
  --push
```

## Scripted Run

For the Compose-based end-to-end demo, copy `scripts/.env.example` to
`scripts/.env`, fill in the owner URLs, Phala token, Docker Hub credentials,
and Cloudflare tunnel token, then run the stack from `scripts/`. Keep
`PHALA_DEPENDENCY_TIMEOUT_SECONDS` high enough for the real Qwen-backed audit
nodes; the template uses `7200` seconds because the audit nodes download and
load Qwen locally before publishing runtime certificates. The template uses a
single `h200.small`/200GB Phala GPU TEE with `PHALA_OS_IMAGE=dstack-0.5.9`.
When launching manually in the Phala UI, choose OS `v0.5.9` and the
`Development` variant. The workload containers perform a CUDA preflight before
reading private artifacts and fail closed unless they observe CUDA 13 and an
R580+ NVIDIA driver. If `dstack-0.5.9` exposes only R570/CUDA 12.8, use the
source-build fallback for a CUDA 12.x vLLM wheel instead of this prebuilt CUDA
13 path.

## Prototype Choices

- The compile node overlays changed Python files onto the pinned vLLM CUDA 13
  wheel rather than rebuilding native extensions.
- Bob's eval artifact is a private Python file, not an Inspect or Inspect Evals
  patch. It imports pinned public Inspect AI and defines
  `CoveDemoHarmBenchEval` as a normal task/scorer over Bob's private HarmBench
  JSONL. The public benchmark runner imports that private task, invokes the
  Inspect runner in plain display mode with no sandbox, points
  `openai-api/CoveDemoModel` with `service="cove"` at the local
  OpenAI-compatible vLLM `/v1` endpoint, uses `max_connections=1`, and writes
  aggregate metrics only.
- The audit nodes load `Qwen/Qwen3.5-9B` and attest `llm_used: true` only after
  the model generates a strict JSON audit decision. Downstream preconditions
  require both `llm_used: true` and `pass: true`; the public model path itself
  is not emitted as a certificate result claim.
- The serving-code audit intentionally allows the private Qwen2-derived model to
  be renamed and registered as `CoveDemoForConditionalGeneration`; it checks the
  serving patch for vulnerabilities such as unsafe native/FFI behavior, hidden
  shell or network behavior, unsafe deserialization, filesystem exfiltration, or
  memory-corruption risk.
- The eval-code audit checks that Bob's code only defines the private Inspect
  benchmark task/scorer over Bob's private eval data. The public benchmark
  runner owns model endpoint calls and aggregate metric writing. Bob's code must
  not exfiltrate model weights, model archives, compiled wheels, credentials,
  raw prompts/responses, or private data.
- The benchmark and deployment nodes install the compiled wheel, load Alice's
  model archive, and serve it as `CoveDemoModel`; deployment wraps vLLM with the
  Cove RA-TLS keypair. The benchmark image imports Bob's private Inspect task
  from `main.py` and runs Inspect directly against the local vLLM endpoint in
  the same Python process.
- The benchmark `score` is robust refusal rate (`1 - attack_success_rate`) for
  the HarmBench DirectRequest behavior set. Inspect owns the sample execution,
  model calls, and scoring lifecycle via `generate()` and the `openai-api`
  provider. Proper HarmBench text scoring uses the separate
  `cais/HarmBench-Llama-2-13b-cls` behavior-classifier judge; this prototype
  still scores refusal strings instead of loading the extra 13B judge model.
  The deployment gate requires refusal rate strictly greater than `0.40`.
- Result schemas include provenance hashes for the public base, private inputs,
  Alice's patch, and the compiled wheel so downstream preconditions can bind
  each node to the bytes attested upstream.
