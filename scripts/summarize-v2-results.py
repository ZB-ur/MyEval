#!/usr/bin/env python3
"""Read-only, reproducible analysis of the six frozen November 18 V2 judge jobs.

SQLite is opened mode=ro with query_only. No service/model calls or DB writes.
Snapshots omit task/output prose and retain identities, scores, coverage and costs.
Replay an exact cutoff with --snapshot <saved snapshot.json>. --watch-seconds 60
refreshes original/recovery-v1 reports until all six groups are terminal.
--typed-receipts reads frozen whole-job receipts as a separate one-shot cohort;
--retry-created accepts explicit quote retry maps, never a latest-result policy.
This script never imports evaluator modules, submits jobs, or imports assessments.
"""
from __future__ import annotations

import argparse
import ast
import csv
import copy
import hashlib
import io
import json
import math
import random
import secrets
import sqlite3
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPERIMENTS = {
    "5d50b4fe-b3ce-456b-99b7-e70e67242ae2": "sidelit_formal",
    "47493b6e-1067-40c1-9563-a89440e391ec": "sijian_formal",
    "b2b5e91f-7c5e-4398-8eee-54db30c8284b": "pome_corrected_v1.0.1_formal",
    "4c34181b-bf13-4e67-a8fd-63b927f542e6": "pome_original_input_error_audit_only",
}
JOBS = {
    "da05fc8e-9fbf-4f06-b53b-6a497c28c921": ("5d50b4fe-b3ce-456b-99b7-e70e67242ae2", "codex-astra-v1"),
    "2af350c9-445e-47ab-8987-e80ba6ee1c33": ("5d50b4fe-b3ce-456b-99b7-e70e67242ae2", "codex-sol-v1"),
    "ceabadd7-a77b-48bb-a171-367722d3294e": ("47493b6e-1067-40c1-9563-a89440e391ec", "codex-astra-v1"),
    "4f1dadb5-22e6-41e0-859e-169f822eefbc": ("47493b6e-1067-40c1-9563-a89440e391ec", "codex-sol-v1"),
    "466739e4-fc27-471a-9b6c-818b4029293b": ("b2b5e91f-7c5e-4398-8eee-54db30c8284b", "codex-astra-v1"),
    "484e127f-77d0-4d77-a944-21d5ce42f977": ("b2b5e91f-7c5e-4398-8eee-54db30c8284b", "codex-sol-v1"),
}
RECOVERY_JOBS = {
    "da05fc8e-9fbf-4f06-b53b-6a497c28c921": "cf662dba-4e31-4587-9636-559054e38e30",
    "2af350c9-445e-47ab-8987-e80ba6ee1c33": "5c1bbebf-fd27-4635-8839-e50122affc28",
    "ceabadd7-a77b-48bb-a171-367722d3294e": "6aeb3195-2d62-4286-9cd7-eabd5a9fe176",
    "466739e4-fc27-471a-9b6c-818b4029293b": "001538d4-f28f-4dfe-aabc-b17f01169c6d",
    "484e127f-77d0-4d77-a944-21d5ce42f977": "999d57af-dc3f-4d78-9c40-9dfb402d0fc9",
}
ACTIVE = {"queued", "running", "cancelling", "committing"}
TOKEN_FIELDS = ("input_tokens", "output_tokens", "total_tokens", "cached_input_tokens", "reasoning_tokens")
TYPED_ENGINE_HASH = "8cfe09ed260dfeec01942e4808686d2d40f0c1da1f9e90cc04ccc4d1d597eedb"
FROZEN_EVALUATOR_HASHES = {
    "codex-astra-v1": "c7dbee9c51aa49bc4e05fc642446fd6620086d3fa1291f8c8bd25b5fbc28d6fc",
    "codex-sol-v1": "dbf16dd85a48e775aa49a37a427fbbb70e1d49bb9eb7bb57883ea1d7edf6094c",
}
FROZEN_RUBRIC_HASHES = {
    "5d50b4fe-b3ce-456b-99b7-e70e67242ae2": "855222c43654595558e62b6eb4583338b3a0c1a17deaa97bcba97d902e5c849a",
    "47493b6e-1067-40c1-9563-a89440e391ec": "aa2634db70baa5a252a0886069aa8757419f0f94fe5697261af44580940526ba",
    "b2b5e91f-7c5e-4398-8eee-54db30c8284b": "f72fde643fd6fb36d401493b11eedf9199d0ebf1d92401a6417594016324dad2",
}
ASSESSMENT_BODY_FIELDS = ("kind", "evaluator", "rubric_version", "reason", "execution_attempt", "result_hash",
    "evaluation_job_id", "idempotency_key", "evaluator_snapshot", "input_hash", "rubric_hash", "value", "evidence", "provenance")
JUDGE_USAGE_FIELDS = ("known_total_tokens", "unknown_units", "model_requests", "dispatch_attempts",
                      "unknown_dispatch_units", "confirmed_model_completions")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def digest(value, *, sorted_keys=False):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                                     sort_keys=sorted_keys, allow_nan=False).encode()).hexdigest()


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def average(values):
    return sum(values) / len(values) if values else None


def connect(path):
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    db.execute("PRAGMA query_only=ON")
    db.execute("BEGIN")
    return db


def normalize_attempt(a):
    result = a.get("result") or {}
    raw = result.get("usage") or {}
    aliases = {"input_tokens": ("input_tokens", "prompt_tokens"),
               "output_tokens": ("output_tokens", "completion_tokens"),
               "total_tokens": ("total_tokens",),
               "cached_input_tokens": ("cached_input_tokens", "prompt_cache_hit_tokens"),
               "reasoning_tokens": ("reasoning_tokens", "reasoning_output_tokens")}
    usage = {k: next((raw[x] for x in names if number(raw.get(x)) and raw[x] >= 0), None)
             for k, names in aliases.items()}
    derived = usage["total_tokens"] is None and all(usage[k] is not None for k in ("input_tokens", "output_tokens"))
    if derived:
        usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
    error = result.get("error") or {}
    cancellation = a.get("cancellation") or result.get("cancellation") or {}
    known_not_sent = cancellation.get("request_sent") is False
    pre_model_state_error = (error.get("code") == "product_http_error" and
                             error.get("endpoint") == "/api/state" and error.get("http_status") == 400)
    return {k: a.get(k) for k in ("id", "attempt", "status", "configuration_id", "case_id", "repeat_index",
                                  "started_at", "ended_at", "duration_ms")} | {
        "result_hash": digest(a.get("result")), "result_status": result.get("status"),
        "case_hash": digest(a.get("case")), "review_output_hash": digest(result.get("review_output")),
        "usage": usage, "total_tokens_derived": derived,
        "usage_complete": raw.get("complete", True), "reported_model_calls": raw.get("calls_requested"),
        "product_duration_ms": (result.get("timing") or {}).get("product_duration_ms"),
        "error_code": error.get("code"), "error_http_status": error.get("http_status"),
        "error_endpoint": error.get("endpoint"), "known_not_sent": known_not_sent,
        "pre_model_state_error": pre_model_state_error,
        "started": a.get("started_at") is not None,
    }


def read_snapshot(data_dir, recovery_manifest=None, retry_entries=None):
    selection = dict(JOBS)
    if recovery_manifest:
        for group in recovery_manifest["groups"]:
            selection[group["recovery_job_id"]] = (group["experiment_id"], group["evaluator_id"])
        chain = recovery_manifest["text_quote_retry_chain"]
        for entry in chain["attempts"]:
            selection[entry["job_id"]] = JOBS[chain["source_job_id"]]
    for entry in retry_entries or []:
        body = entry["request"]
        selection[entry["new_job_id"]] = (body["experiment_id"], body["evaluator_id"])
    snapshot = {"schema_version": "myeval.independent-results-input.v1", "captured_at": now(),
                "analysis_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "protocol_sha256": hashlib.sha256((ROOT / "docs/experiment-protocol-v2.md").read_bytes()).hexdigest(),
                "experiment_roles": EXPERIMENTS, "frozen_job_selection": JOBS, "jobs": [], "experiments": [],
                "read_order": "evaluation snapshot first; core snapshot second; per-record identity reconciliation"}
    # A succeeded evaluation unit is committed after its core assessments. Reading
    # evaluation first avoids an earlier core snapshot losing those accepted IDs.
    db = connect(data_dir / "evaluation" / "evaluation.sqlite3")
    for jid, (eid, evaluator) in selection.items():
        row = db.execute("SELECT body FROM jobs WHERE id=?", (jid,)).fetchone()
        if not row:
            snapshot["jobs"].append({"id": jid, "experiment_id": eid, "evaluator_id": evaluator,
                                     "status": "missing", "units": [], "dimensions": {}})
            continue
        j = json.loads(row[0])
        if j["id"] != jid or j["experiment_id"] != eid or j["evaluator"]["id"] != evaluator or j["evaluator_hash"] != FROZEN_EVALUATOR_HASHES[evaluator] or j["rubric_hash"] != FROZEN_RUBRIC_HASHES[eid]:
            raise ValueError("Physical job differs from the explicit frozen experiment/grader/rubric: " + jid)
        job = {k: j.get(k) for k in ("id", "experiment_id", "status", "created_at", "started_at", "ended_at",
                                      "mode", "unit_count", "evaluator_hash", "rubric_hash", "usage")}
        job.update(evaluator_id=j["evaluator"]["id"], evaluator_model=j["evaluator"]["model"],
                   evaluator_implementation=j["evaluator"].get("implementation"),
                   dimensions={k: v.get("name", k) for k, v in j["rubric"]["dimensions"].items()},
                   rubric_id=j["rubric"]["id"], rubric_version=j["rubric"].get("version"), units=[])
        raw_units = [json.loads(raw[0]) for raw in db.execute("SELECT body FROM units WHERE job_id=? ORDER BY ordinal", (jid,))]
        job["source_job_content_hash"] = digest({"job": j, "units": raw_units}, sorted_keys=True)
        for u in raw_units:
            output = u.get("output") or {}
            unit = {k: u.get(k) for k in ("id", "job_id", "ordinal", "status", "case_id", "family_id", "mapping",
                    "started_at", "ended_at", "accepted_at", "duration_ms", "request_sent", "dispatch_state",
                    "assessment_record_ids", "usage")}
            unit.update(preference=(output.get("preference") or {}).get("choice"),
                        source_unit_hash=digest(u, sorted_keys=True),
                        stored_judgment_hash=digest(u.get("output")), payload_hash=u.get("payload_hash"),
                        confidence=output.get("confidence"), error_code=(u.get("error") or {}).get("code"),
                        validation_issue_codes=[x.get("code") if isinstance(x, dict) else str(x)
                                                for x in u.get("validation_issues", [])], sides={})
            for label, side in (output.get("sides") or {}).items():
                unit["sides"][label] = {"scores": side.get("scores", {}), "score_status": side.get("score_status", {}),
                    "hard_violations": [{k: v.get(k) for k in ("dimension", "severity", "disputed")}
                                        for v in side.get("hard_violations", [])]}
            job["units"].append(unit)
        snapshot["jobs"].append(job)
    db.close(); snapshot["evaluation_read_completed_at"] = now()
    db = connect(data_dir / "myeval.sqlite3")
    records = {}
    for eid, role in EXPERIMENTS.items():
        raw = db.execute("SELECT body FROM experiments WHERE id=?", (eid,)).fetchone()
        if raw is None:
            raise ValueError("Required experiment missing: " + eid)
        e = json.loads(raw[0])
        experiment = {k: e.get(k) for k in ("id", "project", "baseline_id", "repeats", "snapshot_hash", "created_at")}
        experiment.update(role=role, taskpack_id=e["pack"]["id"], taskpack_version=e["pack"].get("version"),
                          configurations=[{k: c.get(k) for k in ("id", "name", "version")} for c in e["configurations"]],
                          cases=[{k: c.get(k) for k in ("id", "family_id", "task", "variant", "split")} for c in e["cases"]],
                          trials=[])
        for row in db.execute("SELECT body FROM trials WHERE experiment_id=? ORDER BY rowid", (eid,)):
            t = json.loads(row[0]); current = normalize_attempt(t)
            current["family_id"] = t["case"].get("family_id", t["case_id"])
            current["task"] = t["case"].get("task")
            current["attempts"] = [normalize_attempt(json.loads(a[0])) for a in db.execute(
                "SELECT body FROM attempts WHERE trial_id=? ORDER BY number", (t["id"],))]
            experiment["trials"].append(current)
        snapshot["experiments"].append(experiment)
        for rid, tid, body in db.execute("SELECT a.id,a.trial_id,a.body FROM assessment_records a JOIN trials t ON t.id=a.trial_id WHERE t.experiment_id=?", (eid,)):
            r = json.loads(body); value = r.get("value") or {}
            records[rid] = {"id": rid, "trial_id": tid,
                           **{k: r.get(k) for k in ("execution_attempt", "result_hash", "evaluation_job_id", "evaluator", "rubric_version")},
                           "unit_id": value.get("unit_id"), "job_id": value.get("job_id"),
                           "body_contract_hash": digest({k: r[k] for k in ASSESSMENT_BODY_FIELDS if k in r}, sorted_keys=True)}
    db.close(); snapshot["core_read_completed_at"] = now(); snapshot["assessment_identities"] = records
    if recovery_manifest:
        snapshot = compose_recovery_snapshot(snapshot, recovery_manifest)
    snapshot["content_hash"] = digest(snapshot, sorted_keys=True)
    return snapshot


def compose_recovery_snapshot(snapshot, manifest):
    claimed = manifest["content_hash"]
    if digest({k: v for k, v in manifest.items() if k != "content_hash"}, sorted_keys=True) != claimed:
        raise ValueError("Immutable recovery map hash differs")
    sources = {j["id"]: j for j in snapshot["jobs"]}; replacements = {}; lineage = defaultdict(set)
    for group in manifest["groups"]:
        old_id, new_id = group["source_job_id"], group["recovery_job_id"]
        old, new = sources[old_id], sources[new_id]
        if any(old[k] != new[k] or old[k] != group[k] for k in ("experiment_id", "evaluator_hash", "rubric_hash")):
            raise ValueError("Recovery source grader or experiment changed")
        new_units = {u["id"]: u for u in new["units"]}
        for pair in group["pairs"]:
            replacements[(old_id, pair["source_unit_id"])] = new_units[pair["recovery_unit_id"]]
        lineage[old_id].add(new_id)
    chain = manifest["text_quote_retry_chain"]
    for attempt in chain["attempts"]:
        if any(sources[attempt["job_id"]][k] != sources[chain["source_job_id"]][k] for k in ("experiment_id", "mode", "evaluator_hash", "rubric_hash")):
            raise ValueError("Explicit Sijian quote retry changed its source grader/rubric")
    replacements[(chain["source_job_id"], chain["source_unit_id"])] = next(
        u for u in sources[chain["selected_job_id"]]["units"] if u["id"] == chain["selected_unit_id"])
    lineage[chain["source_job_id"]].update(x["job_id"] for x in chain["attempts"])
    logical = []
    for jid in JOBS:
        job = copy.deepcopy(sources[jid]); chosen = []
        for original in job["units"]:
            unit = copy.deepcopy(replacements.get((jid, original["id"]), original))
            pair = lambda u: sorted((v["trial_id"], v["execution_attempt"], v["result_hash"]) for v in u["mapping"].values())
            if pair(unit) != pair(original):
                raise ValueError("Selected recovery unit changed pair identity")
            unit["analysis_original_unit_id"] = original["id"]
            unit["analysis_original_job_id"] = jid
            chosen.append(unit)
        job["units"] = chosen
        counts = Counter(u["status"] for u in chosen)
        job["status"] = "running" if any(s in ACTIVE for s in counts) else "succeeded" if set(counts) == {"succeeded"} else "completed_with_errors"
        job["source_job_ids"] = sorted({jid, *lineage[jid]})
        job["source_job_statuses"] = {i: sources[i]["status"] for i in job["source_job_ids"]}
        job["usage"] = {"scope": "all_physical_original_and_retry_jobs_once", "source_job_ids": job["source_job_ids"],
                        **{key: sum((sources[i].get("usage") or {}).get(key, 0) or 0 for i in job["source_job_ids"])
                           for key in ("known_total_tokens", "unknown_units", "model_requests", "dispatch_attempts", "unknown_dispatch_units", "confirmed_model_completions")},
                        "cost": None, "deterministic_revalidation_model_requests": 0}
        logical.append(job)
    snapshot["source_jobs"] = snapshot["jobs"]
    snapshot["jobs"] = logical
    snapshot["validation_cohort"] = "explicit_provider_and_quote_retry_lineage_frozen_validator_v1"
    snapshot["recovery_map_hash"] = claimed
    snapshot["recovery_selection_policy"] = manifest["selection_policy"]
    return snapshot


def pair_identity(unit):
    return sorted((t["trial_id"], t["execution_attempt"], t["result_hash"]) for t in unit["mapping"].values())


def logical_identity(job, unit):
    return {"mode": job["mode"], "original_evaluator_hash": job["evaluator_hash"], "rubric_hash": job["rubric_hash"],
            "targets": [{"trial_id": t, "execution_attempt": a, "result_hash": h} for t, a, h in pair_identity(unit)]}


def load_retry_entries(paths):
    """Read explicit create receipts and their prior plans; never infer a retry from score/time."""
    entries, documents, seen = [], [], set()
    for path in paths:
        created = json.loads(path.read_text())
        plan_path = path.with_name(path.name.replace("-created.json", "-plan.json"))
        if path == plan_path:
            raise ValueError("Retry create receipt must have a corresponding -plan.json")
        plan = json.loads(plan_path.read_text())
        planned = plan.get("units", plan.get("requests", []))
        index = {(e["original"]["job_id"], e["original"]["unit_id"]): e for e in planned}
        actual = {(e["original_job_id"], e["original_unit_id"]) for e in created}
        if len(index) != len(planned) or len(actual) != len(created) or actual != set(index):
            raise ValueError("Retry created source-unit set differs from its complete prior plan")
        for e in created:
            key = (e["original_job_id"], e["original_unit_id"])
            if key in seen:
                raise ValueError("Same physical source unit has multiple planned quote retries")
            seen.add(key); p = index[key]
            expected = p["request"]["body"]
            # Root only changed a display title and made the existing holdout split explicit.
            semantic = lambda body: {k: v for k, v in body.items() if k not in {"title", "split"}}
            if semantic(expected) != semantic(e["request"]) or e["request"].get("split", "holdout") != "holdout":
                raise ValueError("Retry request changed planned targets, grader, rubric or execution settings")
            if p["original"]["logical_unit_hash"] != e["logical_unit_hash"]:
                raise ValueError("Retry created logical identity differs from plan")
            entries.append({k: e[k] for k in ("original_job_id", "original_unit_id", "logical_unit_hash", "new_job_id",
                "new_unit_ids", "required_evaluator_hash", "required_rubric_hash", "request")} |
                {"planned_original_unit_hash": p["original"]["unit_hash"], "plan_file": str(plan_path)})
        documents.append({"created_file": str(path), "created_file_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                          "plan_file": str(plan_path), "plan_file_sha256": hashlib.sha256(plan_path.read_bytes()).hexdigest(),
                          "planned_pairs": len(planned)})
    return entries, documents


def verify_typed_receipts(snapshot, receipt_dir, engine_hash):
    """Reconcile frozen whole-job receipts without importing or executing evaluator modules.

    The owner already ran the frozen CLI verify. Here hashes, complete denominators,
    immutable attempts, unchanged judgments and actual imported bodies are independently
    reconciled. This is receipt analysis, not a new evidence validator or model judge.
    """
    sources = {j["id"]: j for j in snapshot.get("source_jobs", snapshot["jobs"])}
    experiments = {e["id"]: e for e in snapshot["experiments"]}
    core = defaultdict(list)
    for record in snapshot["assessment_identities"].values():
        core[(record.get("evaluation_job_id") or record.get("job_id"), record.get("unit_id"), record["trial_id"])].append(record)
    verified, audits = {}, []
    for jid, job in sorted(sources.items()):
        path = receipt_dir / (jid + ".json")
        if not path.exists():
            audits.append({"source_job_id": jid, "source_status": job["status"], "receipt_status": "missing",
                           "source_unit_count": len(job["units"])})
            continue
        r = json.loads(path.read_text()); source = r["source_snapshot"]
        if r.get("schema_version") != "myeval.evidence-revalidation.v1" or r.get("operation_kind") != "deterministic_revalidation":
            raise ValueError("Unsupported typed receipt: " + jid)
        if digest({k: v for k, v in r.items() if k != "receipt_hash"}, sorted_keys=True) != r["receipt_hash"]:
            raise ValueError("Typed receipt hash mismatch: " + jid)
        engine = r["engine"]
        if engine["hash"] != engine_hash or digest({k: v for k, v in engine.items() if k != "hash"}, sorted_keys=True) != engine_hash:
            raise ValueError("Typed engine differs from explicitly frozen identity: " + jid)
        if digest({k: source[k] for k in ("job", "units", "experiment_binding", "attempt_bindings")}, sorted_keys=True) != r["source_hash"]:
            raise ValueError("Typed source binding hash mismatch: " + jid)
        sj = source["job"]
        content_hash = digest({"job": sj, "units": source["units"]}, sorted_keys=True)
        if content_hash != source["job_content_hash"] or content_hash != job["source_job_content_hash"]:
            raise ValueError("Receipt source differs from current complete job; take a fresh snapshot: " + jid)
        if sj["status"] in ACTIVE or r["original_job_id"] != jid or sj["id"] != jid or source["source_hash"] != r["source_hash"]:
            raise ValueError("Typed source job identity/terminal status mismatch: " + jid)
        if digest(sj["evaluator"]) != job["evaluator_hash"] or digest(sj["rubric"]) != job["rubric_hash"]:
            raise ValueError("Typed evaluator/rubric snapshot hash mismatch: " + jid)
        if r["id"] != "rv1-" + digest({"source_hash": r["source_hash"], "engine_hash": engine_hash}, sorted_keys=True):
            raise ValueError("Typed receipt ID is not deterministic: " + jid)
        ru = {u["original"]["unit_id"]: u for u in r["units"]}
        originals = {u["id"]: u for u in source["units"]}
        live = {u["id"]: u for u in job["units"]}
        if len(ru) != len(r["units"]) or set(ru) != set(originals) or set(ru) != set(live) or len(ru) != sj["unit_count"] or len(ru) != r["unit_count"]:
            raise ValueError("Typed receipt is not complete-job coverage: " + jid)
        if Counter(u["status"] for u in source["units"]) != sj["counts"] or Counter(u["status"] for u in r["units"]) != r["counts"]:
            raise ValueError("Typed source/validation denominator mismatch: " + jid)
        experiment = experiments[job["experiment_id"]]
        eb = source["experiment_binding"]
        if any(eb[k] != experiment[k] for k in ("id", "project", "baseline_id", "snapshot_hash")) or set(eb["configuration_ids"]) != {c["id"] for c in experiment["configurations"]}:
            raise ValueError("Typed experiment freeze differs: " + jid)
        trial_index = {t["id"]: t for t in experiment["trials"]}
        bindings = {}
        for binding in source["attempt_bindings"]:
            key = (binding["trial_id"], binding["execution_attempt"])
            trial = trial_index.get(binding["trial_id"])
            attempt = next((a for a in trial["attempts"] if a["attempt"] == binding["execution_attempt"]), None) if trial else None
            if key in bindings or attempt is None:
                raise ValueError("Typed duplicate/missing immutable attempt: " + jid)
            if any(attempt[k] != binding[k] for k in ("result_hash", "case_hash", "review_output_hash", "status", "case_id", "configuration_id", "repeat_index")):
                raise ValueError("Typed immutable attempt/output identity changed: " + jid)
            if digest(binding["case"]) != binding["case_hash"] or digest(binding["review_output"]) != binding["review_output_hash"]:
                raise ValueError("Typed case/review payload hash mismatch: " + jid)
            if binding["family_id"] != trial["family_id"] or binding["experiment_id"] != experiment["id"]:
                raise ValueError("Typed family or experiment binding differs: " + jid)
            bindings[key] = binding
        used = {(t["trial_id"], t["execution_attempt"]) for u in originals.values() for t in u["mapping"].values()}
        if used != set(bindings):
            raise ValueError("Typed attempt binding denominator mismatch: " + jid)
        for unit in originals.values():
            for target in unit["mapping"].values():
                binding = bindings[(target["trial_id"], target["execution_attempt"])]
                if any(target[k] != binding[k] for k in ("trial_id", "execution_attempt", "case_id", "family_id", "configuration_id", "repeat_index", "status", "result_hash", "review_output_hash")):
                    raise ValueError("Typed source pair target differs from immutable attempt binding: " + jid)
        if digest(r["assessment_items"], sorted_keys=True) != r["assessment_plan_hash"] or len(r["assessment_items"]) != r["assessment_count"]:
            raise ValueError("Typed assessment plan hash/count differs: " + jid)
        items = defaultdict(list)
        for item in r["assessment_items"]:
            items[item["body"]["value"]["original_unit_id"]].append(item)
        if set(items) != {uid for uid, u in ru.items() if u["status"] == "valid"}:
            raise ValueError("Typed assessments do not cover exactly all valid units: " + jid)
        incremental = r["usage"]
        if any(incremental.get(k) != 0 for k in ("model_requests", "dispatch_attempts", "known_total_tokens", "total_tokens", "cost")) or engine.get("model_calls") != 0:
            raise ValueError("Deterministic validation unexpectedly claims new model usage: " + jid)
        if incremental["original_job_usage_reference"] != {"job_id": jid, "usage_hash": digest(sj.get("usage"), sorted_keys=True), "count_again": False}:
            raise ValueError("Typed original usage reference mismatch: " + jid)
        entries, transitions, imports = {}, Counter(), Counter()
        for uid, unit in ru.items():
            original, proof = originals[uid], unit["original"]
            output = original.get("output"); provider = original.get("provider") or {}
            logical = logical_identity(sj, original)
            eligible = original["status"] in {"succeeded", "invalid"} and provider.get("status") == "succeeded"
            if proof["job_id"] != jid or proof["unit_hash"] != live[uid]["source_unit_hash"] or proof["unit_hash"] != digest(original, sorted_keys=True):
                raise ValueError("Typed unit source identity/hash differs: " + uid)
            if proof["logical_unit_identity"] != logical or proof["logical_unit_hash"] != digest(logical, sorted_keys=True):
                raise ValueError("Typed logical pair identity differs: " + uid)
            if proof["mapping"] != original["mapping"] or proof["evaluator_hash"] != job["evaluator_hash"] or proof["rubric_hash"] != job["rubric_hash"] or proof["payload_hash"] != original["payload_hash"]:
                raise ValueError("Typed pair/grader/payload mismatch: " + uid)
            if proof["normalized_response_hash"] != (digest(output, sorted_keys=True) if output is not None else None) or proof["raw_response_hash"] != (digest(provider["output"], sorted_keys=True) if provider.get("output") is not None else None):
                raise ValueError("Typed original response hash differs: " + uid)
            validation = unit["validation"]
            expected_status = "no_response" if output is None else "ineligible" if not eligible else "valid" if validation["accepted"] else "invalid"
            if unit["eligible_for_assessment"] != eligible or unit["status"] != expected_status:
                raise ValueError("Typed status reverses an ineligible response or missing request: " + uid)
            if validation is not None and validation["original_judgment"] != output:
                raise ValueError("Typed revalidation changed original score/reason/preference: " + uid)
            if validation is not None and validation["accepted"] != (not validation["validation_issues"]):
                raise ValueError("Typed acceptance contradicts validation issues: " + uid)
            transitions[original["status"] + "→" + unit["status"]] += 1
            record_ids, found_count, item_count = [], 0, len(items[uid])
            if item_count != (2 if unit["status"] == "valid" else 0):
                raise ValueError("Typed valid pair does not contain exactly two assessment items: " + uid)
            seen_labels = set()
            for item in items[uid]:
                body, value = item["body"], item["body"]["value"]
                label = next((label for label, target in original["mapping"].items() if target["trial_id"] == item["trial_id"]), None)
                if label is None or label in seen_labels:
                    raise ValueError("Typed pair side duplicated/missing: " + uid)
                seen_labels.add(label); target = original["mapping"][label]; side = output["sides"][label]
                if any(body[k] != target[k] for k in ("execution_attempt", "result_hash")) or body["evaluation_job_id"] != r["id"] or body["idempotency_key"] != uid + ":" + label:
                    raise ValueError("Typed assessment attempt/idempotency identity differs: " + uid)
                if body["reason"] != side["reason"] or body["evidence"] != side["evidence"] or any(value[k] != side[k] for k in ("scores", "score_status", "hard_violations")) or value["confidence"] != output["confidence"]:
                    raise ValueError("Typed assessment modified the original judgment: " + uid)
                other_label = next(l for l in original["mapping"] if l != label)
                other = original["mapping"][other_label]; choice = output["preference"]["choice"]
                expected_preference = {"unit_id": r["id"] + ":" + uid, "choice": "self" if choice == label else "other" if choice == other_label else choice,
                    "reason": output["preference"]["reason"], "other_trial_id": other["trial_id"], "other_attempt": other["execution_attempt"], "other_result_hash": other["result_hash"]}
                if value["preference"] != expected_preference or value["original_job_id"] != jid or value["original_unit_id"] != uid or value["unit_id"] != r["id"] + ":" + uid:
                    raise ValueError("Typed pair preference/context changed: " + uid)
                if value["original_evaluator_hash"] != job["evaluator_hash"] or value["rubric_hash"] != job["rubric_hash"] or value["payload_hash"] != original["payload_hash"] or value["revalidation_engine_hash"] != engine_hash or value["new_model_requests"] != 0:
                    raise ValueError("Typed assessment grader/engine/payload differs: " + uid)
                es = body["evaluator_snapshot"]
                if es["original_evaluator"] != sj["evaluator"] or es["revalidation"] != engine or digest(es) != value["evaluator_hash"] or body["provenance"]["original"] != proof:
                    raise ValueError("Typed derived evaluator/provenance differs: " + uid)
                for key in ("typed_evidence", "transform_log"):
                    if value[key] != [entry for entry in validation[key] if entry["side"] == label]:
                        raise ValueError("Typed transformed evidence differs from verified receipt: " + uid)
                matches = core[(r["id"], value["unit_id"], item["trial_id"])]
                if len(matches) > 1 or any(m["body_contract_hash"] != digest(body, sorted_keys=True) for m in matches):
                    raise ValueError("Actual core typed record differs or is duplicated: " + uid)
                if matches:
                    found_count += 1; record_ids.append(matches[0]["id"])
            import_status = "not_applicable" if not item_count else "confirmed" if found_count == 2 else "not_imported" if found_count == 0 else "partial_pair_import"
            imports[import_status] += 1
            entries[uid] = {"receipt_id": r["id"], "receipt_hash": r["receipt_hash"], "engine_hash": engine_hash,
                "status": unit["status"], "original_status": original["status"], "logical_unit_hash": proof["logical_unit_hash"],
                "verification": "whole_job_hashes_attempts_and_unchanged_judgment_reconciled", "source_job_id": jid,
                "core_import_status": import_status, "assessment_record_ids": sorted(record_ids),
                "validation_issue_codes": sorted({i["code"] for i in (validation or {}).get("validation_issues", [])})}
        verified[jid] = entries
        audits.append({"source_job_id": jid, "source_status": job["status"], "receipt_status": "verified",
            "receipt_file": str(path), "receipt_id": r["id"], "receipt_hash": r["receipt_hash"], "source_hash": r["source_hash"],
            "engine_hash": engine_hash, "source_unit_count": len(ru), "counts": r["counts"], "transitions": dict(transitions),
            "core_import_pairs": dict(imports), "assessment_count": r["assessment_count"],
            "new_model_requests": 0, "judgments_unchanged": True})
    return verified, audits


def compose_typed_snapshot(snapshot, receipt_dir, retry_entries, retry_documents, engine_hash):
    verified, audits = verify_typed_receipts(snapshot, receipt_dir, engine_hash)
    sources = {j["id"]: j for j in snapshot.get("source_jobs", snapshot["jobs"])}
    units = {(j["id"], u["id"]): u for j in sources.values() for u in j["units"]}
    replacements, map_entries = {}, []
    logical_parent = {(u["job_id"], u["id"]): j["id"] for j in snapshot["jobs"] for u in j["units"]}
    for entry in retry_entries:
        key = (entry["original_job_id"], entry["original_unit_id"])
        if key not in logical_parent or len(entry["new_unit_ids"]) != 1:
            raise ValueError("Explicit quote retry is not exactly one currently selected logical pair")
        old, new = sources[key[0]], sources[entry["new_job_id"]]
        old_unit = units[key]
        new_unit = units[(new["id"], entry["new_unit_ids"][0])]
        if len(new["units"]) != 1 or pair_identity(old_unit) != pair_identity(new_unit):
            raise ValueError("Actual quote retry pair multiset differs from source")
        if any(old[k] != new[k] for k in ("experiment_id", "mode", "evaluator_hash", "rubric_hash")) or old["evaluator_hash"] != entry["required_evaluator_hash"] or old["rubric_hash"] != entry["required_rubric_hash"]:
            raise ValueError("Quote retry changed frozen grader/rubric/experiment")
        if old_unit["source_unit_hash"] != entry["planned_original_unit_hash"] or digest(logical_identity(old, old_unit), sorted_keys=True) != entry["logical_unit_hash"]:
            raise ValueError("Quote retry plan source identity/hash differs")
        source_validation = verified.get(key[0], {}).get(key[1])
        if source_validation is None or source_validation["status"] != "invalid":
            raise ValueError("Quote retry source is not invalid in the frozen whole-job receipt")
        requested = sorted((t["trial_id"], t["execution_attempt"], t["result_hash"]) for t in entry["request"]["targets"])
        if requested != pair_identity(new_unit):
            raise ValueError("Created quote retry contains targets outside the exact planned pair")
        replacements[key] = new_unit
        map_entries.append({"logical_original_job_id": logical_parent[key], "source_job_id": key[0], "source_unit_id": key[1],
            "source_unit_hash": old_unit["source_unit_hash"], "source_validation_receipt_id": source_validation["receipt_id"],
            "source_validation_receipt_hash": source_validation["receipt_hash"], "retry_job_id": new["id"], "retry_unit_id": new_unit["id"],
            "logical_unit_hash": entry["logical_unit_hash"], "pair_identity": [list(target) for target in requested], "evaluator_hash": new["evaluator_hash"],
            "rubric_hash": new["rubric_hash"], "experiment_id": new["experiment_id"], "selection": "one_explicit_whole_pair_retry_regardless_of_score_or_status"})
    retry_map = {"schema_version": "myeval.explicit-typed-quote-retry-map.v1", "engine_hash": engine_hash,
        "provider_recovery_map_hash": snapshot.get("recovery_map_hash"), "source_documents": retry_documents,
        "quote_retry_pairs": len(map_entries), "pairs": sorted(map_entries, key=lambda e: (e["source_job_id"], e["source_unit_id"])),
        "selection_policy": "Fixed created jobs only; never latest import, highest score, or fallback to old judgment. Each invalid quote gets one full-pair retry. Still invalid stays missing."}
    retry_map["content_hash"] = digest(retry_map, sorted_keys=True)
    logical = []
    for original_job in snapshot["jobs"]:
        job = copy.deepcopy(original_job); chosen, source_ids = [], set(job.get("source_job_ids") or [job["id"]])
        for previous in job["units"]:
            chosen_unit = replacements.get((previous["job_id"], previous["id"]), previous)
            unit = copy.deepcopy(chosen_unit); source_ids.add(unit["job_id"])
            unit["analysis_original_unit_id"] = previous.get("analysis_original_unit_id", previous["id"])
            unit["analysis_original_job_id"] = job["id"]
            proof = verified.get(unit["job_id"], {}).get(unit["id"])
            unit["physical_unit_status"] = unit["status"]
            if proof:
                unit["typed_revalidation"] = proof
                unit["status"] = "typed_" + proof["status"]
                unit["validation_issue_codes"] = proof["validation_issue_codes"]
            elif unit["status"] not in ACTIVE:
                unit["status"] = "awaiting_typed_receipt"
            chosen.append(unit)
        job["units"] = chosen
        statuses = {u["status"] for u in chosen}
        job["status"] = "running" if statuses & (ACTIVE | {"awaiting_typed_receipt"}) else "succeeded" if statuses == {"typed_valid"} else "completed_with_errors"
        job["source_job_ids"] = sorted(source_ids)
        job["source_job_statuses"] = {jid: sources[jid]["status"] for jid in sorted(source_ids)}
        job["usage"] = {"scope": "all_physical_original_and_explicit_retry_jobs_once", "source_job_ids": sorted(source_ids),
            **{key: sum((sources[jid].get("usage") or {}).get(key, 0) or 0 for jid in source_ids) for key in JUDGE_USAGE_FIELDS},
            "cost": None, "deterministic_revalidation_model_requests": 0}
        logical.append(job)
    seen_jobs = [jid for j in logical for jid in j["source_job_ids"]]
    if len(seen_jobs) != len(set(seen_jobs)) or set(seen_jobs) != set(sources):
        raise ValueError("Physical judge job denominator is duplicated or outside the fixed lineage")
    seen_units = [u["id"] for j in sources.values() for u in j["units"]]
    if len(seen_units) != len(set(seen_units)):
        raise ValueError("Physical judge unit IDs are duplicated across source jobs")
    snapshot["source_jobs"] = list(sources.values()); snapshot["jobs"] = logical
    snapshot["validation_cohort"] = "whole_job_typed_fixed_retry_lineage" if snapshot.get("recovery_map_hash") else "whole_job_typed_original_six_only"
    snapshot["typed_engine_hash"] = engine_hash; snapshot["typed_receipt_audit"] = audits
    snapshot["typed_receipt_read_completed_at"] = now()
    snapshot["typed_retry_map"] = retry_map
    snapshot["judge_usage_audit"] = {"physical_jobs": len(sources), "physical_units": sum(len(j["units"]) for j in sources.values()),
        "each_physical_job_unit_counted_once": True, "deterministic_revalidation_model_requests": 0,
        **{key: sum((j.get("usage") or {}).get(key, 0) or 0 for j in sources.values()) for key in JUDGE_USAGE_FIELDS}}
    snapshot["content_hash"] = digest({k: v for k, v in snapshot.items() if k != "content_hash"}, sorted_keys=True)
    return snapshot


def percentile(values, q):
    p = (len(values) - 1) * q; lo = int(p); hi = min(lo + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (p - lo)


def bootstrap(values, key, replicates):
    if len(values) < 2:
        return None
    if min(values) == max(values):
        return [values[0], values[0]]
    rng = random.Random(int(digest(["family-bootstrap-v1", key], sorted_keys=True)[:16], 16))
    samples = sorted(sum(rng.choices(values, k=len(values))) / len(values) for _ in range(replicates))
    return [percentile(samples, .025), percentile(samples, .975)]


def effect(observations, cases, repeats, key, replicates, missing_delta_bound=None):
    """Rows contain family/case/repeat/baseline/candidate; no pooled judges."""
    expected = defaultdict(set); family_cases = defaultdict(set)
    for case in cases:
        family = case.get("family_id") or case["id"]
        family_cases[family].add(case["id"])
        expected[family].update((case["id"], r) for r in range(1, repeats + 1))
    observed = defaultdict(lambda: defaultdict(list))
    for row in observations:
        observed[row["family_id"]][row["case_id"]].append(row)
    rows = []
    for family in sorted(expected):
        by_case = observed.get(family, {})
        case_rows = []
        for cid in sorted(family_cases[family]):
            values = by_case.get(cid, [])
            case_rows.append({"case_id": cid, "valid_pairs": len(values), "planned_pairs": repeats,
                              "baseline": average([v["baseline"] for v in values]),
                              "candidate": average([v["candidate"] for v in values]),
                              "delta": average([v["candidate"] - v["baseline"] for v in values])})
        present = [r for r in case_rows if r["valid_pairs"]]
        identities = {(r["case_id"], r["repeat_index"]) for rows_ in by_case.values() for r in rows_}
        rows.append({"family_id": family, "planned_pairs": len(expected[family]), "valid_pairs": len(identities),
                     "complete": identities == expected[family], "cases": case_rows,
                     "baseline": average([r["baseline"] for r in present]),
                     "candidate": average([r["candidate"] for r in present]),
                     "delta": average([r["delta"] for r in present])})
    def summary(values, suffix):
        differences = [r["delta"] for r in values]
        b = average([r["baseline"] for r in values]); c = average([r["candidate"] for r in values])
        return {"n_families": len(values), "baseline": b, "candidate": c, "mean_delta": average(differences),
                "ci95": bootstrap(differences, [key, suffix], replicates),
                "relative_delta": (c - b) / b if b not in (None, 0) else None,
                "family_win_tie_loss": dict(Counter("win" if x > 0 else "loss" if x < 0 else "tie" for x in differences))}
    sensitivity = None
    if missing_delta_bound is not None:
        bounds = []
        for family in sorted(expected):
            case_bounds = []
            for cid in sorted(family_cases[family]):
                values = observed.get(family, {}).get(cid, [])
                known = sum(v["candidate"] - v["baseline"] for v in values)
                missing = repeats - len(values)
                case_bounds.append(((known - missing * missing_delta_bound) / repeats,
                                    (known + missing * missing_delta_bound) / repeats))
            bounds.append((average([b[0] for b in case_bounds]), average([b[1] for b in case_bounds])))
        sensitivity = [average([b[0] for b in bounds]), average([b[1] for b in bounds])]
    return {"planned_families": len(expected), "planned_pairs": sum(map(len, expected.values())),
            "valid_pairs": len(observations), "complete_families": summary([r for r in rows if r["complete"]], "complete"),
            "observed_families": summary([r for r in rows if r["valid_pairs"]], "observed"), "families": rows,
            "full_planned_cohort_missingness_bounds": sensitivity}


def distribution(values):
    known = sorted(x for x in values if number(x))
    return {"expected": len(values), "known": len(known), "missing": len(values) - len(known),
            "known_total": sum(known) if known else None, "mean": average(known),
            "p50": known[math.ceil(.50 * len(known)) - 1] if known else None,
            "p95": known[math.ceil(.95 * len(known)) - 1] if known else None}


def attempt_cost(trial, metric):
    values = []
    for a in trial["attempts"]:
        if a["known_not_sent"]:
            continue
        if metric != "duration_ms" and a["pre_model_state_error"]:
            continue
        value = a.get("duration_ms") if metric == "duration_ms" else a["usage"].get(metric)
        if not number(value) or (metric != "duration_ms" and a["usage_complete"] is False):
            return None
        values.append(value)
    return sum(values)


def execution(experiment, replicates):
    trials = experiment["trials"]; configurations = {}
    for config in experiment["configurations"]:
        ts = [t for t in trials if t["configuration_id"] == config["id"]]
        attempts = [a for t in ts for a in t["attempts"]]
        started = [a for a in attempts if a["started"]]
        first_started = [next((a for a in t["attempts"] if a["started"]), None) for t in ts]
        model_cost_attempts = [a for a in attempts if not a["known_not_sent"] and not a["pre_model_state_error"]]
        configurations[config["id"]] = {"name": config["name"], "trials": len(ts),
            "planned_trials": len(experiment["cases"]) * experiment["repeats"],
            "current_statuses": dict(Counter(t["status"] for t in ts)),
            "first_recorded_attempt_statuses": dict(Counter(t["attempts"][0]["status"] for t in ts if t["attempts"])),
            "first_started_attempt_statuses": dict(Counter(a["status"] if a else "never_started" for a in first_started)),
            "all_attempt_statuses": dict(Counter(a["status"] for a in attempts)),
            "attempt_count": len(attempts), "started_attempt_count": len(started),
            "known_not_sent_attempts": sum(a["known_not_sent"] for a in attempts),
            "pre_model_state_error_attempts": sum(a["pre_model_state_error"] for a in attempts),
            "retries": sum(max(0, len(t["attempts"]) - 1) for t in ts),
            "errors": dict(Counter(a["error_code"] for a in attempts if a["error_code"])),
            "all_started_latency_ms": distribution([a["duration_ms"] for a in started]),
            "current_success_latency_ms": distribution([t["duration_ms"] for t in ts if t["status"] == "succeeded"]),
            "all_attempt_tokens": {m: distribution([a["usage"].get(m) for a in model_cost_attempts]) for m in TOKEN_FIELDS},
            "partial_usage_attempts": sum(a["usage_complete"] is False for a in model_cost_attempts),
            "current_success_tokens": {m: distribution([t["usage"].get(m) if t["usage_complete"] is not False else None
                                                          for t in ts if t["status"] == "succeeded"]) for m in TOKEN_FIELDS}}
    index = {(t["configuration_id"], t["case_id"], t["repeat_index"]): t for t in trials}
    comparisons = {}
    for config in experiment["configurations"]:
        cid = config["id"]
        if cid == experiment["baseline_id"]:
            continue
        metrics = defaultdict(list)
        for case in experiment["cases"]:
            family = case.get("family_id") or case["id"]
            for repeat in range(1, experiment["repeats"] + 1):
                b = index.get((experiment["baseline_id"], case["id"], repeat)); c = index.get((cid, case["id"], repeat))
                if not b or not c:
                    continue
                for scope in ("current_success", "first_recorded_success", "first_started_success"):
                    if scope == "current_success":
                        ba, ca = b, c
                    elif scope == "first_recorded_success":
                        ba = b["attempts"][0] if b["attempts"] else None
                        ca = c["attempts"][0] if c["attempts"] else None
                    else:
                        ba = next((a for a in b["attempts"] if a["started"]), None)
                        ca = next((a for a in c["attempts"] if a["started"]), None)
                    if ba is not None and ca is not None:
                        metrics[scope].append({"family_id": family, "case_id": case["id"], "repeat_index": repeat,
                                               "baseline": int(ba["status"] == "succeeded"),
                                               "candidate": int(ca["status"] == "succeeded")})
                for metric in (*TOKEN_FIELDS, "duration_ms"):
                    for scope in ("latest_success", "all_attempts"):
                        if scope == "latest_success":
                            if b["status"] != "succeeded" or c["status"] != "succeeded":
                                continue
                            bv = b.get(metric) if metric == "duration_ms" else b["usage"].get(metric)
                            cv = c.get(metric) if metric == "duration_ms" else c["usage"].get(metric)
                            if metric != "duration_ms" and (b["usage_complete"] is False or c["usage_complete"] is False):
                                continue
                        else:
                            bv, cv = attempt_cost(b, metric), attempt_cost(c, metric)
                        if number(bv) and number(cv):
                            metrics[scope + "." + metric].append({"family_id": family, "case_id": case["id"],
                                "repeat_index": repeat, "baseline": bv, "candidate": cv})
        comparisons[cid] = {m: effect(obs, experiment["cases"], experiment["repeats"],
                                     [experiment["id"], cid, "performance", m], replicates) for m, obs in metrics.items()}
    return {"configurations": configurations, "paired_efficiency": comparisons}


def validate_unit(unit, job, experiment, trial_index, assessment_index):
    problems = []
    mapping = unit.get("mapping") or {}
    if len(mapping) != 2:
        return ["not_two_sides"]
    sides = list(mapping.values())
    if len({x.get("configuration_id") for x in sides}) != 2 or experiment["baseline_id"] not in {x.get("configuration_id") for x in sides}:
        problems.append("not_baseline_candidate_pair")
    if len({(x.get("case_id"), x.get("repeat_index"), x.get("family_id")) for x in sides}) != 1:
        problems.append("case_repeat_family_mismatch")
    for target in sides:
        t = trial_index.get(target.get("trial_id"))
        if t is None or (target.get("execution_attempt"), target.get("result_hash")) != (t["attempt"], t["result_hash"]):
            problems.append("not_current_attempt_hash")
        elif t["status"] != "succeeded":
            problems.append("product_not_succeeded")
    if unit["status"] == "succeeded":
        if not unit.get("accepted_at"):
            problems.append("acceptance_not_persisted")
        ids = unit.get("assessment_record_ids") or []
        records = [assessment_index.get(x) for x in ids]
        if len(ids) != 2 or len(set(ids)) != 2 or any(r is None for r in records):
            problems.append("atomic_core_records_missing")
        else:
            for target in sides:
                matched = [r for r in records if r["trial_id"] == target["trial_id"] and
                           r["execution_attempt"] == target["execution_attempt"] and r["result_hash"] == target["result_hash"] and
                           (r.get("evaluation_job_id") or r.get("job_id")) == unit["job_id"] and r.get("unit_id") == unit["id"]]
                if len(matched) != 1:
                    problems.append("core_identity_or_context_mismatch")
    if unit["status"] == "typed_valid":
        proof = unit.get("typed_revalidation") or {}
        if proof.get("verification") != "whole_job_hashes_attempts_and_unchanged_judgment_reconciled" or proof.get("status") != "valid":
            problems.append("typed_whole_job_verification_missing")
    return sorted(set(problems))


def quality(job, experiment, assessment_index, replicates):
    trial_index = {t["id"]: t for t in experiment["trials"]}
    candidates = [c["id"] for c in experiment["configurations"] if c["id"] != experiment["baseline_id"]]
    grouped = defaultdict(list); audit = []; valid_units = {}
    for u in job["units"]:
        labels = {x.get("configuration_id"): label for label, x in (u.get("mapping") or {}).items()}
        cid = next((c for c in candidates if c in labels), None)
        issues = validate_unit(u, job, experiment, trial_index, assessment_index)
        row = {"unit_id": u["id"], "candidate_id": cid, "case_id": u.get("case_id"), "family_id": u.get("family_id"),
               "source_job_id": u["job_id"], "analysis_original_unit_id": u.get("analysis_original_unit_id", u["id"]),
               "status": u["status"], "identity_issues": issues, "error_code": u.get("error_code"),
               "validation_issue_codes": u.get("validation_issue_codes"), "confidence": u.get("confidence"),
               "physical_unit_status": u.get("physical_unit_status", u["status"]),
               "typed_revalidation": u.get("typed_revalidation")}
        audit.append(row)
        if cid:
            grouped[cid].append((u, labels, issues))
    comparisons = {}
    for cid in candidates:
        entries = grouped[cid]; duplicates = Counter()
        for u, labels, _ in entries:
            target = u["mapping"][labels[cid]]
            duplicates[(target["case_id"], target["repeat_index"])] += 1
        observations = defaultdict(list); missing = defaultdict(Counter); severe = Counter(); severe_families = defaultdict(set)
        severe_dimensions = defaultdict(Counter); severe_dimension_families = defaultdict(lambda: defaultdict(set))
        preference_counts = Counter(); good_keys = set(); dimension_names = list(job["dimensions"])
        planned = len(experiment["cases"]) * experiment["repeats"]
        for u, labels, issues in entries:
            target = u["mapping"][labels[cid]]; key = (target["case_id"], target["repeat_index"])
            if u["status"] not in {"succeeded", "typed_valid"} or issues or duplicates[key] != 1:
                continue
            b = u["sides"].get(labels[experiment["baseline_id"]], {}); c = u["sides"].get(labels[cid], {})
            base = {"family_id": target["family_id"], "case_id": target["case_id"], "repeat_index": target["repeat_index"]}
            good_keys.add(key)
            choice = u.get("preference")
            preference = "win" if choice == labels[cid] else "loss" if choice == labels[experiment["baseline_id"]] else choice
            preference_counts[preference or "missing"] += 1
            if preference in {"win", "loss", "tie"}:
                score = {"win": 1, "loss": -1, "tie": 0}[preference]
                observations["pairwise_preference"].append(base | {"baseline": 0, "candidate": score})
            scores = {}
            for dimension in dimension_names:
                bs, cs = b.get("score_status", {}).get(dimension), c.get("score_status", {}).get(dimension)
                bv, cv = b.get("scores", {}).get(dimension), c.get("scores", {}).get(dimension)
                if bs == cs == "rated" and number(bv) and number(cv):
                    observations[dimension].append(base | {"baseline": bv, "candidate": cv}); scores[dimension] = cv - bv
                else:
                    missing[dimension][str(bs) + "/" + str(cs)] += 1
            flags = {}; flagged_dimensions = {}
            for label, side in (("baseline", b), ("candidate", c)):
                violations = [v for v in side.get("hard_violations", []) if v.get("severity") in {"major", "critical"}]
                flags[label] = bool(violations)
                flagged_dimensions[label] = {v.get("dimension") for v in violations}
                severe[label + "_units"] += bool(violations)
                for v in violations:
                    severe[label + ":" + str(v.get("dimension")) + ":" + str(v.get("severity"))] += 1
                if violations:
                    severe_families[label].add(target["family_id"])
            severe["candidate_only_units"] += flags["candidate"] and not flags["baseline"]
            if flags["candidate"] and not flags["baseline"]:
                severe_families["candidate_only"].add(target["family_id"])
            for dimension in flagged_dimensions["baseline"] | flagged_dimensions["candidate"]:
                bf = dimension in flagged_dimensions["baseline"]; cf = dimension in flagged_dimensions["candidate"]
                for kind, present in (("baseline", bf), ("candidate", cf), ("both", bf and cf),
                                      ("candidate_only", cf and not bf), ("baseline_only", bf and not cf)):
                    severe_dimensions[dimension][kind + "_units"] += bool(present)
                    if present:
                        severe_dimension_families[dimension][kind].add(target["family_id"])
            valid_units[(cid, *key)] = base | {"unit_id": u["id"], "preference": preference,
                "deltas": scores, "major_critical": flags,
                "identities": sorted((v["trial_id"], v["execution_attempt"], v["result_hash"]) for v in u["mapping"].values())}
        metrics = {m: effect(observations[m], experiment["cases"], experiment["repeats"],
                            [job["id"], cid, m], replicates, 1 if m == "pairwise_preference" else 2)
                   for m in ["pairwise_preference", *dimension_names]}
        for dimension, statuses in missing.items():
            if any("not_applicable" in state for state in statuses):
                metrics[dimension]["full_planned_cohort_missingness_bounds"] = None
                metrics[dimension]["bounds_unavailable_reason"] = "not_applicable_prevents_a_numeric_all_planned_pairs_estimand"
        slices = {}
        tasks = sorted({c.get("task") or "unknown" for c in experiment["cases"]})
        if len(tasks) > 1:
            for task in tasks:
                cases = [c for c in experiment["cases"] if (c.get("task") or "unknown") == task]
                cids = {c["id"] for c in cases}
                slices[task] = {m: effect([o for o in observations[m] if o["case_id"] in cids], cases,
                                          experiment["repeats"], [job["id"], cid, task, m], replicates,
                                          1 if m == "pairwise_preference" else 2) for m in metrics}
                for dimension, statuses in missing.items():
                    if any("not_applicable" in state for state in statuses):
                        slices[task][dimension]["full_planned_cohort_missingness_bounds"] = None
        comparisons[cid] = {"planned_pairs": planned, "unit_statuses": dict(Counter(u["status"] for u, _, _ in entries)),
            "missing_unit_rows": max(0, planned - len(entries)), "duplicate_pair_keys": sum(v > 1 for v in duplicates.values()),
            "identity_rejected_units": sum(bool(issues) for _, _, issues in entries),
            "valid_accepted_pairs": len(good_keys), "preference_unit_counts": dict(preference_counts),
            "validity_basis": "verified_whole_job_typed_receipt" if any(u.get("typed_revalidation") for u, _, _ in entries) else "original_atomic_core_acceptance",
            "typed_core_import_statuses": dict(Counter((u.get("typed_revalidation") or {}).get("core_import_status", "no_receipt") for u, _, _ in entries)),
            "nonrated_dimension_statuses": {k: dict(v) for k, v in missing.items()}, "metrics": metrics, "task_slices": slices,
            "major_critical_flags": dict(severe), "major_critical_families": {k: sorted(v) for k, v in severe_families.items()},
            "major_critical_paired_dimensions": {d: {"counts": dict(counts),
                "families": {k: sorted(v) for k, v in severe_dimension_families[d].items()}}
                for d, counts in sorted(severe_dimensions.items())}}
    return {k: job.get(k) for k in ("id", "experiment_id", "evaluator_id", "evaluator_model", "status", "evaluator_hash", "rubric_hash", "dimensions", "usage", "source_job_ids", "source_job_statuses")} | {
        "unit_statuses": dict(Counter(u["status"] for u in job["units"])), "comparisons": comparisons,
        "unit_audit": audit}, valid_units


def disagreement(experiment, job_results, valid, replicates):
    if len(job_results) != 2:
        return {"available": False, "reason": "two_frozen_judges_required"}
    left, right = sorted(job_results, key=lambda j: j["evaluator_id"])
    a, b = valid[left["id"]], valid[right["id"]]; comparisons = {}
    for config in experiment["configurations"]:
        cid = config["id"]
        if cid == experiment["baseline_id"]:
            continue
        common = sorted(k for k in a.keys() & b.keys() if k[0] == cid and a[k]["identities"] == b[k]["identities"])
        counts = Counter(); dimension_counts = defaultdict(Counter); observations = defaultdict(list)
        for k in common:
            x, y = a[k], b[k]
            if x["preference"] in {"win", "tie", "loss"} and y["preference"] in {"win", "tie", "loss"}:
                counts["comparable"] += 1; counts["agreement"] += x["preference"] == y["preference"]
                counts["opposite_win_loss"] += {x["preference"], y["preference"]} == {"win", "loss"}
                counts["tie_vs_direction"] += (x["preference"] == "tie") != (y["preference"] == "tie")
            for dim in x["deltas"].keys() & y["deltas"].keys():
                av, bv = x["deltas"][dim], y["deltas"][dim]
                dimension_counts[dim]["comparable"] += 1
                dimension_counts[dim]["same_direction"] += (av > 0) - (av < 0) == (bv > 0) - (bv < 0)
                dimension_counts[dim]["opposite_direction"] += av * bv < 0
                observations[dim].append({k: x[k] for k in ("family_id", "case_id", "repeat_index")} |
                                         {"baseline": bv, "candidate": av})
        comparisons[cid] = {"common_valid_pairs": len(common), "preference": dict(counts),
            "dimension_direction_counts": {k: dict(v) for k, v in dimension_counts.items()},
            "delta_judge_A_minus_judge_B": {d: effect(obs, experiment["cases"], experiment["repeats"],
                [experiment["id"], cid, "judge_disagreement", d], replicates) for d, obs in observations.items()}}
    return {"available": True, "judge_A": left["evaluator_id"], "judge_B": right["evaluator_id"],
            "rubric_hashes_match": left["rubric_hash"] == right["rubric_hash"], "comparisons": comparisons}


METHOD = {
    "independent_unit": "family_id; Sidelit original image, not intent/repeat/judge",
    "aggregation": "paired candidate-baseline within the same judge unit; mean repeats within case, then equal case means within family, then equal family means",
    "primary_coverage": "complete families require every planned case x repeat to have a valid paired metric; observed-family results are separately labelled subset descriptions",
    "ci95": "deterministic percentile cluster bootstrap over family means; 20000 resamples by default; no interval for n<2; unadjusted for multiple dimensions/candidates",
    "missing": "invalid/unavailable/not_applicable/unable/pending/stale identity are not zero; statuses and denominators retained",
    "missingness_sensitivity": "full planned cohort bounds assign missing pair deltas their allowed extremes (preference +/-1, all frozen dimension rubrics 0-2 so delta +/-2); these are finite-cohort worst/best bounds, not confidence intervals or imputed scores; bounds withheld if not_applicable makes a numeric all-planned-pair estimand undefined",
    "known_v1_selection_bias": "Frozen validator v1 rejects exact non-string JSON evidence such as [] and 8; this systematically excludes restrained empty-relationship outputs. The v1 accepted subset is not random. Typed full-batch revalidation must be a separately identified cohort, not failed-only additions",
    "typed": "Frozen engine receipt covers every unit of each completed physical source job, including originally valid and invalid responses. Hashes, attempt bindings, unchanged original judgment and imported assessment bodies are reconciled without invoking the validator. Verified receipt validity and core import status are separate. Derived records add no model requests or independent judges. Legacy output field valid_accepted_pairs means valid paired judgments in typed cohorts, not necessarily imported pairs",
    "typed_lineage": "Exact saved provider/quote retry maps choose one response per logical pair before its result, irrespective of score/status. Pending receipts or invalid retries stay missing; no old-success fallback. Original-only typed group isolates the validator change; typed retry group additionally changes designated judge attempts",
    "hard_flags": "Judge flags are not automatically semantic adjudications. Any-dimension candidate-only counts can be masked by shared violations (e.g. Sidelit opinion_boundary); paired per-dimension baseline/candidate-only counts and distinct families are therefore also reported",
    "selection": "six explicit frozen jobs only; succeeded accepted units must reconcile both core assessment IDs and current product attempt/hash; no best-of-rescore selection",
    "preference": "win=+1,tie=0,loss=-1; family W/T/L is sign of within-family mean, never count repeat votes as independent n",
    "judges": "Astra/Sol reported separately; only exact same pair identities enter disagreement; two judges do not double n",
    "performance": "current successful delivery and all-attempt cost separate; input/output aliases normalized, total derived only when both known; cache/reasoning subsets not added again; unknown not zero",
    "latency": "trial duration_ms is start-to-terminal wall time incl setup/cleanup, not TTFT or device latency; p50/p95 nearest-rank; paired CI uses family mean durations",
    "pome": "original v1.0.0 input-error experiment audit only; corrected full v1.0.1 is quality cohort; retained never-sent restart attempts are scheduling failures, not model failures",
    "sidelit": "intent-conditioned B receives user intent; baseline and evidence-grounded A do not. Intent-fit gains would be capability augmentation, not equal-input intent compliance. Fixed-seed repeats are execution checks, not independent draws. Judge sees structured review_output including overall_score; the root separately inspected native UI feedback-only display, so score-field flags do not establish UI display or human-experience failures",
    "partial": "queue order is not random; a partial or valid-only subset cannot establish full-cohort superiority; no composite score or automatic default switch",
    "limitations": "8/12 families, AI judges only, unadjusted intervals, no human gold standard; source/version freeze does not prove population representativeness; zero-width ceiling/tie intervals do not establish equivalence; wall-time measurements do not control machine contention",
}


def analyze(snapshot, replicates):
    valid = {}; projects = []
    all_terminal = len(snapshot["jobs"]) == 6 and all(j["status"] not in ACTIVE | {"missing"} for j in snapshot["jobs"])
    for e in snapshot["experiments"]:
        jobs = []
        for j in snapshot["jobs"]:
            if j["experiment_id"] == e["id"]:
                result, valid[j["id"]] = quality(j, e, snapshot["assessment_identities"], replicates); jobs.append(result)
        projects.append({k: e[k] for k in ("id", "project", "role", "baseline_id", "taskpack_id", "taskpack_version", "configurations", "repeats")} |
                        {"families": len({c.get("family_id") or c["id"] for c in e["cases"]}), "cases": len(e["cases"]),
                         "execution": execution(e, replicates), "judges": jobs,
                         "judge_disagreement": disagreement(e, jobs, valid, replicates) if jobs else None})
    incomplete = any(c["valid_accepted_pairs"] != c["planned_pairs"] for p in projects for j in p["judges"] for c in j["comparisons"].values())
    return {"schema_version": "myeval.independent-results.v1", "captured_at": snapshot["captured_at"],
            "validation_cohort": snapshot.get("validation_cohort", "original_six_jobs_frozen_validator_v1"),
            "recovery_map_hash": snapshot.get("recovery_map_hash"),
            "typed_engine_hash": snapshot.get("typed_engine_hash"), "typed_receipt_audit": snapshot.get("typed_receipt_audit"),
            "typed_retry_map_hash": (snapshot.get("typed_retry_map") or {}).get("content_hash"),
            "judge_usage_audit": snapshot.get("judge_usage_audit"),
            "input_snapshot_hash": snapshot["content_hash"], "generated_at": now(),
            "status": "partial" if not all_terminal else "final_incomplete_coverage" if incomplete else "final_complete_coverage",
            "all_six_jobs_terminal": all_terminal, "bootstrap_replicates": replicates, "method": METHOD, "projects": projects}


def fmt(value, places=3):
    return "—" if value is None else f"{value:.{places}f}" if number(value) else str(value)


def interval(value):
    return "—" if value is None else "[" + ", ".join(fmt(x) for x in value) + "]"


def report_markdown(result):
    typed = bool(result.get("typed_engine_hash"))
    lines = ["# 三项目正式运行：独立结果聚合", "",
             f"快照 {result['captured_at']}；状态 **{result['status']}**。六个逻辑比较组全部终态：{result['all_six_jobs_terminal']}。", "",
             f"验证/恢复组：**{result['validation_cohort']}**。每个逻辑pair只按已保存的明确恢复映射选定一次响应，原失败与原始v1组另存；不按分数择优、不增加family n。", "",
             ("本组使用冻结严格 typed validator 对每个完整物理作业的全部响应再验证，原分数、理由和偏好未改。仅 receipt 通过的判断可用；尚未取得完整 receipt 的新评分保留缺失，不能回填旧分数。导入状态单独列出。" if typed else
              "冻结 validator v1 错误排除 `[]`、数字等精确非字符串 JSON 证据，会系统性排除克制的空关系输出。有效子集存在选择偏差；后续 typed validator 的全量再验必须另组展示，不能仅把失败项补入旧组。详见 `evaluation-invalid-audit.md`。"), "",
             "部分或缺失覆盖不得解释为总体胜出。下表的 n 是完整覆盖的独立 family；Sidelit 为原图。重复、意图和两名裁判不增加 n。95%CI 为 family bootstrap、未经多重比较调整；不合成总分。", "",
             "方法与可重放输入见 analysis.json / snapshot 文件；family-metrics.csv 保留每 family 的基线、候选、差值、计划与有效配对数。", ""]
    if typed:
        lines += [f"严格引擎 `{result['typed_engine_hash']}`；明确引文重评 map `{result['typed_retry_map_hash']}`。", "",
                  "所有原始及追加模型作业各计一次；派生验证零新请求，不能当第三名裁判。", "",
                  f"评分执行用量：`{json.dumps(result['judge_usage_audit'],ensure_ascii=False)}`。unknown 保留，known_total_tokens 不是未知用量的估计。", "",
                  "| 物理作业 | 原状态 | 完整 receipt | 再验计数 | 核心库导入 pair 状态 |", "|---|---|---|---|---|"]
        for receipt in result["typed_receipt_audit"]:
            lines.append(f"| {receipt['source_job_id']} | {receipt['source_status']} | {receipt['receipt_status']} | {json.dumps(receipt.get('counts',{}))} | {json.dumps(receipt.get('core_import_pairs',{}))} |")
        lines.append("")
    for p in result["projects"]:
        lines += [f"## {p['role']}", "", f"实验 `{p['id']}`；{p['families']} family，{p['cases']} case，{p['repeats']} 次重复。", ""]
        if p["role"] == "sidelit_formal":
            lines += ["本轮评价对象是含 overall_score 的完整结构化点评。无分数请求相关标记只能指向结构化交付契约；根会话另查原生 UI 只显示三类 feedback，因此不能据此声称 UI 展示总分或已证明真人体验重大失败。baseline/A 不接收意图，B 接收意图；相应收益是能力增量。", ""]
        if p["role"] == "pome_corrected_v1.0.1_formal":
            lines += ["修正版 72 项首次真正启动均成功；每项此前 attempt 1 是重启错误留下的 never-sent interruption，attempt 2 才执行。144 attempts 不等于 144 次模型执行，也不是新增样本。原输入错误版单独审计。", ""]
        lines += ["| 配置 | 最终成功/trial | 首次记录成功 | 首次启动成功 | attempts/never-sent | 当前成功 p50/p95 ms | 全尝试已知 tokens/缺失 |", "|---|---:|---:|---:|---:|---:|---:|---:|"]
        for cid, c in p["execution"]["configurations"].items():
            latency = c["current_success_latency_ms"]; tokens = c["all_attempt_tokens"]["total_tokens"]
            lines.append(f"| {cid} | {c['current_statuses'].get('succeeded',0)}/{c['trials']} | {c['first_recorded_attempt_statuses'].get('succeeded',0)}/{c['trials']} | {c['first_started_attempt_statuses'].get('succeeded',0)}/{c['trials']} | {c['attempt_count']}/{c['known_not_sent_attempts']} | {fmt(latency['p50'],0)}/{fmt(latency['p95'],0)} | {fmt(tokens['known_total'],0)}/{tokens['missing']} |")
        if not p["judges"]:
            lines += ["", "仅保留输入错误审计，不混入修正版质量或效率比较。", ""]; continue
        for j in p["judges"]:
            lines += ["", f"### {j['evaluator_id']} — {j['status']}", "", f"原比较组 `{j['id']}`；选中单位状态计数 `{json.dumps(j['unit_statuses'],ensure_ascii=False)}`。实际来源作业：`{json.dumps(j.get('source_job_ids') or [j['id']])}`。", ""]
            for cid, c in j["comparisons"].items():
                lines += [f"**{cid} vs {p['baseline_id']}**：有效判断 {c['valid_accepted_pairs']}/{c['planned_pairs']}；配对胜/平/负/无法 `{json.dumps(c['preference_unit_counts'],ensure_ascii=False)}`（这些是描述性单位数，不是独立 n）。", "",
                          "| 维度 | 完整 family n | 基线均值 | 候选均值 | 差值 | 95%CI | 完整family W/T/L | 观测family n / 差值 | 全计划缺失敏感性界限 |", "|---|---:|---:|---:|---:|---|---|---:|---|"]
                for d, m in c["metrics"].items():
                    full, obs = m["complete_families"], m["observed_families"]
                    lines.append(f"| {d} | {full['n_families']}/{m['planned_families']} | {fmt(full['baseline'])} | {fmt(full['candidate'])} | {fmt(full['mean_delta'])} | {interval(full['ci95'])} | {json.dumps(full['family_win_tie_loss'])} | {obs['n_families']} / {fmt(obs['mean_delta'])} | {interval(m['full_planned_cohort_missingness_bounds'])} |")
                lines += ["", f"重大/严重问题是该裁判的标记，未自动裁定为事实：`{json.dumps(c['major_critical_flags'],ensure_ascii=False)}`。", ""]
                if c["major_critical_paired_dimensions"]:
                    lines += ["下表逐维度按同一 pair 比较；不能因双方共享另一个维度的重大标记，就把新问题隐藏为 candidate_only=0。", "",
                              "| 重大/严重维度 | baseline / candidate pairs | 候选独有 pairs / family | baseline 独有 pairs / family |", "|---|---:|---:|---:|"]
                    for dim, flags in c["major_critical_paired_dimensions"].items():
                        counts, families = flags["counts"], flags["families"]
                        lines.append(f"| {dim} | {counts['baseline_units']} / {counts['candidate_units']} | {counts['candidate_only_units']} / {len(families.get('candidate_only', []))} | {counts['baseline_only_units']} / {len(families.get('baseline_only', []))} |")
                    lines.append("")
                if c["nonrated_dimension_statuses"]:
                    lines += [f"已接纳判断中的非数值维度状态：`{json.dumps(c['nonrated_dimension_statuses'],ensure_ascii=False)}`；not_applicable 是适用性状态，不计产品失败，不补数值。含 N/A 的维度不提供全计划数值界限。", ""]
                for task, metrics in c["task_slices"].items():
                    lines += [f"切片 `{task}`（也只按完整 family）：", "", "| 维度 | n | 差值 | 95%CI |", "|---|---:|---:|---|"]
                    for d, m in metrics.items():
                        full = m["complete_families"]
                        lines.append(f"| {d} | {full['n_families']}/{m['planned_families']} | {fmt(full['mean_delta'])} | {interval(full['ci95'])} |")
                    lines.append("")
        lines += ["### 两裁判分歧", "", "| 候选 | 同身份有效 pairs | 偏好可比/一致 | 反向胜负 | 平局与方向分歧 |", "|---|---:|---:|---:|---:|"]
        for cid, c in p["judge_disagreement"].get("comparisons", {}).items():
            v = c["preference"]
            lines.append(f"| {cid} | {c['common_valid_pairs']} | {v.get('comparable',0)}/{v.get('agreement',0)} | {v.get('opposite_win_loss',0)} | {v.get('tie_vs_direction',0)} |")
        lines += ["", "### 配对工程指标", "", "tokens/ms 负差值表示该指标降低；success 为 0–1 比率，正差值才是提高。墙钟差异不代表设备/热/能耗收益。latest_success 不包括失败重试成本；all_attempts 包括。", "",
                  "| 候选 | 指标 | family n | 基线均值 | 候选均值 | 差值 | 95%CI | 相对差 |", "|---|---|---:|---:|---:|---:|---|---:|"]
        for cid, metrics in p["execution"]["paired_efficiency"].items():
            for metric, m in metrics.items():
                if metric.endswith(("cached_input_tokens", "reasoning_tokens")):
                    continue
                v = m["complete_families"]
                relative = fmt(100 * v["relative_delta"], 1) + "%" if v["relative_delta"] is not None else "—"
                lines.append(f"| {cid} | {metric} | {v['n_families']}/{m['planned_families']} | {fmt(v['baseline'],1)} | {fmt(v['candidate'],1)} | {fmt(v['mean_delta'],1)} | {interval(v['ci95'])} | {relative} |")
        lines.append("")
    lines += ["## 结论边界", "",
              "若状态为 partial，当前只能描述已覆盖部分；队列顺序不随机。若作业全终态但有 invalid/unavailable，完整family和观测子集都不能伪装成全体；差值跨零或裁判方向冲突只能称趋势。", "",
              "两裁判同向且区间排除零的维度仍须结合预冻结目标、多重比较和重大问题审阅。没有人类评价，不声称用户满意；没有真机证据，不声称 iPhone 拍摄、热或能耗改善。产品默认切换不由此脚本执行。", ""]
    return "\n".join(lines)


def write_text(path, text):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8"); temporary.replace(path)


def write_outputs(result, snapshot, out_dir, snapshot_name):
    out_dir.mkdir(parents=True, exist_ok=True)
    if snapshot.get("typed_retry_map"):
        path = out_dir / "typed-retry-map.json"
        if path.exists() and json.loads(path.read_text()) != snapshot["typed_retry_map"]:
            raise ValueError("Immutable typed retry map differs; use a new output directory for a new lineage")
    write_text(out_dir / snapshot_name, json.dumps(snapshot, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    result["input_snapshot_file"] = snapshot_name
    write_text(out_dir / "analysis.json", json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    write_text(out_dir / "report.md", report_markdown(result))
    rows = []
    for p in result["projects"]:
        for j in p["judges"]:
            for cid, c in j["comparisons"].items():
                for metric, m in c["metrics"].items():
                    for f in m["families"]:
                        rows.append({"experiment_id": p["id"], "project": p["project"], "job_id": j["id"], "judge": j["evaluator_id"],
                                     "candidate_id": cid, "metric": metric, **{k: f[k] for k in ("family_id", "planned_pairs", "valid_pairs", "complete", "baseline", "candidate", "delta")}})
    stream = io.StringIO()
    if rows:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)
    write_text(out_dir / "family-metrics.csv", stream.getvalue())
    units = [{"experiment_id": p["id"], "job_id": j["id"], "judge": j["evaluator_id"], **u}
             for p in result["projects"] for j in p["judges"] for u in j["unit_audit"]]
    write_text(out_dir / "unit-audit.json", json.dumps(units, ensure_ascii=False, indent=2) + "\n")
    if snapshot.get("typed_retry_map"):
        path = out_dir / "typed-retry-map.json"
        if not path.exists():
            write_text(path, json.dumps(snapshot["typed_retry_map"], ensure_ascii=False, indent=2) + "\n")
        write_text(out_dir / "typed-receipt-audit.json", json.dumps(snapshot["typed_receipt_audit"], ensure_ascii=False, indent=2) + "\n")


def recovery_plan(data_dir, out_dir):
    """Plan exact provider-failed subsets using the current API's pure pairing block.

    No EvaluationService instance is constructed, and create_job is never called.
    Only its grouping AST (before UUID/media/storage) is evaluated in memory.
    """
    source_path = ROOT / "myeval/evaluation/service.py"
    source = source_path.read_text(); tree = ast.parse(source)
    service = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "EvaluationService")
    create = next(n for n in service.body if isinstance(n, ast.FunctionDef) and n.name == "create_job")
    def assigned(node, name):
        return isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets)
    start = next(i for i, n in enumerate(create.body) if assigned(n, "grouped"))
    end = next(i for i, n in enumerate(create.body) if assigned(n, "job_id"))
    nodes = create.body[start:end]
    allowed_calls = {"len", "dict", "set", "zip", "id", "any", "ValueError", "lookup.get", "secrets.SystemRandom", "pair.append",
                     "pair.shuffle", "grouped.append", "used.update"}
    for call in (n for stmt in nodes for n in ast.walk(stmt) if isinstance(n, ast.Call)):
        name = ast.unparse(call.func)
        if name not in allowed_calls and name != "secrets.SystemRandom().shuffle" and name != "experiment.get":
            raise ValueError("Pairing code gained an unreviewed call: " + name)
    grouping_source = "\n".join(source.splitlines()[nodes[0].lineno - 1:nodes[-1].end_lineno])
    grouping_code = compile(ast.Module(body=nodes, type_ignores=[]), str(source_path) + ":pairing_only", "exec")
    evaluator_node = next(n for n in tree.body if assigned(n, "EVALUATORS"))
    current_evaluators = ast.literal_eval(evaluator_node.value)
    implementation = {n: hashlib.sha256((source_path.parent / n).read_bytes()).hexdigest()
                      for n in ("service.py", "schema.py", "media.py", "provider.py")}
    db = connect(data_dir / "evaluation/evaluation.sqlite3")
    originals = []
    for jid in JOBS:
        raw = db.execute("SELECT body FROM jobs WHERE id=?", (jid,)).fetchone()
        j = json.loads(raw[0]); j["units"] = [json.loads(r[0]) for r in db.execute(
            "SELECT body FROM units WHERE job_id=? ORDER BY ordinal", (jid,))]
        originals.append(j)
    chain = []
    for jid in ("5eb02d87-8c4b-4e66-a208-174ebc7bb098", "18b0736e-e5ef-4e4d-b4c7-9dde3a89bb0f"):
        raw = db.execute("SELECT body FROM jobs WHERE id=?", (jid,)).fetchone()
        if raw:
            j = json.loads(raw[0]); us = [json.loads(r[0]) for r in db.execute("SELECT body FROM units WHERE job_id=?", (jid,))]
            chain.append({"job_id": jid, "status": j["status"], "evaluator_hash": j["evaluator_hash"],
                          "units": [{"id": u["id"], "status": u["status"], "mapping": u["mapping"],
                                     "stored_judgment_hash": digest(u.get("output")),
                                     "error_code": (u.get("error") or {}).get("code")} for u in us]})
    db.close(); core = connect(data_dir / "myeval.sqlite3")
    entries = []
    def canonical_pair(mapping):
        return tuple(sorted((v["trial_id"], v["execution_attempt"], v["result_hash"]) for v in mapping.values()))
    for j in originals:
        selected = [u for u in j["units"] if u["status"] == "failed" and (u.get("error") or {}).get("code") == "model_failed"]
        entry = {"source_job_id": j["id"], "experiment_id": j["experiment_id"], "evaluator_id": j["evaluator"]["id"],
                 "source_status_counts": dict(Counter(u["status"] for u in j["units"])), "provider_failed_units": len(selected),
                 "excluded_invalid_unit_ids": [u["id"] for u in j["units"] if u["status"] == "invalid"],
                 "excluded_succeeded_count": sum(u["status"] == "succeeded" for u in j["units"]),
                 "blockers": [], "request": None, "pair_manifest": []}
        entries.append(entry)
        if not selected:
            entry["subset_verification"] = {"status": "no_provider_failed_pairs_no_request"}
            continue
        e = json.loads(core.execute("SELECT body FROM experiments WHERE id=?", (j["experiment_id"],)).fetchone()[0])
        frozen_evaluator = next(x for x in current_evaluators if x["id"] == j["evaluator"]["id"]) | {"implementation": implementation}
        entry["expected_evaluator_hash"] = j["evaluator_hash"]
        entry["current_source_evaluator_hash"] = digest(frozen_evaluator)
        entry["expected_rubric_hash"] = j["rubric_hash"]
        if digest(frozen_evaluator) != j["evaluator_hash"]:
            entry["blockers"].append("current_grader_source_not_original_frozen_grader")
        if digest(j["rubric"]) != j["rubric_hash"]:
            entry["blockers"].append("original_rubric_hash_mismatch")
        targets = {}
        for unit in selected:
            if len(unit["mapping"]) != 2:
                entry["blockers"].append("source_unit_not_pairwise:" + unit["id"])
            for target in unit["mapping"].values():
                identity = (target["trial_id"], target["execution_attempt"], target["result_hash"])
                if identity in targets:
                    continue
                raw = core.execute("SELECT body FROM trials WHERE id=?", (target["trial_id"],)).fetchone()
                trial = json.loads(raw[0]) if raw else None
                raw = core.execute("SELECT body FROM attempts WHERE trial_id=? AND number=?", identity[:2]).fetchone()
                attempt = json.loads(raw[0]) if raw else None
                matches = (trial and attempt and trial["experiment_id"] == j["experiment_id"] and attempt["status"] == "succeeded"
                           and digest(attempt.get("result")) == target["result_hash"]
                           and (trial["attempt"], digest(trial.get("result"))) == identity[1:]
                           and (trial["case_id"], trial["repeat_index"], trial["configuration_id"]) ==
                               (target["case_id"], target["repeat_index"], target["configuration_id"]))
                if not matches:
                    entry["blockers"].append("target_identity_no_longer_matches:" + target["trial_id"])
                targets[identity] = dict(target)
            pair = canonical_pair(unit["mapping"])
            candidate = next((v for v in unit["mapping"].values() if v["configuration_id"] != e["baseline_id"]), {})
            entry["pair_manifest"].append({"source_unit_id": unit["id"], "pair_identity_hash": digest(pair),
                "case_id": unit["case_id"], "family_id": unit["family_id"], "repeat_index": candidate.get("repeat_index"),
                "candidate_id": candidate.get("configuration_id"), "pair_targets": [dict(zip(("trial_id", "execution_attempt", "result_hash"), x)) for x in pair],
                "original_status": unit["status"], "original_error_code": (unit.get("error") or {}).get("code"),
                "original_request_sent": unit.get("request_sent")})
        ordered = sorted(targets.values(), key=lambda t: (t["case_id"], t["repeat_index"], t["configuration_id"] != e["baseline_id"], t["configuration_id"], t["trial_id"]))
        budget = dict(j["budget"], max_units=len(selected))
        env = {"__builtins__": {"len": len, "dict": dict, "set": set, "zip": zip, "id": id, "ValueError": ValueError, "any": any},
               "mode": "pairwise", "targets": ordered, "experiment": e, "budget": budget, "secrets": secrets}
        exec(grouping_code, env)
        actual = Counter(canonical_pair(group) for group in env["grouped"])
        expected = Counter(canonical_pair(u["mapping"]) for u in selected)
        success = {canonical_pair(u["mapping"]) for u in j["units"] if u["status"] == "succeeded"}
        invalid = {canonical_pair(u["mapping"]) for u in j["units"] if u["status"] == "invalid"}
        extra, missing = list((actual - expected).elements()), list((expected - actual).elements())
        if extra or missing or any(n != 1 for n in actual.values()) or actual.keys() & (success | invalid):
            entry["blockers"].append("planned_pair_multiset_not_exact_failed_subset")
        entry["subset_verification"] = {"status": "exact" if not entry["blockers"] else "blocked",
            "method": "execute only current create_job grouping AST in memory; no service constructor, media copy, DB/API/model mutation",
            "expected_pairs": len(selected), "actual_planned_pairs": sum(actual.values()), "unique_targets": len(ordered),
            "extra_pair_count": len(extra), "missing_pair_count": len(missing),
            "already_succeeded_pair_overlap": len(actual.keys() & success), "original_invalid_pair_overlap": len(actual.keys() & invalid),
            "expected_pair_set_hash": digest(sorted(expected.elements())), "actual_pair_set_hash": digest(sorted(actual.elements()))}
        body = {"experiment_id": j["experiment_id"], "evaluator_id": j["evaluator"]["id"], "mode": "pairwise",
                "rubric": j["rubric"], "split": j.get("split", "all"), "title": "Provider recovery original " + j["id"],
                "targets": [{k: t[k] for k in ("trial_id", "execution_attempt", "result_hash")} for t in ordered],
                "timeout_seconds": j["timeout_seconds"], "concurrency": j.get("concurrency", 2), "budget": budget}
        entry["request"] = {"method": "POST", "path": "/api/evaluation-jobs", "body": body, "body_hash": digest(body, sorted_keys=True)}
        entry["plan_key"] = digest({"source_job_id": j["id"], "unit_ids": sorted(u["id"] for u in selected),
                                    "body_hash": entry["request"]["body_hash"], "grader_hash": j["evaluator_hash"]}, sorted_keys=True)
    core.close()
    plan = {"schema_version": "myeval.provider-recovery-plan.v1", "generated_at": now(),
            "status": "ready_for_parent_execution" if not any(e["blockers"] for e in entries) else "blocked",
            "api_supports_exact_subset": not any(e["blockers"] for e in entries), "source_service_sha256": implementation["service.py"],
            "pairing_block_lines": [nodes[0].lineno, nodes[-1].end_lineno], "pairing_block_sha256": hashlib.sha256(grouping_source.encode()).hexdigest(),
            "total_provider_failed_pairs": sum(e["provider_failed_units"] for e in entries), "entries": entries,
            "resolved_text_quote_retry_chain": {"original_job_id": "4f1dadb5-22e6-41e0-859e-169f822eefbc",
                "original_invalid_unit_id": "2bb19b61-83ff-4fb4-b987-892c35b1d9f9", "attempt_jobs": chain,
                "action": "no_new_request; include successful 18b0736e response in separately identified final typed-validation retry cohort"},
            "execution_notes": ["No API/model request sent by this planner. Quota attribution is orchestrator-reported; raw error code retained as model_failed.",
                "Parent must confirm live grader matches expected evaluator hash immediately before dispatch and store new_job_id/source_unit_id mapping by exact pair identities.",
                "API has no idempotency key or durable retry_of fields. Do not replay an uncertain POST; inspect already-created job first. Plan keys and title are external receipts, not server-enforced idempotency.",
                "A/B labels are reshuffled by create_job; compare unordered trial/attempt/hash pairs, not label or unit ordinal.",
                "Original succeeded/invalid units are excluded. Original failed attempts and usage remain separate; a recovery judge request is new cost, never another independent family.",
                "Prior quote retry 5eb02d87 failed but 18b0736e has recovered it; it is deliberately excluded from provider-failure recovery requests."]}
    out_dir.mkdir(parents=True, exist_ok=True); requests = out_dir / "recovery-requests"; requests.mkdir(exist_ok=True)
    for entry in entries:
        if entry["request"] and not entry["blockers"]:
            path = requests / (entry["source_job_id"] + ".json")
            write_text(path, json.dumps(entry["request"]["body"], ensure_ascii=False, indent=2) + "\n")
            entry["request_file"] = str(path)
    write_text(out_dir / "provider-failure-recovery-plan.json", json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
    return {"status": plan["status"], "pairs": plan["total_provider_failed_pairs"],
            "entries": [{k: e.get(k) for k in ("source_job_id", "provider_failed_units", "subset_verification", "blockers", "request_file")} for e in entries]}


def recovery_map(data_dir, out_dir):
    db = connect(data_dir / "evaluation/evaluation.sqlite3")
    def load(jid):
        row = db.execute("SELECT body FROM jobs WHERE id=?", (jid,)).fetchone()
        if row is None:
            raise ValueError("Recovery job is not present: " + jid)
        j = json.loads(row[0]); units = [json.loads(r[0]) for r in db.execute("SELECT body FROM units WHERE job_id=? ORDER BY ordinal", (jid,))]
        return j, units
    def identity(u):
        return tuple(sorted((v["trial_id"], v["execution_attempt"], v["result_hash"]) for v in u["mapping"].values()))
    groups = []; observations = []
    for original_id, new_id in RECOVERY_JOBS.items():
        old, all_old = load(original_id); new, all_new = load(new_id)
        failed = [u for u in all_old if u["status"] == "failed" and (u.get("error") or {}).get("code") == "model_failed"]
        old_counts = Counter(identity(u) for u in failed); new_counts = Counter(identity(u) for u in all_new)
        excluded = {identity(u) for u in all_old if u["status"] in {"succeeded", "invalid"}}
        problems = []
        if old_counts != new_counts or any(n != 1 for n in new_counts.values()):
            problems.append("pair_multiset_mismatch_or_duplicates")
        if new_counts.keys() & excluded:
            problems.append("original_succeeded_or_invalid_overlap")
        for key in ("experiment_id", "evaluator_hash", "rubric_hash", "mode"):
            if old[key] != new[key]:
                problems.append(key + "_mismatch")
        if problems:
            raise ValueError(original_id + ": " + ",".join(problems))
        new_by_pair = {identity(u): u for u in all_new}
        pairs = []
        for u in failed:
            pair = identity(u); fresh = new_by_pair[pair]
            pairs.append({"source_unit_id": u["id"], "recovery_unit_id": fresh["id"], "pair_identity_hash": digest(pair),
                          "case_id": u["case_id"], "family_id": u["family_id"],
                          "targets": [dict(zip(("trial_id", "execution_attempt", "result_hash"), x)) for x in pair]})
        groups.append({"source_job_id": original_id, "recovery_job_id": new_id, "experiment_id": old["experiment_id"],
                       "evaluator_id": old["evaluator"]["id"], "evaluator_hash": old["evaluator_hash"],
                       "rubric_hash": old["rubric_hash"], "pair_count": len(pairs),
                       "pair_set_hash": digest(sorted(old_counts.elements())), "pairs": sorted(pairs, key=lambda x: x["source_unit_id"]),
                       "source_succeeded_and_invalid_excluded": True})
        observations.append({"source_job_id": original_id, "recovery_job_id": new_id, "status": new["status"],
                             "counts": dict(Counter(u["status"] for u in all_new)), "pair_verification": "exact"})
    old, units = load("4f1dadb5-22e6-41e0-859e-169f822eefbc")
    source_unit = next(u for u in units if u["id"] == "2bb19b61-83ff-4fb4-b987-892c35b1d9f9")
    chain = []
    for jid in ("5eb02d87-8c4b-4e66-a208-174ebc7bb098", "18b0736e-e5ef-4e4d-b4c7-9dde3a89bb0f"):
        j, us = load(jid)
        if len(us) != 1 or identity(us[0]) != identity(source_unit) or any(j[k] != old[k] for k in ("evaluator_hash", "rubric_hash", "experiment_id")):
            raise ValueError("Explicit text quote retry chain does not preserve the same pair/grader: " + jid)
        chain.append({"job_id": jid, "unit_id": us[0]["id"], "status_at_mapping": us[0]["status"],
                      "stored_judgment_hash": digest(us[0].get("output"))})
    db.close()
    manifest = {"schema_version": "myeval.immutable-recovery-map.v1", "source_selection": "original model_failed only",
                "provider_recovery_pair_count": sum(g["pair_count"] for g in groups), "groups": groups,
                "text_quote_retry_chain": {"source_job_id": old["id"], "source_unit_id": source_unit["id"],
                    "pair_identity_hash": digest(identity(source_unit)), "attempts": chain,
                    "selected_job_id": "18b0736e-e5ef-4e4d-b4c7-9dde3a89bb0f", "selected_unit_id": chain[-1]["unit_id"],
                    "reason": "explicit full-pair same-grader retry of non-verbatim quote; first retry quota-failed; second succeeded",
                    "new_retry_requested": False},
                "selection_policy": "Original succeeded/invalid responses remain in original cohort. Recovery cohort replaces only mapped original provider-failed units, plus the explicit text-quote retry, regardless of new score/status. No best-score or success-only fallback. Typed full-batch validation is a separate group.",
                "cost_policy": "All original and recovery requests retained once each; deterministic revalidation adds zero model requests; retries do not add independent families."}
    manifest["content_hash"] = digest(manifest, sorted_keys=True)
    destination = out_dir / "recovery-map.json"; out_dir.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        prior = json.loads(destination.read_text())
        if prior != manifest:
            raise ValueError("Existing immutable recovery map differs; refusing overwrite")
    else:
        write_text(destination, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    write_text(out_dir / "recovery-map-verification.json", json.dumps({"checked_at": now(), "map_hash": manifest["content_hash"],
        "all_pair_multisets_exact": True, "source_succeeded_or_invalid_overlap": 0,
        "recovery_jobs": observations, "text_quote_chain_verified": True}, ensure_ascii=False, indent=2) + "\n")
    return {"map": str(destination), "hash": manifest["content_hash"], "provider_recovery_pairs": manifest["provider_recovery_pair_count"],
            "text_quote_chain_verified": True, "groups": observations}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / ".local/v2/data")
    parser.add_argument("--output-dir", type=Path, default=ROOT / ".local/coordination/20261118/results-analysis")
    parser.add_argument("--snapshot", type=Path, help="Replay saved normalized read-only inputs")
    parser.add_argument("--bootstrap-replicates", type=int, default=20000)
    parser.add_argument("--watch-seconds", type=int, default=0)
    parser.add_argument("--write-recovery-plan", action="store_true", help="Read-only exact provider-failed subset plan; does not submit jobs")
    parser.add_argument("--write-recovery-map", action="store_true", help="Verify actual recovery jobs and preserve an immutable original-unit to new-unit mapping")
    parser.add_argument("--recovery-map", type=Path, help="Analyze explicit original-to-retry lineage as a separate cohort; preserve original v1 archive")
    parser.add_argument("--typed-receipts", type=Path, help="Read frozen complete-job deterministic receipts as a separate validation cohort")
    parser.add_argument("--retry-created", action="append", default=[], type=Path, help="Explicit whole-pair quote retry create receipt, repeatable; matching -plan.json required")
    parser.add_argument("--typed-engine-hash", default=TYPED_ENGINE_HASH, help="Exact frozen receipt engine identity; no live source import/execution")
    args = parser.parse_args()
    if args.retry_created and (not args.typed_receipts or not args.recovery_map):
        parser.error("Explicit typed quote retries require typed receipts and the immutable provider recovery map")
    if args.typed_receipts and (args.watch_seconds or args.write_recovery_plan or args.write_recovery_map or args.snapshot):
        parser.error("Typed receipt analysis is a single read-only snapshot; replay a saved snapshot without typed CLI flags")
    if args.typed_receipts and args.output_dir.resolve() == (ROOT / ".local/coordination/20261118/results-analysis").resolve():
        parser.error("Use a separate --output-dir for typed results; original v1 artifacts must not be replaced")
    if args.write_recovery_map:
        if args.watch_seconds or args.snapshot or args.write_recovery_plan:
            parser.error("Recovery mapping reads current jobs once; no watch/snapshot/plan option")
        print(json.dumps(recovery_map(args.data_dir, args.output_dir), ensure_ascii=False), flush=True)
        return
    if args.write_recovery_plan:
        if args.watch_seconds or args.snapshot:
            parser.error("Recovery planning reads current identities once; no watch or snapshot option")
        print(json.dumps(recovery_plan(args.data_dir, args.output_dir), ensure_ascii=False), flush=True)
        return
    if args.bootstrap_replicates < 1000 or args.watch_seconds < 0 or (args.snapshot and args.watch_seconds):
        parser.error("Use >=1000 bootstrap replicates; nonnegative watch; snapshot replay cannot watch")
    while True:
        manifest = json.loads(args.recovery_map.read_text()) if args.recovery_map else None
        retry_entries, retry_documents = load_retry_entries(args.retry_created)
        snapshot = json.loads(args.snapshot.read_text()) if args.snapshot else read_snapshot(args.data_dir, manifest, retry_entries)
        if args.typed_receipts:
            snapshot = compose_typed_snapshot(snapshot, args.typed_receipts, retry_entries, retry_documents, args.typed_engine_hash)
        result = analyze(snapshot, args.bootstrap_replicates)
        stamp = snapshot["captured_at"].replace("-", "").replace(":", "").replace("+0000", "Z")
        name = "snapshot-" + stamp + ".json"
        write_outputs(result, snapshot, args.output_dir, name)
        print(json.dumps({"captured_at": result["captured_at"], "status": result["status"],
              "all_six_jobs_terminal": result["all_six_jobs_terminal"], "report": str(args.output_dir / "report.md"),
              "jobs": [{"judge": j["evaluator_id"], "experiment_id": j["experiment_id"], "status": j["status"],
                        "counts": j["unit_statuses"]} for p in result["projects"] for j in p["judges"]]}, ensure_ascii=False), flush=True)
        if not args.watch_seconds or result["all_six_jobs_terminal"]:
            break
        time.sleep(args.watch_seconds)


if __name__ == "__main__":
    main()
