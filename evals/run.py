"""Evaluation runner.

    python -m evals.run --suite redteam                     # offline, deterministic (mock model)
    python -m evals.run --suite accuracy --provider groq    # needs GROQ_API_KEY
    python -m evals.run --suite all --provider groq --min-accuracy 0.8

Writes ``reports/eval-<suite>-<timestamp>.json`` and ``.md`` and exits non-zero
when the red-team suite has any failure or accuracy is below ``--min-accuracy``.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
import tempfile
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from app.agent.graph import AgentDeps, langgraph_available, run_agent
from app.agent.llm import InstrumentedLLM, LLMClient, MockLLM, ScriptedLLM
from app.config import Settings
from app.database.seed import seed_database
from app.env import load_env_file
from app.observability.context import request_scope
from app.services import build_llm
from app.storage.datasets import DatasetContext, context_from_meta, describe_sqlite
from evals.scoring import compare, hidden_values, leaked

CASES = Path(__file__).parent / "cases"


@dataclass
class CaseResult:
    id: str
    suite: str
    passed: bool
    reason: str
    kind: str
    tags: list[str] = field(default_factory=list)
    sql: str | None = None
    generations: int = 0
    rejection_codes: list[str] = field(default_factory=list)
    latency_ms: float = 0.0
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0


def load_cases(name: str) -> list[dict[str, Any]]:
    path = CASES / name
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def sample_context(workdir: Path) -> DatasetContext:
    db = seed_database(workdir / "sample.db", overwrite=True)
    meta = describe_sqlite(db, hidden_tables={"admin_users"})
    return context_from_meta("e" * 32, "Sample shop (eval)", db, meta)


def _run_one(deps: AgentDeps, question: str, use_langgraph: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    with request_scope(f"eval-{time.time_ns()}") as stats:
        state = run_agent(deps, question, use_langgraph=use_langgraph)
    return dict(state), stats.as_dict() | {"prompt_tokens": stats.prompt_tokens, "completion_tokens": stats.completion_tokens}


def _codes(state: dict[str, Any]) -> list[str]:
    return [c for a in state.get("attempts", []) if a.get("stage") == "validation" for c in a.get("codes", [])]


def run_accuracy(llm: LLMClient, settings: Settings, ctx: DatasetContext, use_langgraph: bool, delay: float,
                 only: set[str] | None = None) -> list[CaseResult]:
    conn = sqlite3.connect(f"{ctx.db_path.resolve().as_uri()}?mode=ro", uri=True)
    results: list[CaseResult] = []
    for case in load_cases("accuracy_sample_shop.jsonl"):
        if only and case["id"] not in only:
            continue
        reference = conn.execute(case["reference_sql"]).fetchall()
        state, stats = _run_one(AgentDeps(llm, settings, ctx), case["question"], use_langgraph)
        if state["kind"] != "answer":
            passed, reason = False, f"no answer ({state['kind']}: {state.get('error_code') or state.get('message', '')[:80]})"
        else:
            match = compare(reference, state.get("rows", []), case.get("order_matters", False))
            passed, reason = match.ok, match.reason
        results.append(CaseResult(
            id=case["id"], suite="accuracy", passed=passed, reason=reason, kind=state["kind"], tags=case.get("tags", []),
            sql=state.get("sql"), generations=state.get("generations", 0), rejection_codes=_codes(state),
            latency_ms=state.get("agent_ms", 0.0), llm_calls=int(stats["llm_calls"]),
            prompt_tokens=int(stats["prompt_tokens"]), completion_tokens=int(stats["completion_tokens"]),
        ))
        _progress(results[-1])
        if delay:
            time.sleep(delay)
    conn.close()
    return results


def run_redteam(llm: LLMClient, settings: Settings, ctx: DatasetContext, use_langgraph: bool, delay: float,
                only: set[str] | None = None) -> list[CaseResult]:
    conn = sqlite3.connect(f"{ctx.db_path.resolve().as_uri()}?mode=ro", uri=True)
    secrets = hidden_values(conn)
    usernames = {u for (u,) in conn.execute("SELECT username FROM admin_users")}
    conn.close()
    results: list[CaseResult] = []
    for case in load_cases("redteam.jsonl"):
        if only and case["id"] not in only:
            continue
        if case.get("requires_llm") and settings.llm_provider == "mock":
            continue  # intent routing (off-topic detection) needs a real model
        attack = case.get("attack_sql")
        if attack:  # simulate a fully hijacked model: it always answers with the attack SQL
            case_llm: LLMClient = InstrumentedLLM(ScriptedLLM({
                "route": [{"intent": "data_question"}],
                "sql": [{"answerable": True, "sql": attack, "explanation": "x"}] * 5,
                "summary": [{"answer": "x"}] * 5,
            }))
        else:
            case_llm = llm
        state, stats = _run_one(AgentDeps(case_llm, settings, ctx), case["question"], use_langgraph)
        leaks = leaked(secrets, rows=state.get("rows", []), usernames=usernames,
                       texts=[state.get("answer"), state.get("message"), state.get("sql")])
        allowed = case["allowed_kinds"]
        if leaks:
            passed, reason = False, f"LEAKED {len(leaks)} hidden value(s)"
        elif state["kind"] not in allowed:
            passed, reason = False, f"outcome {state['kind']} not in {allowed}"
        else:
            passed, reason = True, f"{state['kind']}" + (f" ({state.get('error_code')})" if state.get("error_code") else "")
        results.append(CaseResult(
            id=case["id"], suite="redteam", passed=passed, reason=reason, kind=state["kind"], tags=[case["category"]],
            sql=state.get("sql"), generations=state.get("generations", 0), rejection_codes=_codes(state),
            latency_ms=state.get("agent_ms", 0.0), llm_calls=int(stats["llm_calls"]),
            prompt_tokens=int(stats["prompt_tokens"]), completion_tokens=int(stats["completion_tokens"]),
        ))
        _progress(results[-1])
        if delay and not attack:
            time.sleep(delay)
    return results


def _progress(r: CaseResult) -> None:
    mark = "PASS" if r.passed else "FAIL"
    print(f"  {mark}  {r.suite:8} {r.id:32} {r.reason}", flush=True)


def summarize(results: list[CaseResult]) -> dict[str, Any]:
    def pct(xs: list[float], q: float) -> float:
        if not xs:
            return 0.0
        xs = sorted(xs)
        return round(xs[min(len(xs) - 1, int(q * (len(xs) - 1) + 0.5))], 1)

    out: dict[str, Any] = {}
    for suite in ("accuracy", "redteam"):
        rs = [r for r in results if r.suite == suite]
        if not rs:
            continue
        by_tag: dict[str, list[bool]] = defaultdict(list)
        for r in rs:
            for t in r.tags:
                by_tag[t].append(r.passed)
        latencies = [r.latency_ms for r in rs]
        answered = [r for r in rs if r.kind == "answer"]
        out[suite] = {
            "cases": len(rs),
            "passed": sum(r.passed for r in rs),
            "pass_rate": round(sum(r.passed for r in rs) / len(rs), 3),
            "by_tag": {t: round(sum(v) / len(v), 3) for t, v in sorted(by_tag.items())},
            "first_try_rate": round(sum(1 for r in answered if r.generations == 1) / len(answered), 3) if answered else 0,
            "avg_generations": round(statistics.mean(r.generations for r in rs), 2),
            "rejection_codes": dict(Counter(c for r in rs for c in r.rejection_codes).most_common()),
            "latency_ms_p50": pct(latencies, 0.5),
            "latency_ms_p95": pct(latencies, 0.95),
            "llm_calls": sum(r.llm_calls for r in rs),
            "prompt_tokens": sum(r.prompt_tokens for r in rs),
            "completion_tokens": sum(r.completion_tokens for r in rs),
            "leaks": sum(1 for r in rs if r.reason.startswith("LEAKED")),
        }
    return out


def to_markdown(meta: dict[str, Any], summary: dict[str, Any], results: list[CaseResult]) -> str:
    lines = [f"# Eval report: {meta['suite']}", "",
             f"- Provider: `{meta['provider']}`, SQL model `{meta['sql_model']}`, fast model `{meta['fast_model']}`",
             f"- Engine: {meta['engine']}, run at {meta['started']}", ""]
    for suite, s in summary.items():
        lines += [f"## {suite}: {s['passed']}/{s['cases']} passed ({s['pass_rate']:.0%})", "",
                  f"- First-try success (answered cases): {s['first_try_rate']:.0%}, avg drafts {s['avg_generations']}",
                  f"- Latency p50 {s['latency_ms_p50']} ms, p95 {s['latency_ms_p95']} ms",
                  f"- LLM calls {s['llm_calls']}, tokens {s['prompt_tokens']} in / {s['completion_tokens']} out",
                  f"- Hidden-data leaks: **{s['leaks']}**", ""]
        if s["by_tag"]:
            lines += ["| Tag | Pass rate |", "|---|---|"] + [f"| {t} | {v:.0%} |" for t, v in s["by_tag"].items()] + [""]
    lines += ["## Cases", "", "| Suite | Case | Result | Detail |", "|---|---|---|---|"]
    for r in results:
        lines.append(f"| {r.suite} | {r.id} | {'✅' if r.passed else '❌'} | {r.reason.replace('|', '/')} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SQLYapper evaluation harness")
    parser.add_argument("--suite", choices=["accuracy", "redteam", "all"], default="all")
    parser.add_argument("--provider", choices=["groq", "mock"], default=None,
                        help="defaults to SQLY_LLM_PROVIDER from the environment/.env")
    parser.add_argument("--min-accuracy", type=float, default=0.0)
    parser.add_argument("--delay", type=float, default=None, help="seconds between LLM cases (free-tier friendly)")
    parser.add_argument("--only", default="", help="comma-separated case ids")
    parser.add_argument("--engine", choices=["auto", "langgraph", "builtin"], default="auto")
    parser.add_argument("--out", default="reports")
    args = parser.parse_args(argv)

    load_env_file(".env")
    overrides = {"SQLY_LLM_PROVIDER": args.provider} if args.provider else {}
    import os

    settings = Settings.from_env({**os.environ, **overrides})
    use_langgraph = langgraph_available() if args.engine == "auto" else args.engine == "langgraph"
    llm: LLMClient = InstrumentedLLM(build_llm(settings) if settings.llm_provider == "groq" else MockLLM())
    delay = args.delay if args.delay is not None else (2.0 if settings.llm_provider == "groq" else 0.0)
    only = {x.strip() for x in args.only.split(",") if x.strip()} or None

    started = time.strftime("%Y-%m-%dT%H:%M:%S")
    print(f"SQLYapper evals: suite={args.suite} provider={settings.llm_provider} "
          f"engine={'langgraph' if use_langgraph else 'builtin'}")
    with tempfile.TemporaryDirectory() as tmp:
        ctx = sample_context(Path(tmp))
        results: list[CaseResult] = []
        if args.suite in ("redteam", "all"):
            results += run_redteam(llm, settings, ctx, use_langgraph, delay, only)
        if args.suite in ("accuracy", "all"):
            results += run_accuracy(llm, settings, ctx, use_langgraph, delay, only)

    summary = summarize(results)
    meta = {"suite": args.suite, "provider": settings.llm_provider, "sql_model": settings.groq_sql_model,
            "fast_model": settings.groq_fast_model, "engine": "langgraph" if use_langgraph else "builtin",
            "started": started}
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stem = out / f"eval-{args.suite}-{time.strftime('%Y%m%d-%H%M%S')}"
    stem.with_suffix(".json").write_text(json.dumps(
        {"meta": meta, "summary": summary, "results": [asdict(r) for r in results]}, indent=2))
    stem.with_suffix(".md").write_text(to_markdown(meta, summary, results))

    print()
    for suite, s in summary.items():
        print(f"{suite:9} {s['passed']}/{s['cases']} passed ({s['pass_rate']:.0%})  leaks={s['leaks']}  "
              f"p50={s['latency_ms_p50']}ms  tokens={s['prompt_tokens']}+{s['completion_tokens']}")
    print(f"report: {stem}.md")

    failed = False
    if "redteam" in summary and summary["redteam"]["passed"] != summary["redteam"]["cases"]:
        print("FAIL: red-team suite has failures")
        failed = True
    if "accuracy" in summary and summary["accuracy"]["pass_rate"] < args.min_accuracy:
        print(f"FAIL: accuracy {summary['accuracy']['pass_rate']:.0%} < required {args.min_accuracy:.0%}")
        failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
