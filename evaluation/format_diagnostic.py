"""Ollama format diagnostic — does schema-constrained decoding suppress risk flags?

Run from repo root:
    uv run python -m evaluation.format_diagnostic

Runs every golden email twice through the real pipeline (run_triage → Agent 2 → Agent 3,
Agent 4 skipped) with the same prompt, temperature and seed:
    schema — format=TriageOutput JSON schema (what OllamaClient.chat_json sends)
    none   — no format constraint; the raw reply goes straight to model_validate_json with
             no retry or repair. Replies that fail validation are counted as parse failures
             and left out of that arm's metrics.

The only difference between the arms is the `format` argument. The prompt is built by
run_triage itself, and the script checks that the system prompt it sends is exactly the
triage skill file (sha must match the baseline's) and that the schema arm reproduces the
baseline predictions.

Writes evaluation/reports/<timestamp>-format_diagnostic/{report.json, report.md, per_email.json}.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from evaluation.run_eval import (
    HIGH,
    REPO,
    REPORTS_DIR,
    TRIAGE_SKILL,
    _executor_decision,
    _load_golden,
    _metadata,
    _model_info,
    _NoDraft,
    _score,
    _sha12,
)
from src.agents.spam_decision import run_spam_decision
from src.agents.triage import run_triage
from src.llm.ollama_client import OllamaClient
from src.pipeline.orchestrator import Orchestrator

BASELINE_PROMPT_SHA = "2b60d0786797"
BASELINE_DIR = REPORTS_DIR / "2026-11-20T09-42-55-267632+00-00-baseline"
ARMS = ("schema", "none")


class _FormatLLM:
    """Same request as OllamaClient.chat_json, with `format` switchable; keeps the raw reply."""

    def __init__(self, client: OllamaClient, use_schema: bool, temperature: float, seed: int) -> None:
        self.client = client
        self.use_schema = use_schema
        self.temperature = temperature
        self.seed = seed
        self.last: dict[str, Any] = {}

    async def chat_json(self, *, system: str, user: str, schema):
        self.last = {"system_sha": hashlib.sha256(system.encode()).hexdigest(), "raw": None}
        response = await self.client.client.chat(
            model=self.client.model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            format=schema.model_json_schema() if self.use_schema else None,
            options={"temperature": self.temperature, "seed": self.seed},
        )
        self.last["raw"] = response["message"]["content"]
        return schema.model_validate_json(self.last["raw"])


async def _predict(orch: Orchestrator, llm: _FormatLLM, row: dict[str, Any]) -> dict[str, Any]:
    try:
        r = await orch.process_email((REPO / row["raw_email_path"]).read_bytes())
    except ValidationError as e:
        return {"error": "parse_failure", "detail": str(e).splitlines()[0], **llm.last}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}", **llm.last}
    return {
        "error": None,
        "category": r.triage.category.value,
        "spam_level": r.triage.spam_level.value,
        "reply_needed": r.triage.reply_needed,
        "reply_type": r.triage.reply_type.value,
        "recommended_action": r.triage.recommended_action.value,
        "final_action": r.final_action.value,
        "risk_flags": sorted(f.value for f in r.triage.risk_flags),
        "confidence": r.triage.confidence,
        "executor_decision": _executor_decision(r),
        **llm.last,
    }


def _summary(rows: list[dict[str, Any]], preds: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [(r, p) for r, p in zip(rows, preds) if p["error"] is None]
    s = _score([r for r, _ in ok], [p for _, p in ok])
    v = s["safety_violations"]
    return {
        "n_scored": len(ok),
        "empty_risk_flags": sum(not p["risk_flags"] for _, p in ok),
        "empty_high_risk_flags": sum(not set(p["risk_flags"]) & set(HIGH) for _, p in ok),
        "high_risk_micro": s["high_risk_micro"],
        "high_risk_per_flag": s["high_risk_per_flag"],
        "high_risk_macro_recall": s["high_risk_macro_recall"],
        "external_link": s["secondary_flags"]["external_link"],
        "credential_request": s["secondary_flags"]["credential_request"],
        "safety_violations": v,
        "violations_final_action": len(v["final_action"]["junk"]) + len(v["final_action"]["pass"]),
        "violations_executor": len(v["executor"]["quarantine"]) + len(v["executor"]["pass"]),
        "injection": {k: s["injection"][k] for k in ("model_all", "model_any", "system", "n")},
        "categorical_accuracy": {f: c["accuracy"] for f, c in s["categorical"].items()},
    }


def _baseline_check(rows: list[dict[str, Any]], schema_preds: list[dict[str, Any]]) -> dict[str, Any]:
    path = BASELINE_DIR / "per_email.json"
    if not path.exists():
        return {"available": False}
    base = {e["id"]: e["predicted"][0] for e in json.loads(path.read_text())}
    keys = ("category", "spam_level", "reply_type", "recommended_action", "final_action",
            "risk_flags", "confidence", "executor_decision")
    differ = [r["id"] for r, p in zip(rows, schema_preds)
              if any(p.get(k) != base[r["id"]].get(k) for k in keys)]
    return {"available": True, "baseline_dir": str(BASELINE_DIR.relative_to(REPO)), "differing_ids": differ}


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.0%}"


def _ratio(hits: int, total: int) -> str:
    return f"{hits}/{total} = {_pct(hits / total if total else None)}"


def _render_md(report: dict[str, Any]) -> str:
    m, chk = report["metadata"], report["checks"]
    full, paired = report["full"], report["paired"]
    lines = [
        f"# Format diagnostic — {report['timestamp']}", "",
        f"- model `{m['model']}` (digest `{m['model_digest'][:12]}`, {m['model_quantization']}), "
        f"temperature {m['temperature']}, seed {m['seed']}",
        f"- commit `{m['git_commit'][:8]}` dirty={m['git_dirty']} untracked={m['git_untracked']}",
        f"- prompt sha `{m['triage_prompt_sha']}` (baseline `{BASELINE_PROMPT_SHA}`); system prompt sent "
        f"== triage skill file for every call: {chk['system_prompt_identical']}",
        f"- schema arm vs baseline predictions: "
        + (f"differing ids {chk['baseline']['differing_ids']}" if chk["baseline"]["available"] else "baseline not found"),
        f"- format=None parse failures: **{len(report['parse_failures'])}** {[f['id'] for f in report['parse_failures']]}",
        "",
        "## All parsed emails (each arm scored on its own parsed set)", "",
    ]
    lines += _table(full)
    lines += ["", f"## Paired: only emails both arms parsed (n={paired['schema']['n_scored']})", ""]
    lines += _table(paired)
    lines += ["", "## Per-flag recall (paired set)", "", "| flag | schema | none |", "|---|---|---|"]
    for f in HIGH:
        a, b = paired["schema"]["high_risk_per_flag"][f], paired["none"]["high_risk_per_flag"][f]
        lines.append(f"| {f} | {a['hits']}/{a['gold']} (pred {a['predicted']}) | {b['hits']}/{b['gold']} (pred {b['predicted']}) |")
    return "\n".join(lines) + "\n"


def _table(block: dict[str, dict[str, Any]]) -> list[str]:
    rows = [("emails scored", lambda s: str(s["n_scored"])),
            ("empty risk_flags", lambda s: _ratio(s["empty_risk_flags"], s["n_scored"])),
            ("no high-risk flag", lambda s: _ratio(s["empty_high_risk_flags"], s["n_scored"])),
            ("high-risk precision", lambda s: _ratio(s["high_risk_micro"]["hits"], s["high_risk_micro"]["predicted"])),
            ("high-risk recall", lambda s: _ratio(s["high_risk_micro"]["hits"], s["high_risk_micro"]["gold"])),
            ("macro recall (6 flags)", lambda s: _pct(s["high_risk_macro_recall"])),
            ("external_link recall", lambda s: _ratio(s["external_link"]["hits"], s["external_link"]["gold"])),
            ("violations, final_action", lambda s: f"{s['violations_final_action']}/{s['safety_violations']['n_high_risk_emails']}"),
            ("violations, executor", lambda s: f"{s['violations_executor']}/{s['safety_violations']['n_high_risk_emails']}"),
            ("injection system", lambda s: f"{s['injection']['system']}/{s['injection']['n']}")]
    out = ["| metric | format=schema | format=None |", "|---|---|---|"]
    out += [f"| {name} | {fn(block['schema'])} | {fn(block['none'])} |" for name, fn in rows]
    return out


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    prompt_sha = _sha12(TRIAGE_SKILL.read_bytes())
    if prompt_sha != BASELINE_PROMPT_SHA:
        raise SystemExit(f"triage prompt sha {prompt_sha} != baseline {BASELINE_PROMPT_SHA}")
    expected_system_sha = hashlib.sha256(
        TRIAGE_SKILL.read_text(encoding="utf-8").replace("{{few_shot_examples}}", "").encode()
    ).hexdigest()

    rows = _load_golden()
    client = OllamaClient()
    args.mode, args.runs = "format_diagnostic", 1
    metadata = _metadata(args, client.model, await _model_info(client), len(rows))

    preds: dict[str, list[dict[str, Any]]] = {}
    for arm in ARMS:
        llm = _FormatLLM(client, arm == "schema", args.temperature, args.seed)
        orch = Orchestrator(triage_runner=partial(run_triage, llm_client=llm),
                            spam_decider=run_spam_decision, draft_agent=_NoDraft())
        preds[arm] = []
        for row in rows:
            preds[arm].append(await _predict(orch, llm, row))
            print(f"{arm} {row['id']} {preds[arm][-1]['error'] or 'ok'}", flush=True)

    both_ok = [i for i in range(len(rows)) if all(preds[a][i]["error"] is None for a in ARMS)]
    report = {
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "metadata": metadata,
        "checks": {
            "system_prompt_identical": all(p.get("system_sha") == expected_system_sha
                                           for a in ARMS for p in preds[a]),
            "baseline": _baseline_check(rows, preds["schema"]),
        },
        "parse_failures": [{"id": r["id"], "detail": p.get("detail")}
                           for r, p in zip(rows, preds["none"]) if p["error"] == "parse_failure"],
        "other_errors": [{"arm": a, "id": r["id"], "error": p["error"]} for a in ARMS
                         for r, p in zip(rows, preds[a]) if p["error"] not in (None, "parse_failure")],
        "full": {a: _summary(rows, preds[a]) for a in ARMS},
        "paired": {a: _summary([rows[i] for i in both_ok], [preds[a][i] for i in both_ok]) for a in ARMS},
    }
    out = REPORTS_DIR / f"{report['timestamp'].replace(':', '-').replace('.', '-')}-format_diagnostic"
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    per_email = [{"id": r["id"], "group": r["group"], "expected": r["expected"],
                  **{a: preds[a][i] for a in ARMS}} for i, r in enumerate(rows)]
    (out / "per_email.json").write_text(json.dumps(per_email, indent=2, ensure_ascii=False))
    (out / "report.md").write_text(_render_md(report))
    print(f"\nReport written to {out}")


if __name__ == "__main__":
    asyncio.run(main())
