"""Read-only, portable registry of complete deterministic revalidation receipts.

The optional local register command publishes already-verified artifacts. It never
imports assessments, changes a job, calls a model, or reruns a semantic validator.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import io
import json
import os
import re
import shutil
import stat
import tempfile
import threading
import zipfile
from collections import Counter, OrderedDict
from pathlib import Path
from urllib.parse import quote

from .snapshots import _check_path
from .util import now

ARTIFACT_SCHEMA = "myeval.revalidation-artifact.v1"
RECEIPT_SCHEMA = "myeval.evidence-revalidation.v1"
RECEIPT_ID = re.compile(r"rv1-[0-9a-f]{64}")
STATUSES = ("valid", "invalid", "no_response", "ineligible")
TERMINAL_JOBS = {"succeeded", "completed_with_errors", "cancelled", "budget_exhausted", "interrupted"}
TERMINAL_UNITS = {"succeeded", "invalid", "failed", "timed_out", "cancelled", "interrupted", "unavailable", "budget_exhausted"}
MAX_RECEIPT_BYTES = 64 * 1024 * 1024


def _hash(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _json(raw):
    value = json.loads(raw, object_pairs_hook=_pairs)
    if not isinstance(value, dict):
        raise ValueError("artifact_requires_json_object")
    _hash(value)  # Reject nonfinite numbers even in fields not projected to the UI.
    return value


def _read(path, root, limit):
    _check_path(path, root)
    current = path.stat()
    if not stat.S_ISREG(current.st_mode) or current.st_size > limit:
        raise ValueError("artifact_not_regular_or_too_large")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ValueError("artifact_not_regular_or_too_large")
        raw = handle.read(limit + 1)
        after = os.fstat(handle.fileno())
    identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    if len(raw) > limit or len({identity(s) for s in (current, before, after, path.stat())}) != 1:
        raise ValueError("artifact_changed_during_read")
    return raw


def _same_counts(declared, observed):
    return (isinstance(declared, dict) and all(isinstance(k, str) and type(v) is int and v >= 0 for k, v in declared.items())
            and {k: v for k, v in declared.items() if v} == dict(observed))


def _verify_receipt(receipt):
    """Check immutable identity and full denominators, not judgment correctness."""
    try:
        if receipt.get("schema_version") != RECEIPT_SCHEMA or receipt.get("operation_kind") != "deterministic_revalidation":
            raise ValueError("unsupported_revalidation_receipt")
        if _hash({k: v for k, v in receipt.items() if k != "receipt_hash"}) != receipt["receipt_hash"]:
            raise ValueError("receipt_hash_mismatch")
        source, engine = receipt["source_snapshot"], receipt["engine"]
        job, originals, units = source["job"], source["units"], receipt["units"]
        if (not isinstance(job["evaluator"], dict) or not isinstance(job["rubric"], dict)
                or not isinstance(job["rubric"].get("id"), str)
                or not all(isinstance(job[k], str) for k in ("evaluator_hash", "rubric_hash"))
                or not isinstance(receipt["limitations"], list)):
            raise ValueError("malformed_revalidation_projection")
        if source.get("schema_version") != "myeval.revalidation-source.v1":
            raise ValueError("unsupported_revalidation_source")
        if (_hash({k: source[k] for k in ("job", "units", "experiment_binding", "attempt_bindings")}) != source["source_hash"]
                or source["source_hash"] != receipt["source_hash"]
                or _hash({"job": job, "units": originals}) != source["job_content_hash"]):
            raise ValueError("source_hash_mismatch")
        if (_hash({k: v for k, v in engine.items() if k != "hash"}) != engine["hash"]
                or receipt["id"] != "rv1-" + _hash({"source_hash": receipt["source_hash"], "engine_hash": engine["hash"]})):
            raise ValueError("receipt_engine_identity_mismatch")
        if (not isinstance(job["id"], str) or not job["id"] or receipt["original_job_id"] != job["id"]
                or receipt["experiment_id"] != job["experiment_id"] or receipt["original_job_status"] != job["status"]):
            raise ValueError("original_job_identity_mismatch")
        if job["status"] not in TERMINAL_JOBS:
            raise ValueError("original_job_not_terminal")
        if (not isinstance(originals, list) or not originals or not isinstance(units, list)
                or type(receipt["unit_count"]) is not int or type(job["unit_count"]) is not int
                or not len(originals) == len(units) == receipt["unit_count"] == job["unit_count"]):
            raise ValueError("complete_unit_denominator_mismatch")
        original_index = {u["id"]: u for u in originals}
        if len(original_index) != len(originals) or any(u["job_id"] != job["id"] or u["status"] not in TERMINAL_UNITS for u in originals):
            raise ValueError("duplicate_or_wrong_source_unit")
        original_counts = Counter(u["status"] for u in originals)
        if not _same_counts(job["counts"], original_counts) or not _same_counts(receipt["original_counts"], original_counts):
            raise ValueError("original_status_denominator_mismatch")
        seen = set()
        for unit in units:
            original = unit["original"]
            validation = unit.get("validation")
            if (not isinstance(original, dict) or validation is not None and not isinstance(validation, dict)
                    or not isinstance(unit.get("original_validation_issues", []), list)
                    or isinstance(validation, dict) and any(not isinstance(validation.get(k, []), list) for k in ("validation_issues", "transform_log"))):
                raise ValueError("malformed_revalidation_unit_projection")
            identifier = original["unit_id"]
            if identifier in seen or identifier not in original_index or original["job_id"] != job["id"]:
                raise ValueError("revalidated_unit_denominator_mismatch")
            seen.add(identifier)
            if original["unit_hash"] != _hash(original_index[identifier]) or original["unit_status"] != original_index[identifier]["status"]:
                raise ValueError("original_unit_identity_mismatch")
            if unit["status"] not in STATUSES or type(unit["eligible_for_assessment"]) is not bool:
                raise ValueError("unsupported_revalidated_unit_status")
        if seen != set(original_index) or not _same_counts(receipt["counts"], Counter(u["status"] for u in units)):
            raise ValueError("revalidation_status_denominator_mismatch")
        items = receipt["assessment_items"]
        if (not isinstance(items, list) or type(receipt["assessment_count"]) is not int
                or len(items) != receipt["assessment_count"] or _hash(items) != receipt["assessment_plan_hash"]):
            raise ValueError("assessment_plan_hash_or_count_mismatch")
        usage = receipt["usage"]
        if usage.get("scope") != "incremental_revalidation_only" or any(usage.get(k) != 0 for k in ("model_requests", "dispatch_attempts", "total_tokens", "cost")):
            raise ValueError("unexpected_incremental_model_usage")
    except (KeyError, TypeError, AttributeError) as error:
        raise ValueError("malformed_revalidation_receipt") from error
    return receipt


class RevalidationReceipts:
    def __init__(self, data_dir):
        self.root = Path(data_dir).expanduser().absolute()
        self.directory = self.root / "evaluation" / "revalidations"
        self.lock = threading.RLock()
        self.cache = OrderedDict()

    def _location(self, identifier):
        if not isinstance(identifier, str) or not RECEIPT_ID.fullmatch(identifier):
            raise ValueError("revalidation_id_format_invalid")
        location = self.directory / identifier
        _check_path(location, self.root, allow_missing=True)
        if not location.is_dir():
            raise KeyError("revalidation_receipt_missing")
        return location

    def _key(self, location):
        values = []
        for name in ("manifest.json", "receipt.json"):
            path = _check_path(location / name, self.root)
            s = path.stat()
            values.append((s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns))
        return tuple(values)

    def _bundle(self, identifier):
        location = self._location(identifier)
        manifest_raw = _read(location / "manifest.json", self.root, 1024 * 1024)
        manifest = _json(manifest_raw)
        if (manifest.get("schema_version") != ARTIFACT_SCHEMA or manifest.get("id") != identifier
                or manifest.get("receipt_file") != "receipt.json"
                or _hash({k: v for k, v in manifest.items() if k != "manifest_hash"}) != manifest.get("manifest_hash")):
            raise ValueError("revalidation_manifest_invalid")
        raw = _read(location / "receipt.json", self.root, MAX_RECEIPT_BYTES)
        if hashlib.sha256(raw).hexdigest() != manifest.get("receipt_sha256") or len(raw) != manifest.get("receipt_bytes"):
            raise ValueError("revalidation_artifact_hash_mismatch")
        receipt = _verify_receipt(_json(raw))
        if any(receipt[k] != manifest.get(k) for k in ("id", "receipt_hash", "original_job_id", "experiment_id", "unit_count")):
            raise ValueError("revalidation_manifest_identity_mismatch")
        return manifest, receipt, manifest_raw, raw

    def get(self, identifier):
        with self.lock:
            location = self._location(identifier)
            key = self._key(location)
            if identifier in self.cache and self.cache[identifier][0] == key:
                self.cache.move_to_end(identifier)
                return copy.deepcopy(self.cache[identifier][1])
            manifest, receipt, _, _ = self._bundle(identifier)
            if self._key(location) != key:
                raise ValueError("revalidation_artifact_changed_during_read")
            job = receipt["source_snapshot"]["job"]
            original_url = "/api/evaluation-jobs/" + quote(receipt["original_job_id"], safe="")
            summary = {k: receipt[k] for k in ("id", "schema_version", "operation_kind", "receipt_hash", "source_hash", "experiment_id",
                                              "original_job_id", "original_job_status", "original_counts", "unit_count", "assessment_count", "usage", "limitations")}
            summary.update(status="verified_artifact", label="原 AI 判断重新验证", registered_at=manifest.get("registered_at"),
                           counts={status: receipt["counts"].get(status, 0) for status in STATUSES},
                           original_job_title=job.get("title"), original_job_url=original_url,
                           original_job_page="/#job/" + quote(receipt["original_job_id"], safe=""),
                           url="/api/evaluation-revalidations/" + identifier, export_url="/api/evaluation-revalidations/" + identifier + "/export",
                           evaluator={"id": job["evaluator"].get("id"), "name": job["evaluator"].get("name"), "hash": job["evaluator_hash"]},
                           rubric={"id": job["rubric"]["id"], "version": job.get("rubric_version"), "hash": job["rubric_hash"]},
                           engine={k: receipt["engine"].get(k) for k in ("id", "version", "hash", "validator")},
                           assessment_count_kind="planned_not_import_confirmation",
                           verification={"artifact": "sha256_verified", "receipt": "canonical_hash_and_complete_denominators",
                                         "semantic_validation": "not_recomputed", "new_model_requests": 0})
            units = []
            for unit in receipt["units"]:
                original, validation = unit["original"], unit.get("validation") or {}
                units.append({"original": {k: original.get(k) for k in ("job_id", "unit_id", "unit_hash", "unit_status", "logical_unit_hash", "case_id", "family_id", "evaluator_hash", "rubric_hash", "payload_hash", "raw_response_hash", "normalized_response_hash")},
                              "status": unit["status"], "eligible_for_assessment": unit["eligible_for_assessment"],
                              "original_validation_issues": unit.get("original_validation_issues", []),
                              "validation_issues": validation.get("validation_issues", []),
                              "transform_log": validation.get("transform_log", []),
                              "ineligibility_reason": unit.get("ineligibility_reason")})
            value = {**summary, "units": units}
            self.cache[identifier] = (key, value)
            while len(self.cache) > 64:
                self.cache.popitem(last=False)
            return copy.deepcopy(value)

    def list(self, experiment_id=None, original_job_id=None):
        result = {"schema_version": "myeval.revalidation-list.v1", "receipts": [], "invalid": [],
                  "operation_kind": "deterministic_revalidation", "counts_scope": "per_original_job_no_cross_receipt_pooling"}
        _check_path(self.directory, self.root, allow_missing=True)
        if not self.directory.exists():
            return result
        for location in sorted(self.directory.iterdir()):
            if not RECEIPT_ID.fullmatch(location.name):
                continue
            try:
                value = self.get(location.name)
                if experiment_id and value["experiment_id"] != experiment_id or original_job_id and value["original_job_id"] != original_job_id:
                    continue
                result["receipts"].append({k: v for k, v in value.items() if k != "units"})
            except (ValueError, KeyError, OSError, TypeError, AttributeError):
                # Invalid artifacts cannot be reliably assigned to an experiment.
                result["invalid"].append({"id": location.name, "status": "invalid_artifact", "error": "重验收材料缺失或完整性校验失败", "filter_scope": "unassigned"})
        return result

    def export(self, identifier):
        files = self.artifact_files(identifier)
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
            for item in files:
                archive.writestr(item["name"], item["raw"])
        return stream.getvalue()

    def artifact_files(self, identifier):
        """Return the original files after validating their shared identities."""
        _, _, manifest_raw, raw = self._bundle(identifier)
        return [{"name": name, "raw": content, "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}
                for name, content in (("manifest.json", manifest_raw), ("receipt.json", raw))]

    def register(self, receipt_path):
        """Local explicit publication only; no HTTP mutation endpoint exists."""
        path = Path(receipt_path).expanduser().absolute()
        raw = _read(path, path.parent, MAX_RECEIPT_BYTES)
        receipt = _verify_receipt(_json(raw))
        identifier = receipt["id"]
        if not RECEIPT_ID.fullmatch(identifier):
            raise ValueError("revalidation_id_format_invalid")
        with self.lock:
            _check_path(self.directory, self.root, allow_missing=True)
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            target = self.directory / identifier
            if target.exists() or target.is_symlink():
                manifest, _, _, _ = self._bundle(identifier)
                if manifest["receipt_sha256"] != hashlib.sha256(raw).hexdigest():
                    raise ValueError("immutable_receipt_already_registered_with_other_bytes")
                return {k: v for k, v in self.get(identifier).items() if k != "units"}
            manifest = {"schema_version": ARTIFACT_SCHEMA, "registered_at": now(), "receipt_file": "receipt.json",
                        "receipt_bytes": len(raw), "receipt_sha256": hashlib.sha256(raw).hexdigest(),
                        **{k: receipt[k] for k in ("id", "receipt_hash", "original_job_id", "experiment_id", "unit_count")}}
            manifest["manifest_hash"] = _hash(manifest)
            staging = Path(tempfile.mkdtemp(prefix=".tmp-", dir=self.directory))
            try:
                for name, content in (("receipt.json", raw), ("manifest.json", (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode())):
                    with (staging / name).open("xb") as handle:
                        handle.write(content)
                        handle.flush()
                        os.fsync(handle.fileno())
                    (staging / name).chmod(0o600)
                os.rename(staging, target)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
            return {k: v for k, v in self.get(identifier).items() if k != "units"}


def main():
    parser = argparse.ArgumentParser(description="完整重验收 receipt 的只读目录与本机显式登记；不评分、不导入 assessment")
    sub = parser.add_subparsers(dest="command", required=True)
    register = sub.add_parser("register", help="登记已由原验证器 verify 的完整最终 receipt；不替代语义验证")
    register.add_argument("--data-dir", type=Path, required=True)
    register.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = RevalidationReceipts(args.data_dir).register(args.receipt)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (OSError, ValueError, KeyError):
        print(json.dumps({"error": "重验收材料登记失败；请检查文件完整性、完整分母与目标目录", "new_model_requests": 0}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
