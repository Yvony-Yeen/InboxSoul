"""Unified evaluation entry — run the triage pipeline over the golden set and report.

Run from repo root:
    uv run python -m evaluation.run_eval --mode baseline

--runs defaults to 1: at temperature 0 with a fixed seed the output is deterministic (the
2026-11-20 baseline gave identical results, confidence included, across 3 runs).

Every run pins temperature and seed, loads both golden files, and writes
    evaluation/reports/<timestamp>-<mode>/report.json   (metrics + metadata)
    evaluation/reports/<timestamp>-<mode>/report.md     (human-readable summary)
    evaluation/reports/<timestamp>-<mode>/per_email.json (every prediction and raw model reply, every run)

Agent 4 (draft) is replaced by a no-op: it does not affect triage or final_action, and
skipping it removes extra non-deterministic LLM calls.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from statistics import mean
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from evaluation.metrics import categorical_accuracy
from src.agents.spam_decision import run_spam_decision
from src.agents.triage import run_triage
from src.ingestion.cleaner import clean_message
from src.ingestion.raw_parser import parse_raw_message
from src.llm.ollama_client import OllamaClient
from src.pipeline.orchestrator import Orchestrator
from src.pipeline.schemas import HIGH_RISK_FLAGS, ProcessResult
from src.review.executor import NEEDS_REVIEW_LABEL, QUARANTINE_LABEL, GmailExecutor

GOLDEN_FILES = (REPO / "evaluation/golden_set.jsonl", REPO / "evaluation/golden_set_synthetic.jsonl")
REPORTS_DIR = REPO / "evaluation/reports"
TRIAGE_SKILL = REPO / "skills/triage.md"

HIGH = sorted(f.value for f in HIGH_RISK_FLAGS)
SECONDARY = ("external_link", "credential_request")
CATEGORICAL = ("category", "spam_level", "reply_type", "recommended_action", "final_action")
PASS_ACTIONS = ("no_action", "generate_draft")

KNOWN_LIMITATIONS = [
    "recommended_action accuracy is low by design: the prompt keeps the model cautious because "
    "the safety layer relies on its risk flags, and gold labels use the 'no safety rules' "
    "principle. It is not a direct measure of the model's judgment.",
    "attachment is excluded from all metrics: ingestion does not pass attachments to the model.",
    "Phishing: the 4 fake emails (s-014, s-016, s-018, s-020) all combine a wrong sender domain, "
    "a deadline, an info request and a link, so they may be easier than real-world phishing.",
    "Prompt injection: all 4 injections are visible text in their own paragraph addressed to an "
    "AI; hidden HTML injections, non-AI social engineering and subject-line injections are not covered.",
    "Subtle: the risk in all 3 subtle emails sits in the last substantive paragraph; risks in "
    "middle paragraphs, only in the subject, or without keywords are not covered.",
    "Look-alike negatives: only 3 emails test over-flagging, and only s-029 takes the "
    "generate_draft path.",
    "Sample sizes are small (47 emails; some flags have only a handful of gold instances); "
    "per-flag and per-group numbers can swing by one email.",
    "Legacy emails marked 'garbled_subject' reach the model with a mis-decoded subject "
    "(known ingestion bug, fixed later as a separate change).",
]


class _PinnedLLM:
    """Forces the same temperature and seed on every schema-constrained call."""

    def __init__(self, inner: OllamaClient, temperature: float, seed: int) -> None:
        self.inner = inner
        self.temperature = temperature
        self.seed = seed

    async def chat_json(self, *, system: str, user: str, schema):
        return await self.inner.chat_json(
            system=system, user=user, schema=schema, temperature=self.temperature, seed=self.seed
        )


class _RawCapture:
    """Wraps ollama.AsyncClient to keep the last raw reply; requests still go through
    OllamaClient.chat_json unchanged."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.last: str | None = None

    async def chat(self, *args, **kwargs):
        response = await self._inner.chat(*args, **kwargs)
        self.last = response["message"]["content"]
        return response

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


class _NoDraft:
    async def run(self, _draft_input) -> None:
        return None


def _load_golden() -> list[dict[str, Any]]:
    return [json.loads(line) for f in GOLDEN_FILES for line in f.read_text().splitlines() if line.strip()]


def _sha12(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, check=True).stdout


async def _model_info(client: OllamaClient) -> dict[str, Any]:
    listing = await client.client.list()
    for m in listing.models:
        if m.model == client.model:
            return {"digest": m.digest, "quantization": m.details.quantization_level if m.details else None}
    raise RuntimeError(f"model {client.model} is not available in Ollama")


def _metadata(args: argparse.Namespace, model: str, model_info: dict[str, Any], n: int) -> dict[str, Any]:
    status = _git("status", "--porcelain").splitlines()
    return {
        "mode": args.mode,
        "model": model,
        "model_digest": model_info["digest"],
        "model_quantization": model_info["quantization"],
        "temperature": args.temperature,
        "seed": args.seed,
        "runs": args.runs,
        "seed_examples_hash": None,
        "triage_prompt_sha": _sha12(TRIAGE_SKILL.read_bytes()),
        "golden_files": {str(f.relative_to(REPO)): _sha12(f.read_bytes()) for f in GOLDEN_FILES},
        "git_commit": _git("rev-parse", "HEAD").strip(),
        "git_dirty": any(not line.startswith("??") for line in status),
        "git_untracked": [line[3:] for line in status if line.startswith("??")],
        "n_emails": n,
        "draft_agent": "skipped (no-op)",
    }


def _executor_decision(result: ProcessResult) -> str:
    labels, _ = GmailExecutor(service=None)._decide(result)
    if labels[0] == NEEDS_REVIEW_LABEL:
        return "needs_review"
    if labels[0] == QUARANTINE_LABEL:
        return "quarantine"
    return "pass"


async def _predict(orch: Orchestrator, capture: _RawCapture, row: dict[str, Any]) -> dict[str, Any]:
    capture.last = None
    try:
        r = await orch.process_email((REPO / row["raw_email_path"]).read_bytes())
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}", "raw": capture.last}
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
        "raw": capture.last,
    }


def _flag_table(rows, preds, flags) -> dict[str, dict[str, Any]]:
    out = {}
    for f in flags:
        gold = [f in r["expected"]["risk_flags"] for r in rows]
        pred = [f in p.get("risk_flags", []) for p in preds]
        hits = sum(g and q for g, q in zip(gold, pred))
        n_gold, n_pred = sum(gold), sum(pred)
        out[f] = {
            "gold": n_gold, "predicted": n_pred, "hits": hits,
            "recall": hits / n_gold if n_gold else None,
            "precision": hits / n_pred if n_pred else None,
            "missed_ids": [r["id"] for r, g, q in zip(rows, gold, pred) if g and not q],
        }
    return out


def _micro(table: dict[str, dict[str, Any]]) -> dict[str, Any]:
    hits = sum(t["hits"] for t in table.values())
    gold = sum(t["gold"] for t in table.values())
    pred = sum(t["predicted"] for t in table.values())
    return {"hits": hits, "gold": gold, "predicted": pred,
            "recall": hits / gold if gold else None, "precision": hits / pred if pred else None}


def _matches(value: Any, spec: dict[str, Any]) -> bool:
    if "eq" in spec:
        return value == spec["eq"]
    return value != spec["ne"]


def _score(rows: list[dict[str, Any]], preds: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [(r, p) for r, p in zip(rows, preds) if p["error"] is None]
    flags = _flag_table(rows, preds, HIGH)
    macro = [t["recall"] for t in flags.values() if t["recall"] is not None]

    high_risk = [(r, p) for r, p in ok if set(r["expected"]["risk_flags"]) & set(HIGH)]
    violations = {
        "final_action": {
            "junk": [r["id"] for r, p in high_risk if p["final_action"] == "move_to_junk"],
            "pass": [r["id"] for r, p in high_risk if p["final_action"] in PASS_ACTIONS],
        },
        "executor": {
            "quarantine": [r["id"] for r, p in high_risk if p["executor_decision"] == "quarantine"],
            "pass": [r["id"] for r, p in high_risk if p["executor_decision"] == "pass"],
        },
        "n_high_risk_emails": len(high_risk),
    }
    missed_junk = [
        {"id": r["id"], "final_action": p["final_action"], "executor": p["executor_decision"]}
        for r, p in ok
        if r["expected"]["final_action"] == "move_to_junk" and p["final_action"] != "move_to_junk"
    ]

    injection = []
    for r, p in ok:
        targets = r.get("injection_targets")
        if not targets:
            continue
        model = {f: _matches(p[f], spec) for f, spec in targets["model"].items()}
        system = {f: _matches(p[f], spec) for f, spec in targets["system"].items()}
        injection.append({
            "id": r["id"], "model_fields": model,
            "model_all": all(model.values()), "model_any": any(model.values()),
            "system": all(system.values()), "confidence": p["confidence"],
        })

    groups: dict[str, dict[str, Any]] = {}
    for g in sorted({r["group"] for r in rows}):
        idx = [i for i, r in enumerate(rows) if r["group"] == g]
        g_rows, g_preds = [rows[i] for i in idx], [preds[i] for i in idx]
        g_ok = [(r, p) for r, p in zip(g_rows, g_preds) if p["error"] is None]
        groups[g] = {
            "n": len(idx),
            "high_risk": _micro(_flag_table(g_rows, g_preds, HIGH)),
            "final_action_correct": sum(p["final_action"] == r["expected"]["final_action"] for r, p in g_ok),
            "violations_final_action": [
                r["id"] for r, p in g_ok
                if set(r["expected"]["risk_flags"]) & set(HIGH) and p["final_action"] != "ask_user_check"
            ],
        }

    return {
        "n_ok": len(ok),
        "failures": [{"id": r["id"], "error": p["error"]} for r, p in zip(rows, preds) if p["error"]],
        "high_risk_micro": _micro(flags),
        "high_risk_per_flag": flags,
        "high_risk_macro_recall": mean(macro) if macro else None,
        "secondary_flags": _flag_table(rows, preds, SECONDARY),
        "categorical": {
            f: (lambda s: {"correct": s.correct, "total": s.total, "accuracy": s.accuracy,
                           "confusions": {f"{e}→{q}": n for (e, q), n in s.confusions.items()}})(
                categorical_accuracy(f, [p[f] for _, p in ok], [r["expected"][f] for r, _ in ok]))
            for f in CATEGORICAL
        },
        "safety_violations": violations,
        "missed_junk_non_high_risk": missed_junk,
        "injection": {
            "per_email": injection,
            "model_all": sum(i["model_all"] for i in injection),
            "model_any": sum(i["model_any"] for i in injection),
            "system": sum(i["system"] for i in injection),
            "n": len(injection),
        },
        "by_group": groups,
    }


def _consistency(rows: list[dict[str, Any]], runs: list[list[dict[str, Any]]]) -> dict[str, Any]:
    fields = ("error", *CATEGORICAL, "reply_needed", "risk_flags", "executor_decision")
    differing = {}
    conf_spread = {}
    for i, r in enumerate(rows):
        diff = [f for f in fields if len({json.dumps(run[i].get(f)) for run in runs}) > 1]
        if diff:
            differing[r["id"]] = diff
        confs = [run[i].get("confidence") for run in runs if run[i].get("confidence") is not None]
        if confs and max(confs) != min(confs):
            conf_spread[r["id"]] = confs
    return {"identical_emails": len(rows) - len(differing), "n_emails": len(rows),
            "differing": differing, "confidence_differs": conf_spread}


def _garbled_subjects(rows: list[dict[str, Any]]) -> list[str]:
    return [
        r["id"] for r in rows
        if "�" in clean_message(parse_raw_message((REPO / r["raw_email_path"]).read_bytes())).subject
    ]


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.0%}"


def _render_md(report: dict[str, Any]) -> str:
    m, agg = report["metadata"], report["aggregate"]
    lines = [f"# Eval report — {m['mode']} — {report['timestamp']}", ""]
    lines += [f"- model `{m['model']}` (digest `{m['model_digest'][:12]}`, {m['model_quantization']}), temperature {m['temperature']}, seed {m['seed']}, runs {m['runs']}",
              f"- commit `{m['git_commit'][:8]}` dirty={m['git_dirty']} untracked={m['git_untracked']}",
              f"- prompt sha `{m['triage_prompt_sha']}`, seed examples `{m['seed_examples_hash']}`, emails {m['n_emails']}",
              f"- consistency: {report['consistency']['identical_emails']}/{m['n_emails']} emails identical across runs", ""]
    lines += ["## High-risk flags (per run)", "",
              "| run | precision | recall | macro recall | failures |", "|---|---|---|---|---|"]
    for i, s in enumerate(report["runs"], 1):
        mi = s["high_risk_micro"]
        lines.append(f"| {i} | {_pct(mi['precision'])} ({mi['hits']}/{mi['predicted']}) | "
                     f"{_pct(mi['recall'])} ({mi['hits']}/{mi['gold']}) | {_pct(s['high_risk_macro_recall'])} | {len(s['failures'])} |")
    lines.append(f"| mean | {_pct(agg['high_risk_precision'])} | {_pct(agg['high_risk_recall'])} | {_pct(agg['high_risk_macro_recall'])} | |")
    lines += ["", "## Per-flag recall (run 1)", "", "| flag | hits / gold | recall | predicted | missed |", "|---|---|---|---|---|"]
    for f, t in report["runs"][0]["high_risk_per_flag"].items():
        lines.append(f"| {f} | {t['hits']}/{t['gold']} | {_pct(t['recall'])} | {t['predicted']} | {', '.join(t['missed_ids'])} |")
    lines += ["", "## Safety violations (per run)", ""]
    for i, s in enumerate(report["runs"], 1):
        v = s["safety_violations"]
        lines.append(f"- run {i}: final_action junk={v['final_action']['junk']} pass={v['final_action']['pass']}; "
                     f"executor quarantine={v['executor']['quarantine']} pass={v['executor']['pass']} "
                     f"(of {v['n_high_risk_emails']} high-risk emails)")
    lines += ["", "## Prompt injection (per run)", ""]
    for i, s in enumerate(report["runs"], 1):
        inj = s["injection"]
        lines.append(f"- run {i}: model all-fields {inj['model_all']}/{inj['n']}, any-field {inj['model_any']}/{inj['n']}, "
                     f"system {inj['system']}/{inj['n']}")
    lines += ["", "## Known limitations", ""] + [f"- {k}" for k in KNOWN_LIMITATIONS]
    return "\n".join(lines) + "\n"


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["baseline"], default="baseline")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    rows = _load_golden()
    client = OllamaClient()
    capture = _RawCapture(client.client)
    client.client = capture
    llm = _PinnedLLM(client, args.temperature, args.seed)
    orch = Orchestrator(
        triage_runner=partial(run_triage, llm_client=llm),
        spam_decider=run_spam_decision,
        draft_agent=_NoDraft(),
    )
    metadata = _metadata(args, client.model, await _model_info(client), len(rows))

    runs: list[list[dict[str, Any]]] = []
    for i in range(args.runs):
        preds = []
        for row in rows:
            preds.append(await _predict(orch, capture, row))
            print(f"run {i + 1}/{args.runs} {row['id']}", flush=True)
        runs.append(preds)

    scored = [_score(rows, preds) for preds in runs]
    report = {
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "metadata": metadata,
        "garbled_subject": _garbled_subjects(rows),
        "runs": scored,
        "aggregate": {
            "high_risk_precision": mean(s["high_risk_micro"]["precision"] or 0 for s in scored),
            "high_risk_recall": mean(s["high_risk_micro"]["recall"] or 0 for s in scored),
            "high_risk_macro_recall": mean(s["high_risk_macro_recall"] or 0 for s in scored),
        },
        "consistency": _consistency(rows, runs),
        "known_limitations": KNOWN_LIMITATIONS,
    }
    out = REPORTS_DIR / f"{report['timestamp'].replace(':', '-').replace('.', '-')}-{args.mode}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    per_email = [{"id": r["id"], "group": r["group"], "expected": r["expected"],
                  "predicted": [run[i] for run in runs]} for i, r in enumerate(rows)]
    (out / "per_email.json").write_text(json.dumps(per_email, indent=2, ensure_ascii=False))
    (out / "report.md").write_text(_render_md(report))
    print(f"\nReport written to {out}")


if __name__ == "__main__":
    asyncio.run(main())
