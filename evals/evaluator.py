"""Model-quality evaluator: scores prompts against expected_regex, pushes metrics."""
import json
import os
import re
import sys
import time
from typing import Any

import requests
from prometheus_client import CollectorRegistry, Gauge, push_to_gateway


def score_response(response_text: str, expected_regex: str) -> bool:
    return bool(re.search(expected_regex, response_text))


def load_prompts(path: str) -> list[dict[str, Any]]:
    prompts = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                prompts.append(json.loads(line))
    return prompts


def call_vllm(vllm_url: str, model: str, prompt: str, api_key: str,
              max_tokens: int, temperature: float, timeout: int = 90) -> tuple[str, int, float]:
    headers = {"content-type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    t0 = time.time()
    r = requests.post(
        f"{vllm_url}/v1/chat/completions",
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature,
        },
        headers=headers,
        timeout=timeout,
    )
    dt = time.time() - t0
    r.raise_for_status()
    body = r.json()
    text = body["choices"][0]["message"]["content"]
    used = body.get("usage", {}).get("completion_tokens", 0)
    return text, used, dt


def aggregate(results: list[dict]) -> dict[str, dict]:
    by_cat: dict[str, dict] = {}
    for r in results:
        cat = r["category"]
        d = by_cat.setdefault(cat, {"pass": 0, "total": 0, "lat": [], "tok": []})
        d["total"] += 1
        if r.get("pass"):
            d["pass"] += 1
        if r.get("latency") is not None:
            d["lat"].append(r["latency"])
        if r.get("tokens") is not None:
            d["tok"].append(r["tokens"])
    return by_cat


def evaluate_all(prompts: list[dict], call_fn) -> list[dict]:
    results = []
    for p in prompts:
        try:
            text, used, dt = call_fn(p["prompt"])
            ok = score_response(text, p["expected_regex"])
            results.append({"id": p["id"], "category": p["category"], "pass": ok,
                            "latency": dt, "tokens": used, "response": text})
        except Exception as e:
            results.append({"id": p["id"], "category": p["category"], "pass": False,
                            "latency": None, "tokens": None, "error": str(e)})
    return results


def build_registry(by_cat: dict, model: str, overall_pass: int,
                   overall_total: int) -> CollectorRegistry:
    reg = CollectorRegistry()
    g_pass = Gauge("model_eval_pass_rate", "fraction of prompts passing per category",
                   ["category", "model"], registry=reg)
    g_lat = Gauge("model_eval_latency_seconds", "mean response latency per category",
                  ["category", "model"], registry=reg)
    g_tokens = Gauge("model_eval_response_tokens", "mean response tokens per category",
                     ["category", "model"], registry=reg)
    g_ts = Gauge("model_eval_last_run_timestamp", "unix epoch of last eval run",
                 ["model"], registry=reg)
    g_total = Gauge("model_eval_prompts_total", "prompts run per category",
                    ["category", "model"], registry=reg)

    for cat, d in by_cat.items():
        rate = d["pass"] / d["total"] if d["total"] else 0.0
        lat = sum(d["lat"]) / len(d["lat"]) if d["lat"] else 0.0
        tok = sum(d["tok"]) / len(d["tok"]) if d["tok"] else 0.0
        g_pass.labels(category=cat, model=model).set(rate)
        g_lat.labels(category=cat, model=model).set(lat)
        g_tokens.labels(category=cat, model=model).set(tok)
        g_total.labels(category=cat, model=model).set(d["total"])

    g_pass.labels(category="_all", model=model).set(
        overall_pass / overall_total if overall_total else 0.0)
    g_total.labels(category="_all", model=model).set(overall_total)
    g_ts.labels(model=model).set(time.time())
    return reg


def main() -> int:
    vllm_url = os.environ["VLLM_URL"]
    model = os.environ["MODEL"]
    api_key = os.environ.get("VLLM_API_KEY", "")
    prompts_file = os.environ["PROMPTS_FILE"]
    pushgw = os.environ["PUSHGATEWAY_URL"].rstrip("/")
    max_tokens = int(os.environ.get("MAX_TOKENS", "200"))
    temperature = float(os.environ.get("TEMPERATURE", "0.1"))
    min_pass_rate = float(os.environ.get("MIN_OVERALL_PASS_RATE", "0.4"))

    prompts = load_prompts(prompts_file)

    def call_fn(prompt: str):
        return call_vllm(vllm_url, model, prompt, api_key, max_tokens, temperature)

    results = evaluate_all(prompts, call_fn)
    for r in results:
        status = "PASS" if r.get("pass") else "FAIL"
        if "error" in r:
            print(f"[{r['category']:12}] {r['id']:24} ERROR {r['error']}", file=sys.stderr)
        else:
            print(f"[{r['category']:12}] {r['id']:24} {status} "
                  f"lat={r['latency']:.2f}s tok={r['tokens']}")

    by_cat = aggregate(results)
    overall_pass = sum(d["pass"] for d in by_cat.values())
    overall_total = sum(d["total"] for d in by_cat.values())

    for cat, d in by_cat.items():
        rate = d["pass"] / d["total"] if d["total"] else 0.0
        lat = sum(d["lat"]) / len(d["lat"]) if d["lat"] else 0.0
        tok = sum(d["tok"]) / len(d["tok"]) if d["tok"] else 0.0
        print(f"category={cat} pass_rate={rate:.2%} latency={lat:.2f}s ntok={tok:.0f}")

    overall_rate = overall_pass / overall_total if overall_total else 0.0
    print(f"overall pass_rate={overall_rate:.2%} ({overall_pass}/{overall_total})")

    reg = build_registry(by_cat, model, overall_pass, overall_total)
    push_to_gateway(pushgw, job="model-quality-eval", registry=reg)
    print(f"pushed metrics to {pushgw}")

    return 1 if overall_total and overall_rate < min_pass_rate else 0


if __name__ == "__main__":
    sys.exit(main())
