"""Offline, versioned evidence revalidation. Never invokes a model or opens a writable DB.

Run ``python3 -m myeval.evaluation.revalidation --help``. Import is an explicit,
authenticated call to the existing loopback atomic assessment API; source jobs
and original judgments are never edited.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import ipaddress
import json
import math
import os
import platform
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from ..util import digest as frozen_digest
from .schema import neutral_payload, normalize_pointers
from .revalidation_schema import revalidate_judgment, validator_identity

SCHEMA_VERSION = "myeval.evidence-revalidation.v1"
ENGINE_ID = "myeval-deterministic-revalidation"
ENGINE_VERSION = "1.0.0"
TERMINAL_JOBS = {"succeeded", "completed_with_errors", "cancelled", "budget_exhausted", "interrupted"}
TERMINAL_UNITS = {"succeeded", "invalid", "failed", "timed_out", "cancelled", "interrupted", "unavailable", "budget_exhausted"}
MAX_BATCH_RECORDS = 2000


def stable_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _finite(value):
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non_finite_json_number")
    if isinstance(value, dict):
        for item in value.values():
            _finite(item)
    elif isinstance(value, list):
        for item in value:
            _finite(item)
    return value


def _loads(text):
    def reject(_):
        raise ValueError("non_standard_json_constant")
    return _finite(json.loads(text, object_pairs_hook=_pairs, parse_constant=reject))


def _read(path):
    return _loads(Path(path).read_text())


def _write_new(path, value):
    """Exclusive artifact creation, preserving original JSON object key order."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    encoded = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def engine_identity():
    root = Path(__file__).parent
    implementation = {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                      for name in ("revalidation.py", "revalidation_schema.py", "schema.py")}
    implementation["../util.py"] = hashlib.sha256((root.parent / "util.py").read_bytes()).hexdigest()
    result = {"id": ENGINE_ID, "version": ENGINE_VERSION, "validator": validator_identity(),
              "implementation": implementation, "operation": "deterministic_revalidation", "model_calls": 0,
              "runtime": {"python": platform.python_version(), "implementation": platform.python_implementation()}}
    return {**result, "hash": stable_hash(result)}


def _checked_engine_identity():
    current = engine_identity()
    if current != _LOADED_ENGINE_IDENTITY:
        raise ValueError("revalidation_source_changed_since_process_start; restart_with_explicit_new_version")
    return copy.deepcopy(current)


def _source_content(snapshot):
    return {key: snapshot[key] for key in ("job", "units", "experiment_binding", "attempt_bindings")}


def _capture_bindings(data_dir, job, units):
    if job.get("experiment_id") is None:
        return None, []
    path = Path(data_dir).resolve() / "myeval.sqlite3"
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        row = db.execute("SELECT json_extract(body,'$.id'),json_extract(body,'$.project'),json_extract(body,'$.baseline_id'),json_extract(body,'$.snapshot_hash'),json_extract(body,'$.configuration_ids') FROM experiments WHERE id=?", (job["experiment_id"],)).fetchone()
        if row is None:
            raise ValueError("source_experiment_missing")
        ids = _loads(row[4]) if row[4] else [r[0] for r in db.execute("SELECT json_extract(value,'$.id') FROM experiments,json_each(experiments.body,'$.configurations') WHERE experiments.id=?", (job["experiment_id"],))]
        experiment = {"id": row[0], "project": row[1], "baseline_id": row[2] or (ids[0] if ids else None),
                      "snapshot_hash": row[3], "configuration_ids": ids}
        targets = {(target["trial_id"], target["execution_attempt"]) for unit in units for target in unit["mapping"].values()}
        bindings = []
        for trial_id, attempt in sorted(targets):
            # Read only execution identity, case and result. Candidate configuration/code is not selected.
            row = db.execute("SELECT json_extract(body,'$.id'),json_extract(body,'$.experiment_id'),json_extract(body,'$.case_id'),json_extract(body,'$.configuration_id'),json_extract(body,'$.repeat_index'),json_extract(body,'$.status'),json_extract(body,'$.case'),json_extract(body,'$.result') FROM attempts WHERE trial_id=? AND number=?", (trial_id, attempt)).fetchone()
            if row is None:
                raise ValueError("source_execution_attempt_missing")
            case, result = _loads(row[6]), _loads(row[7]) if row[7] is not None else None
            output = (result or {}).get("review_output")
            bindings.append({"trial_id": row[0], "execution_attempt": attempt, "experiment_id": row[1],
                             "case_id": row[2], "configuration_id": row[3], "repeat_index": row[4], "status": row[5],
                             "family_id": case.get("family_id", row[2]), "case": case, "case_hash": frozen_digest(case),
                             "result_hash": frozen_digest(result), "review_output": output, "review_output_hash": frozen_digest(output)})
        return experiment, bindings
    finally:
        db.close()


def _verify_bindings(snapshot):
    job, units, experiment = snapshot["job"], snapshot["units"], snapshot["experiment_binding"]
    if job.get("experiment_id") is None:
        if experiment is not None or snapshot["attempt_bindings"]:
            raise ValueError("unexpected_calibration_attempt_binding")
        return
    if not isinstance(experiment, dict) or experiment.get("id") != job["experiment_id"] or experiment.get("project") != job["project"]:
        raise ValueError("source_experiment_binding_mismatch")
    bindings = {}
    for binding in snapshot["attempt_bindings"]:
        key = (binding["trial_id"], binding["execution_attempt"])
        if key in bindings or binding["experiment_id"] != job["experiment_id"]:
            raise ValueError("duplicate_or_wrong_experiment_attempt_binding")
        if frozen_digest(binding["case"]) != binding["case_hash"] or frozen_digest(binding["review_output"]) != binding["review_output_hash"]:
            raise ValueError("source_case_or_review_output_hash_mismatch")
        bindings[key] = binding
    used = set()
    for unit in units:
        labels = {"A", "B"} if job["mode"] == "pairwise" else {"A"} if job["mode"] == "individual" else None
        if labels is None or set(unit["mapping"]) != labels:
            raise ValueError("source_mode_side_count_mismatch")
        selected, sides = [], {}
        for label, target in unit["mapping"].items():
            key = (target["trial_id"], target["execution_attempt"])
            if key not in bindings:
                raise ValueError("source_attempt_binding_missing")
            binding = bindings[key]
            if any(target.get(field) != binding.get(field) for field in ("trial_id", "execution_attempt", "case_id", "family_id", "configuration_id", "repeat_index", "status", "result_hash", "review_output_hash")):
                raise ValueError("source_mapping_attempt_mismatch")
            if target.get("output_projection") != "result.review_output":
                raise ValueError("unexpected_source_output_projection")
            if target["case_id"] != unit["case_id"] or target["family_id"] != unit["family_id"] or target["configuration_id"] not in experiment["configuration_ids"]:
                raise ValueError("source_unit_case_or_configuration_mismatch")
            used.add(key); selected.append(binding)
            sides[label] = {"status": binding["status"], "output": binding["review_output"]}
        if job["mode"] == "pairwise":
            a, b = selected
            if a["trial_id"] == b["trial_id"] or a["configuration_id"] == b["configuration_id"] or experiment["baseline_id"] not in {a["configuration_id"], b["configuration_id"]}:
                raise ValueError("source_pair_not_distinct_baseline_and_candidate")
            if any(a[key] != b[key] for key in ("case_id", "family_id", "repeat_index", "case_hash")):
                raise ValueError("source_pair_case_or_repeat_mismatch")
        payload, removed = neutral_payload(selected[0]["case"], sides, unit.get("media", []))
        if frozen_digest(payload) != unit["payload_hash"] or removed != unit.get("removed_metadata_paths", []):
            raise ValueError("source_neutral_payload_does_not_match_exact_attempts")
    if used != set(bindings):
        raise ValueError("source_attempt_binding_denominator_mismatch")


def verify_snapshot(snapshot):
    if snapshot.get("schema_version") != "myeval.revalidation-source.v1":
        raise ValueError("unsupported_source_snapshot_schema")
    job, units = snapshot["job"], snapshot["units"]
    if job.get("status") not in TERMINAL_JOBS:
        raise ValueError("source_job_not_complete")
    if not isinstance(units, list) or len(units) != job.get("unit_count") or not units:
        raise ValueError("source_job_unit_denominator_mismatch")
    if stable_hash(_source_content(snapshot)) != snapshot.get("source_hash"):
        raise ValueError("source_snapshot_hash_mismatch")
    if stable_hash({"job": job, "units": units}) != snapshot.get("job_content_hash"):
        raise ValueError("source_original_job_content_hash_mismatch")
    if frozen_digest(job["evaluator"]) != job.get("evaluator_hash"):
        raise ValueError("source_evaluator_hash_mismatch")
    if frozen_digest(job["rubric"]) != job.get("rubric_hash"):
        raise ValueError("source_rubric_hash_mismatch")
    identifiers, ordinals = set(), set()
    for unit in units:
        if unit.get("job_id") != job["id"] or unit.get("status") not in TERMINAL_UNITS:
            raise ValueError("source_unit_not_terminal_or_wrong_job")
        if unit.get("id") in identifiers or unit.get("ordinal") in ordinals:
            raise ValueError("duplicate_source_unit")
        identifiers.add(unit["id"])
        ordinals.add(unit["ordinal"])
        if frozen_digest(unit["payload"]) != unit.get("payload_hash"):
            raise ValueError("source_payload_hash_mismatch")
        if set(unit["mapping"]) != set(unit["payload"]["sides"]):
            raise ValueError("source_mapping_side_mismatch")
        for target in unit["mapping"].values():
            if type(target.get("execution_attempt")) is not int or target["execution_attempt"] < 1:
                raise ValueError("source_attempt_identity_missing")
            if not isinstance(target.get("result_hash"), str) or len(target["result_hash"]) != 64:
                raise ValueError("source_result_hash_missing")
    if ordinals != set(range(len(units))):
        raise ValueError("source_unit_ordinal_gap")
    counts = dict(Counter(unit["status"] for unit in units))
    if counts != job.get("counts"):
        raise ValueError("source_job_status_denominator_mismatch")
    _verify_bindings(snapshot)
    return snapshot


def snapshot_job(data_dir, job_id):
    """One read-only SQLite transaction; no Service/Store constructor or recovery."""
    path = Path(data_dir).resolve() / "evaluation" / "evaluation.sqlite3"
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        row = db.execute("SELECT id,status,body FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise ValueError("source_job_not_found")
        job = _loads(row[2])
        if job["id"] != row[0] or job["status"] != row[1]:
            raise ValueError("source_job_row_mismatch")
        units = []
        for row in db.execute("SELECT id,job_id,status,ordinal,body FROM units WHERE job_id=? ORDER BY ordinal", (job_id,)):
            unit = _loads(row[4])
            if (unit["id"], unit["job_id"], unit["status"], unit["ordinal"]) != row[:4]:
                raise ValueError("source_unit_row_mismatch")
            units.append(unit)
        experiment, bindings = _capture_bindings(data_dir, job, units)
        result = {"schema_version": "myeval.revalidation-source.v1",
                  "captured_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                  "source": {"database": str(path), "mode": "sqlite_read_only_transaction"},
                  "job": job, "units": units, "experiment_binding": experiment, "attempt_bindings": bindings,
                  "job_content_hash": stable_hash({"job": job, "units": units})}
        result["source_hash"] = stable_hash(_source_content(result))
        return verify_snapshot(result)
    finally:
        db.close()


def _unit_provenance(job, unit):
    raw = (unit.get("provider") or {}).get("output")
    logical_targets = sorted(({key: target[key] for key in ("trial_id", "execution_attempt", "result_hash")}
                              for target in unit["mapping"].values()), key=lambda target: (str(target["trial_id"]), target["execution_attempt"]))
    logical_unit = {"mode": job["mode"], "original_evaluator_hash": job["evaluator_hash"],
                    "rubric_hash": job["rubric_hash"], "targets": logical_targets}
    return {"job_id": job["id"], "unit_id": unit["id"], "unit_hash": stable_hash(unit),
            "logical_unit_hash": stable_hash(logical_unit), "logical_unit_identity": logical_unit,
            "job_created_at": job["created_at"], "unit_ended_at": unit.get("ended_at"),
            "unit_status": unit["status"], "case_id": unit["case_id"], "family_id": unit["family_id"],
            "evaluator": copy.deepcopy(job["evaluator"]), "evaluator_hash": job["evaluator_hash"],
            "rubric_id": job["rubric"]["id"], "rubric_version": job["rubric_version"],
            "rubric_hash": job["rubric_hash"], "payload_hash": unit["payload_hash"],
            "raw_response_hash": stable_hash(raw) if raw is not None else None,
            "normalized_response_hash": stable_hash(unit["output"]) if unit.get("output") is not None else None,
            "response_hash_format": "sha256_sorted_decoded_json; original lexical JSON is not reconstructed",
            "mapping": copy.deepcopy(unit["mapping"]), "pointer_normalizations": copy.deepcopy(unit.get("pointer_normalizations", [])),
            "usage_reference": {"job_id": job["id"], "unit_id": unit["id"], "usage_hash": stable_hash(unit.get("usage")), "count_again": False}}


def _rerun_draft(job, unit, issues):
    if job.get("experiment_id") is None:
        return None
    return {"status": "proposal_only_not_executed", "original_job_id": job["id"], "original_unit_id": unit["id"],
            "required_evaluator_hash": job["evaluator_hash"], "required_rubric_hash": job["rubric_hash"],
            "new_independent_samples": 0, "reason": "original_response_still_invalid_under_strict_revalidation",
            "validation_issues": copy.deepcopy(issues),
            "body": {"experiment_id": job["experiment_id"], "evaluator_id": job["evaluator"]["id"],
                     "mode": job["mode"], "rubric": copy.deepcopy(job["rubric"]),
                     "targets": [{key: target[key] for key in ("trial_id", "execution_attempt", "result_hash")}
                                 for target in unit["mapping"].values()],
                     "title": "显式重评原无效评分 " + unit["id"], "timeout_seconds": 300, "concurrency": 1,
                     "budget": {"max_units": 1, "max_elapsed_seconds": 600, "max_total_tokens": 50000}}}


def _assessment_items(receipt_id, identity, job, unit, validated, provenance):
    evaluator = job["evaluator"]["id"] + "@" + job["evaluator_hash"][:12] + "+rv1@" + identity["hash"][:12]
    evaluator_snapshot = {"id": evaluator, "name": job["evaluator"]["name"] + " · 结构证据重验证 v1",
                          "original_evaluator": copy.deepcopy(job["evaluator"]), "revalidation": copy.deepcopy(identity)}
    evaluator_hash = frozen_digest(evaluator_snapshot)
    unit_id = receipt_id + ":" + unit["id"]
    output, result = unit["output"], []
    for label, side in output["sides"].items():
        target = unit["mapping"][label]
        value = {"schema_version": SCHEMA_VERSION, "operation_kind": "deterministic_revalidation",
                 "scores": copy.deepcopy(side["scores"]), "score_status": copy.deepcopy(side["score_status"]),
                 "hard_violations": copy.deepcopy(side["hard_violations"]), "confidence": output["confidence"],
                 "job_id": receipt_id, "unit_id": unit_id, "original_job_id": job["id"], "original_unit_id": unit["id"],
                 "logical_unit_hash": provenance["logical_unit_hash"], "original_job_created_at": job["created_at"],
                 "evaluator_id": evaluator, "evaluator_hash": evaluator_hash, "original_evaluator_hash": job["evaluator_hash"],
                 "rubric_id": job["rubric"]["id"], "rubric_hash": job["rubric_hash"], "payload_hash": unit["payload_hash"],
                 "validator": copy.deepcopy(identity["validator"]), "revalidation_engine_hash": identity["hash"],
                 "typed_evidence": [copy.deepcopy(item) for item in validated["typed_evidence"] if item["side"] == label],
                 "transform_log": [copy.deepcopy(item) for item in validated["transform_log"] if item["side"] == label],
                 "pointer_normalizations": copy.deepcopy(unit.get("pointer_normalizations", [])),
                 "media": copy.deepcopy(unit.get("media", [])), "new_model_requests": 0,
                 "original_usage_reference": copy.deepcopy(provenance["usage_reference"])}
        if len(unit["mapping"]) == 2:
            other_label = next(key for key in unit["mapping"] if key != label)
            other, choice = unit["mapping"][other_label], output["preference"]["choice"]
            value["preference"] = {"unit_id": unit_id, "choice": "self" if choice == label else "other" if choice == other_label else choice,
                                   "reason": output["preference"]["reason"], "other_trial_id": other["trial_id"],
                                   "other_attempt": other["execution_attempt"], "other_result_hash": other["result_hash"]}
        body = {"kind": "model", "evaluator": evaluator,
                "rubric_version": job["rubric"]["id"] + "@" + str(job["rubric_version"]) + "@" + job["rubric_hash"][:12],
                "reason": side["reason"], "execution_attempt": target["execution_attempt"], "result_hash": target["result_hash"],
                "evaluation_job_id": receipt_id, "idempotency_key": unit["id"] + ":" + label,
                "evaluator_snapshot": copy.deepcopy(evaluator_snapshot),
                "input_hash": unit["payload_hash"], "rubric_hash": job["rubric_hash"], "value": value,
                "evidence": copy.deepcopy(side["evidence"]),
                "provenance": {"operation_kind": "deterministic_revalidation", "receipt_id": receipt_id,
                               "original": copy.deepcopy(provenance), "validator": copy.deepcopy(identity["validator"]),
                               "revalidation_engine_hash": identity["hash"], "new_model_requests": 0}}
        result.append({"trial_id": target["trial_id"], "body": body})
    return result


def revalidate_snapshot(snapshot):
    verify_snapshot(snapshot)
    identity, job = _checked_engine_identity(), snapshot["job"]
    if job["evaluator"].get("implementation", {}).get("schema.py") != identity["implementation"]["schema.py"]:
        raise ValueError("original_schema_source_unavailable_use_matching_isolated_version")
    receipt_id = "rv1-" + stable_hash({"source_hash": snapshot["source_hash"], "engine_hash": identity["hash"]})
    units, items, drafts = [], [], []
    for original in snapshot["units"]:
        provenance = _unit_provenance(job, original)
        output, provider = original.get("output"), original.get("provider") or {}
        eligible = original["status"] in {"succeeded", "invalid"} and provider.get("status") == "succeeded"
        unit = {"original": provenance, "eligible_for_assessment": eligible, "original_validation_issues": copy.deepcopy(original.get("validation_issues", [])),
                "status": "no_response", "validation": None}
        if output is not None:
            integrity = []
            if eligible:
                raw = provider.get("output")
                if raw is None:
                    integrity.append({"code": "missing_original_provider_response"})
                else:
                    try:
                        expected, corrections = normalize_pointers(raw, original["payload"])
                        if stable_hash(expected) != stable_hash(output) or corrections != original.get("pointer_normalizations", []):
                            integrity.append({"code": "original_normalization_provenance_mismatch"})
                    except (ValueError, KeyError, IndexError, TypeError):
                        integrity.append({"code": "original_normalization_provenance_unverifiable"})
            validation = revalidate_judgment(output, job["rubric"], original["payload"])
            validation["validation_issues"] += integrity
            validation["accepted"] = not validation["validation_issues"]
            unit["validation"] = validation
            unit["status"] = "valid" if validation["accepted"] else "invalid"
            if not eligible:
                unit["status"] = "ineligible"
                unit["ineligibility_reason"] = "original_status_or_provider_not_accepted; cancellation_and_failure_are_not_reversed"
            elif validation["accepted"] and job.get("experiment_id") is not None:
                items.extend(_assessment_items(receipt_id, identity, job, original, validation, provenance))
            elif not validation["accepted"] and not integrity:
                draft = _rerun_draft(job, original, validation["validation_issues"])
                if draft is not None:
                    drafts.append(draft)
        units.append(unit)
    counts = dict(Counter(unit["status"] for unit in units))
    # Capture time and source filesystem location stay in the input artifact.
    # They must not change the immutable receipt when the same completed job is read again.
    receipt_source = {key: copy.deepcopy(snapshot[key]) for key in ("schema_version", "source_hash", "job_content_hash", "job", "units", "experiment_binding", "attempt_bindings")}
    receipt = {"schema_version": SCHEMA_VERSION, "id": receipt_id, "operation_kind": "deterministic_revalidation",
               "source_snapshot": receipt_source, "source_hash": snapshot["source_hash"],
               "engine": identity, "experiment_id": job["experiment_id"], "original_job_id": job["id"],
               "original_job_status": job["status"], "original_counts": copy.deepcopy(job["counts"]),
               "counts": counts, "unit_count": len(units), "units": units, "assessment_items": items,
               "assessment_plan_hash": stable_hash(items), "assessment_count": len(items),
               "usage": {"scope": "incremental_revalidation_only", "model_requests": 0, "dispatch_attempts": 0,
                         "known_total_tokens": 0, "total_tokens": 0, "unknown_units": 0, "cost": 0,
                         "original_job_usage_reference": {"job_id": job["id"], "usage_hash": stable_hash(job.get("usage")), "count_again": False}},
               "rerun_requests": drafts,
               "limitations": ["验证只证明证据位置与值存在，不证明原模型的质量分正确。",
                               "原模型判断被重新验证，没有新增独立模型判断、案例、家族或模型用量。",
                               "保留首轮失败；新旧validator口径分组，不能混算。",
                               "媒体只沿用原附件声明；本操作没有再次读取图片或做视觉判断。",
                               "原provider响应按已保存的decoded JSON追溯，不能重建原始字节或被上游解析丢失的内容。"]}
    receipt["receipt_hash"] = stable_hash(receipt)
    return receipt


def verify_receipt(receipt):
    if receipt.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported_revalidation_receipt")
    claimed = receipt.get("receipt_hash")
    if stable_hash({k: v for k, v in receipt.items() if k != "receipt_hash"}) != claimed:
        raise ValueError("revalidation_receipt_hash_mismatch")
    rebuilt = revalidate_snapshot(receipt["source_snapshot"])
    if stable_hash(rebuilt) != stable_hash(receipt):
        raise ValueError("revalidation_receipt_not_reproducible_with_current_engine")
    return receipt


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise ValueError("redirect_refused")


def _base_url(value):
    parsed = urllib.parse.urlsplit(value)
    host = parsed.hostname
    if parsed.scheme != "http" or not host or parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("base_url_requires_plain_loopback_http_origin")
    if host != "localhost":
        try:
            if not ipaddress.ip_address(host).is_loopback:
                raise ValueError("base_url_must_be_loopback")
        except ValueError:
            raise ValueError("base_url_must_be_loopback") from None
    if parsed.port is None:
        raise ValueError("base_url_requires_explicit_port")
    return value.rstrip("/")


def _request(origin, token, route, body=None):
    headers = {"X-MyEval-Admin": token, "Content-Type": "application/json"}
    data = json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode() if body is not None else None
    request = urllib.request.Request(origin + route, data=data, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=60) as response:
            return _loads(response.read().decode())
    except urllib.error.HTTPError as error:
        raise ValueError("assessment_api_http_" + str(error.code)) from None


def _verify_target_bindings(origin, token, source):
    experiment = source["experiment_binding"]
    if experiment is None:
        return
    remote = _request(origin, token, "/api/experiments/" + urllib.parse.quote(experiment["id"], safe=""))
    if any(remote.get(key) != value for key, value in experiment.items()):
        raise ValueError("target_experiment_identity_changed; nothing_submitted")
    trials = {}
    for binding in source["attempt_bindings"]:
        trial_id = binding["trial_id"]
        if trial_id not in trials:
            trials[trial_id] = _request(origin, token, "/api/trials/" + urllib.parse.quote(trial_id, safe=""))
        trial = trials[trial_id]
        if trial.get("id") != trial_id or any(trial.get(key) != binding[key] for key in ("experiment_id", "case_id", "configuration_id", "repeat_index")):
            raise ValueError("target_trial_identity_changed; nothing_submitted")
        attempt = next((a for a in trial.get("attempts", []) if a.get("attempt") == binding["execution_attempt"]), None)
        if attempt is None or attempt.get("status") != binding["status"]:
            raise ValueError("target_attempt_missing_or_status_changed; nothing_submitted")
        result = attempt.get("result")
        case = attempt.get("case", trial.get("case"))
        if frozen_digest(result) != binding["result_hash"] or frozen_digest(case) != binding["case_hash"] or frozen_digest((result or {}).get("review_output")) != binding["review_output_hash"]:
            raise ValueError("target_attempt_content_changed; nothing_submitted")


def _import_output_path(requested, intent):
    """An explicit rerun keeps old receipts and allocates a new attempt artifact."""
    requested = Path(requested)
    keys = ("receipt_id", "receipt_hash", "assessment_plan_hash", "original_job_id", "target_origin", "assessment_count")
    candidate, index = requested, 0
    while candidate.exists() or Path(str(candidate) + ".intent.json").exists():
        for path in (candidate, Path(str(candidate) + ".intent.json")):
            if path.exists():
                previous = _read(path)
                if any(previous.get(key) != intent.get(key) for key in keys):
                    raise ValueError("existing_import_artifact_belongs_to_different_plan")
        index += 1
        candidate = requested.with_name(requested.stem + ".retry-" + str(index) + requested.suffix)
    return candidate


def import_receipt(receipt, base_url, token_file, output_path):
    verify_receipt(receipt)
    items = receipt["assessment_items"]
    if not items:
        result = {"schema_version": "myeval.revalidation-import.v1", "receipt_id": receipt["id"],
                  "receipt_hash": receipt["receipt_hash"], "status": "no_assessments_to_import",
                  "assessment_count": 0, "http_requests": 0, "new_model_requests": 0,
                  "original_job_mutated": False}
        _write_new(output_path, result)
        return result
    if len(items) > MAX_BATCH_RECORDS:
        raise ValueError("atomic_import_requires_at_most_2000_records; no_partial_batch_was_sent")
    origin = _base_url(base_url)
    token = Path(token_file).read_text().strip()
    if not token or any(c.isspace() for c in token):
        raise ValueError("invalid_admin_token_file")
    source = receipt["source_snapshot"]
    remote = _request(origin, token, "/api/evaluation-jobs/" + urllib.parse.quote(receipt["original_job_id"], safe=""))
    remote_job = {key: value for key, value in remote.items() if key not in {"units", "adjudications"}}
    if stable_hash({"job": remote_job, "units": remote.get("units")}) != source["job_content_hash"]:
        raise ValueError("target_original_job_differs_from_receipt; nothing_submitted")
    _verify_target_bindings(origin, token, source)
    intent = {"schema_version": "myeval.revalidation-import.v1", "receipt_id": receipt["id"],
              "receipt_hash": receipt["receipt_hash"], "assessment_plan_hash": receipt["assessment_plan_hash"],
              "original_job_id": receipt["original_job_id"], "target_origin": origin,
              "assessment_count": len(items), "new_model_requests": 0, "status": "intent_persisted",
              "idempotency": "same receipt ID and unit/side keys; rerunning never replaces original judgments"}
    output_path = _import_output_path(output_path, intent)
    intent["artifact_path"] = str(output_path.resolve())
    _write_new(str(output_path) + ".intent.json", intent)
    try:
        _checked_engine_identity()
        reply = _request(origin, token, "/api/assessments/batch", {"items": items})
        records = reply.get("assessments", [])
        if len(records) != len(items):
            raise ValueError("unexpected_assessment_response_count")
        for expected, record in zip(items, records):
            body = expected["body"]
            if any(stable_hash(record.get(key)) != stable_hash(value) for key, value in body.items()):
                raise ValueError("assessment_response_content_mismatch")
        result = {**intent, "status": "confirmed", "record_ids": [r["id"] for r in records],
                  "response_hash": stable_hash(reply), "original_job_mutated": False}
    except Exception as error:
        result = {**intent, "status": "unconfirmed; replay_same_receipt_is_idempotent", "error_type": type(error).__name__}
        _write_new(output_path, result)
        raise
    _write_new(output_path, result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="零模型、完整作业、有版本的严格证据重验证")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("identity")
    snap = sub.add_parser("snapshot", help="只读冻结一个已完成原job；不接受单位子集")
    snap.add_argument("--data-dir", required=True); snap.add_argument("--job-id", required=True); snap.add_argument("--out", required=True)
    build = sub.add_parser("revalidate", help="重验证完整快照，生成独立receipt和未执行重评草案")
    build.add_argument("--snapshot", required=True); build.add_argument("--out", required=True)
    check = sub.add_parser("verify", help="按当前代码重算receipt；不写数据")
    check.add_argument("--receipt", required=True)
    imp = sub.add_parser("import", help="显式调用本机原子assessment API；绝不修改原job")
    imp.add_argument("--receipt", required=True); imp.add_argument("--base-url", required=True)
    imp.add_argument("--token-file", required=True); imp.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "identity":
            result = engine_identity()
        elif args.command == "snapshot":
            snapshot = snapshot_job(args.data_dir, args.job_id); _write_new(args.out, snapshot)
            result = {"path": str(Path(args.out).resolve()), "job_id": snapshot["job"]["id"], "source_hash": snapshot["source_hash"], "units": len(snapshot["units"])}
        elif args.command == "revalidate":
            receipt = revalidate_snapshot(_read(args.snapshot)); _write_new(args.out, receipt)
            result = {key: receipt[key] for key in ("id", "receipt_hash", "original_counts", "counts", "unit_count", "assessment_count", "usage")}
            result.update(path=str(Path(args.out).resolve()), rerun_requests=len(receipt["rerun_requests"]))
        elif args.command == "verify":
            receipt = verify_receipt(_read(args.receipt))
            result = {"verified": True, "receipt_id": receipt["id"], "receipt_hash": receipt["receipt_hash"], "new_model_requests": 0}
        else:
            result = import_receipt(_read(args.receipt), args.base_url, args.token_file, args.out)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (ValueError, KeyError, TypeError, OSError, sqlite3.Error) as error:
        # Error strings are controlled, never credential-bearing HTTP bodies.
        print(json.dumps({"error": str(error), "type": type(error).__name__, "new_model_requests": 0}, ensure_ascii=False), file=sys.stderr)
        return 2


_LOADED_ENGINE_IDENTITY = engine_identity()


if __name__ == "__main__":
    raise SystemExit(main())
