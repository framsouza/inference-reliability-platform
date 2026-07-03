"""Unit tests for evals/evaluator.py."""
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import evaluator


# ────────── score_response ──────────

@pytest.mark.parametrize("text,pattern,expected", [
    ("Paris", "(?i)paris", True),
    ("The capital is Paris.", "(?i)paris", True),
    ("London", "(?i)paris", False),
    ("345", r"\b345\b", True),
    ("The answer is 345, obviously.", r"\b345\b", True),
    ("3456", r"\b345\b", False),
    ("HELLO", r"^\s*HELLO\s*\.?\s*$", True),
    ("hello", r"^\s*HELLO\s*\.?\s*$", False),
    ("HELLO WORLD", r"^\s*HELLO\s*\.?\s*$", False),
    ("def reverse_string(s): return s[::-1]",
     r"(?s)def\s+reverse_string.*\[::-1\]", True),
])
def test_score_response(text, pattern, expected):
    assert evaluator.score_response(text, pattern) is expected


# ────────── load_prompts ──────────

def test_load_prompts_basic(tmp_path):
    f = tmp_path / "p.jsonl"
    f.write_text(
        '{"id":"a","category":"factual","prompt":"q","expected_regex":"x"}\n'
        '{"id":"b","category":"math","prompt":"q","expected_regex":"y"}\n'
    )
    prompts = evaluator.load_prompts(str(f))
    assert len(prompts) == 2
    assert prompts[0]["id"] == "a"
    assert prompts[1]["category"] == "math"


def test_load_prompts_ignores_blank_lines(tmp_path):
    f = tmp_path / "p.jsonl"
    f.write_text(
        '\n'
        '{"id":"a","category":"factual","prompt":"q","expected_regex":"x"}\n'
        '\n'
        '  \n'
        '{"id":"b","category":"math","prompt":"q","expected_regex":"y"}\n'
    )
    prompts = evaluator.load_prompts(str(f))
    assert len(prompts) == 2


def test_load_prompts_from_shipped_configmap():
    """The real prompts.jsonl in the repo must parse cleanly and have the fields the runner expects."""
    repo_root = Path(__file__).resolve().parents[2]
    cm_path = repo_root / "evals" / "prompts-configmap.yaml"
    import yaml
    doc = yaml.safe_load(cm_path.read_text())
    body = doc["data"]["prompts.jsonl"]
    prompts = [json.loads(l) for l in body.splitlines() if l.strip()]
    assert len(prompts) >= 5
    for p in prompts:
        assert set(p.keys()) >= {"id", "category", "prompt", "expected_regex"}
        assert p["category"] in {"factual", "math", "code", "instruction", "reasoning"}


# ────────── evaluate_all ──────────

def make_call(responses):
    calls = {"n": 0}

    def _call(prompt):
        calls["n"] += 1
        idx = calls["n"] - 1
        if idx >= len(responses):
            raise RuntimeError("unexpected extra call")
        r = responses[idx]
        if isinstance(r, Exception):
            raise r
        return r  # (text, tokens, latency)
    return _call


def test_evaluate_all_passes_and_fails():
    prompts = [
        {"id": "a", "category": "factual", "prompt": "q", "expected_regex": "yes"},
        {"id": "b", "category": "math", "prompt": "q", "expected_regex": "42"},
        {"id": "c", "category": "code", "prompt": "q", "expected_regex": "def "},
    ]
    call = make_call([("yes", 3, 0.5), ("wrong", 5, 0.7), ("def foo():", 10, 1.0)])
    results = evaluator.evaluate_all(prompts, call)
    assert results[0]["pass"] is True
    assert results[1]["pass"] is False
    assert results[2]["pass"] is True
    assert results[0]["latency"] == 0.5
    assert results[2]["tokens"] == 10


def test_evaluate_all_records_errors_as_failures():
    prompts = [{"id": "a", "category": "factual", "prompt": "q", "expected_regex": "x"}]
    call = make_call([RuntimeError("network down")])
    results = evaluator.evaluate_all(prompts, call)
    assert results[0]["pass"] is False
    assert results[0]["error"] == "network down"
    assert results[0]["latency"] is None


# ────────── aggregate ──────────

def test_aggregate_by_category():
    results = [
        {"category": "factual", "pass": True, "latency": 0.5, "tokens": 3},
        {"category": "factual", "pass": False, "latency": 0.8, "tokens": 5},
        {"category": "math", "pass": True, "latency": 1.2, "tokens": 10},
    ]
    by_cat = evaluator.aggregate(results)
    assert by_cat["factual"]["total"] == 2
    assert by_cat["factual"]["pass"] == 1
    assert by_cat["math"]["pass"] == 1
    assert by_cat["math"]["total"] == 1
    assert by_cat["factual"]["lat"] == [0.5, 0.8]


def test_aggregate_handles_errors():
    """A result with no latency/tokens (an errored call) must not blow up aggregation."""
    results = [
        {"category": "factual", "pass": False, "latency": None, "tokens": None},
        {"category": "factual", "pass": True, "latency": 0.5, "tokens": 3},
    ]
    by_cat = evaluator.aggregate(results)
    assert by_cat["factual"]["total"] == 2
    assert by_cat["factual"]["pass"] == 1
    assert by_cat["factual"]["lat"] == [0.5]  # errored call skipped


# ────────── call_vllm (with mocked requests) ──────────

def test_call_vllm_sends_auth_header(monkeypatch):
    captured = {}

    class FakeResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"choices": [{"message": {"content": "hi"}}], "usage": {"completion_tokens": 1}}

    def fake_post(url, **kw):
        captured["url"] = url
        captured["headers"] = kw["headers"]
        captured["json"] = kw["json"]
        return FakeResp()

    monkeypatch.setattr(evaluator.requests, "post", fake_post)
    text, tokens, dt = evaluator.call_vllm(
        "http://vllm:8000", "test-model", "hello",
        "s3cr3t", max_tokens=42, temperature=0.5)
    assert text == "hi"
    assert tokens == 1
    assert dt >= 0
    assert captured["url"] == "http://vllm:8000/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer s3cr3t"
    assert captured["json"]["max_tokens"] == 42


def test_call_vllm_no_auth_when_key_empty(monkeypatch):
    captured = {}

    class FakeResp:
        def raise_for_status(self): pass
        def json(self):
            return {"choices": [{"message": {"content": "x"}}], "usage": {"completion_tokens": 0}}

    def fake_post(url, **kw):
        captured["headers"] = kw["headers"]
        return FakeResp()

    monkeypatch.setattr(evaluator.requests, "post", fake_post)
    evaluator.call_vllm("http://vllm:8000", "m", "p", "", 100, 0.1)
    assert "Authorization" not in captured["headers"]


# ────────── build_registry ──────────

def test_build_registry_emits_all_metrics():
    by_cat = {
        "factual": {"pass": 2, "total": 3, "lat": [0.5, 0.6, 0.7], "tok": [10, 12, 14]},
        "math": {"pass": 1, "total": 2, "lat": [1.0, 1.2], "tok": [20, 22]},
    }
    reg = evaluator.build_registry(by_cat, "m", overall_pass=3, overall_total=5)
    samples = {(m.name, tuple(sorted(m.labels.items()))): m.value
               for family in reg.collect() for m in family.samples}

    def get(name, labels):
        return samples[(name, tuple(sorted(labels.items())))]

    assert get("model_eval_pass_rate", {"category": "factual", "model": "m"}) == pytest.approx(2/3)
    assert get("model_eval_pass_rate", {"category": "math", "model": "m"}) == 0.5
    assert get("model_eval_pass_rate", {"category": "_all", "model": "m"}) == pytest.approx(0.6)
    assert get("model_eval_prompts_total", {"category": "_all", "model": "m"}) == 5
    assert get("model_eval_latency_seconds", {"category": "factual", "model": "m"}) == pytest.approx(0.6)
    assert ("model_eval_last_run_timestamp", (("model", "m"),)) in samples


def test_build_registry_handles_all_zero_pass():
    by_cat = {"factual": {"pass": 0, "total": 3, "lat": [0.1], "tok": [1]}}
    reg = evaluator.build_registry(by_cat, "m", overall_pass=0, overall_total=3)
    samples = {(m.name, tuple(sorted(m.labels.items()))): m.value
               for family in reg.collect() for m in family.samples}
    assert samples[("model_eval_pass_rate", (("category", "_all"), ("model", "m")))] == 0.0
    assert samples[("model_eval_pass_rate", (("category", "factual"), ("model", "m")))] == 0.0


def test_build_registry_no_prompts_run():
    reg = evaluator.build_registry({}, "m", overall_pass=0, overall_total=0)
    samples = {(m.name, tuple(sorted(m.labels.items()))): m.value
               for family in reg.collect() for m in family.samples}
    # Overall rate should be 0.0, not raise ZeroDivisionError
    assert samples[("model_eval_pass_rate", (("category", "_all"), ("model", "m")))] == 0.0


# ────────── configmap drift check ──────────

def test_configmap_body_matches_evaluator_py():
    """The script embedded in evals/script-configmap.yaml must match evals/evaluator.py verbatim.
    This is the CI gate that prevents 'oh I edited evaluator.py but forgot the ConfigMap'."""
    import yaml
    repo_root = Path(__file__).resolve().parents[2]
    src = (repo_root / "evals" / "evaluator.py").read_text()
    cm = yaml.safe_load((repo_root / "evals" / "script-configmap.yaml").read_text())
    cm_script = cm["data"]["evaluator.py"]
    # Compare after normalizing trailing whitespace
    assert cm_script.rstrip() == src.rstrip(), (
        "evals/script-configmap.yaml is out of sync with evals/evaluator.py — regenerate it")
