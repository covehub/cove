#!/usr/bin/env python3
from __future__ import annotations

import concurrent.futures
import json
import os
import statistics
import time
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen


def getenv_int(name: str, default: int) -> int:
    value = os.getenv(name)
    return default if value is None or value == "" else int(value)


def getenv_float(name: str, default: float) -> float:
    value = os.getenv(name)
    return default if value is None or value == "" else float(value)


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((pct / 100) * (len(ordered) - 1))))
    return ordered[index]


def post_json(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    request = Request(
        url,
        data=body,
        headers={
            "content-type": "application/json",
            "authorization": "Bearer " + os.getenv("OPENAI_API_KEY", "vllm"),
        },
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_for_health(base_url: str, timeout_seconds: int) -> None:
    health_url = base_url.rstrip("/").removesuffix("/v1") + "/health"
    deadline = time.monotonic() + timeout_seconds
    last_error = ""
    while time.monotonic() < deadline:
        try:
            request = Request(health_url, method="GET")
            with urlopen(request, timeout=5) as response:
                if 200 <= response.status < 300:
                    return
        except URLError as exc:
            last_error = str(exc)
        except Exception as exc:
            last_error = str(exc)
        time.sleep(2)
    raise RuntimeError(f"server did not become healthy at {health_url}: {last_error}")


def make_prompt(index: int) -> str:
    base = os.getenv(
        "PROMPT_TEMPLATE",
        "Write a concise security review checklist for a Python web service. Request id: {i}",
    )
    return base.format(i=index)


def run_one(index: int) -> dict[str, Any]:
    base_url = os.getenv("OPENAI_BASE_URL", "http://vllm:8000/v1").rstrip("/")
    model = os.getenv("MODEL_NAME", "perf-model")
    max_tokens = getenv_int("MAX_TOKENS", 128)
    temperature = getenv_float("TEMPERATURE", 0.0)
    timeout = getenv_float("REQUEST_TIMEOUT_SECONDS", 180.0)
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "You are a concise assistant. Answer directly.",
            },
            {"role": "user", "content": make_prompt(index)},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    started = time.perf_counter()
    error = ""
    status = "ok"
    usage: dict[str, Any] = {}
    response_chars = 0
    try:
        response = post_json(f"{base_url}/chat/completions", payload, timeout)
        usage = response.get("usage") or {}
        content = response["choices"][0]["message"].get("content") or ""
        response_chars = len(content)
    except Exception as exc:  # runtime diagnostics path
        status = "error"
        error = str(exc)
    ended = time.perf_counter()
    return {
        "index": index,
        "status": status,
        "error": error,
        "latency_seconds": ended - started,
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
        "response_chars": response_chars,
    }


def main() -> int:
    base_url = os.getenv("OPENAI_BASE_URL", "http://vllm:8000/v1")
    output_dir = Path(os.getenv("OUTPUT_DIR", "/results"))
    output_dir.mkdir(parents=True, exist_ok=True)
    total_requests = getenv_int("REQUESTS", 64)
    concurrency = getenv_int("CONCURRENCY", 8)
    warmup_requests = getenv_int("WARMUP_REQUESTS", min(4, total_requests))
    ready_timeout = getenv_int("READY_TIMEOUT_SECONDS", 1800)

    wait_for_health(base_url, ready_timeout)

    for i in range(warmup_requests):
        run_one(-1 - i)

    started = time.perf_counter()
    records: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(run_one, i) for i in range(total_requests)]
        for future in concurrent.futures.as_completed(futures):
            records.append(future.result())
    elapsed = time.perf_counter() - started
    records.sort(key=lambda record: record["index"])

    request_path = output_dir / "perf_requests.jsonl"
    with request_path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True) + "\n")

    ok_records = [record for record in records if record["status"] == "ok"]
    latencies = [float(record["latency_seconds"]) for record in ok_records]
    completion_tokens = sum(int(record["completion_tokens"]) for record in ok_records)
    total_tokens = sum(int(record["total_tokens"]) for record in ok_records)
    summary = {
        "model": os.getenv("MODEL_NAME", "perf-model"),
        "requests": total_requests,
        "concurrency": concurrency,
        "warmup_requests": warmup_requests,
        "succeeded": len(ok_records),
        "failed": total_requests - len(ok_records),
        "elapsed_seconds": elapsed,
        "requests_per_second": len(ok_records) / elapsed if elapsed > 0 else 0.0,
        "completion_tokens_per_second": completion_tokens / elapsed if elapsed > 0 else 0.0,
        "total_tokens_per_second": total_tokens / elapsed if elapsed > 0 else 0.0,
        "latency_avg_seconds": statistics.mean(latencies) if latencies else 0.0,
        "latency_p50_seconds": percentile(latencies, 50),
        "latency_p90_seconds": percentile(latencies, 90),
        "latency_p95_seconds": percentile(latencies, 95),
        "latency_p99_seconds": percentile(latencies, 99),
        "requests_path": str(request_path),
        "errors": [record for record in records if record["status"] != "ok"][:10],
    }
    (output_dir / "perf_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
