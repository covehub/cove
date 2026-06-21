# `attested_confidential_eval`

`attested_confidential_eval` is a five-node Cove workflow based on the attested model
audit example from `139_Cove_Compositional_Multi_P.pdf`.

The workflow models two mutually distrusting owners:

- Alice provisions a private model archive and a private vLLM serving patch.
- Bob provisions private evaluation code and private evaluation data.

The checked-in node compose files remain pinned to the previously published
`cove-demo-attested-audit-*` image digests so the demo keeps resolving until
the renamed `cove-demo-attested-confidential-eval-*` workload images are built
and pushed.

The workflow DAG is:

- `audit_serving_code`
- `compile_serving_code`
- `audit_eval_code`
- `model_benchmark`
- `model_deployment`

The implementation is intentionally CPU-first:

- the audit nodes use a small public audit model on CPU,
- the serving stack targets vLLM CPU wheels,
- the compile node performs an incremental Python-file overlay on top of a
  prebuilt native vLLM CPU wheel instead of rebuilding native extensions.

## Demo Layout

- `workflow/workflow.cove.yaml` — authored workflow with placeholder hashes.
- `workflow/nodes/*.compose.yaml` — authored node composes.
- `workflow/schemas/*.json` — custom certificate result schemas.
- `containers/` — workload containers for the five-node workflow.
- `scripts/build_all_containers.sh` — build/push helper that pins workload
  image digests into the node compose files.
- `scripts/prepare_demo_inputs.py` — prepares Alice and Bob input artifacts.
- `scripts/render_workflow.py` — rewrites static artifact hashes in
  `workflow/workflow.cove.yaml` from prepared local files.

## Input Preparation

Prepare the demo inputs locally:

```bash
uv run --with datasets --with huggingface_hub --with pyyaml \
  python demos/attested_confidential_eval/scripts/prepare_demo_inputs.py
```

This creates:

- `runtime_inputs/alice_private_model.tar`
- `runtime_inputs/alice_private_serving_patch.diff`
- `runtime_inputs/bob_private_eval_code.py`
- `runtime_inputs/bob_private_eval_data.jsonl`

The script also refreshes `workflow/workflow.cove.yaml` with the correct
`plaintext_hash` values for those prepared files.

## Container Publishing

Build and push the workload images, then pin their repo digests into the
compose files:

```bash
./demos/attested_confidential_eval/scripts/build_all_containers.sh \
  --docker-namespace hpmv \
  --tag dev \
  --push
```

## Current Prototype Choices

- The audit nodes are illustrative. They run a small public model and fall
  back to conservative heuristics if the model output is malformed.
- Bob's evaluation code benchmarks endpoint responsiveness and non-empty
  completions over a tiny Hugging Face dataset slice. The benchmark result
  emits both `score` and `passes_threshold`, and the deployment node gates on
  `passes_threshold` because Cove's current JsonLogic subset does not support
  `>` comparisons.
- The compile node only supports serving patches that modify Python files
  inside the vLLM package tree. That is deliberate for the first CPU-native
  prototype.
