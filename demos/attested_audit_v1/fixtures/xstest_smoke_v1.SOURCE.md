# XSTest Smoke Subset

`xstest_smoke_v1.jsonl` is a 10-prompt operational smoke subset of XSTest:

- source repository: https://github.com/paul-rottger/xstest
- source file: `xstest_prompts.csv`
- paper: "XSTest: A Test Suite for Identifying Exaggerated Safety Behaviours in
  Large Language Models"

The subset contains 5 `safe` prompts and 5 `unsafe` contrast prompts. It is used
only to prove that the `run_eval` node can start the model server, send benchmark
requests, capture responses, publish `eval_responses.jsonl`, and write an
operational result certificate.

For this smoke test, `pass=true` means the eval executed and captured non-empty
model responses. It does not certify model safety quality.
