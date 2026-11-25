from __future__ import annotations

import importlib
import json
import copy
import threading
import time

from .util import ROOT, digest, safe_error

_description_cache = {}
_description_lock = threading.RLock()


def adapter(project):
    # Only locally installed task packages are executable; no user-supplied module paths.
    if not isinstance(project, str) or not project.isidentifier() or project not in {p["project"] for p in packs()}:
        raise ValueError("未知项目")
    return importlib.import_module("myeval.adapters." + project)


def _describe(pack, *, cached=False, configuration_ids=None):
    for config in pack["configurations"]:
        if configuration_ids is not None and config["id"] not in configuration_ids:
            continue
        key = (pack["project"], digest({k: v for k, v in config.items() if k != "provenance"}))
        if cached:
            with _description_lock:
                previous = _description_cache.get(key)
            if previous and time.monotonic() - previous[0] < 10:
                config["provenance"] = copy.deepcopy(previous[1])
                continue
        try:
            config["provenance"] = adapter(pack["project"]).describe(config)
        except Exception as error:
            config["provenance"] = {"unavailable": safe_error(error)}
        if cached:
            with _description_lock:
                _description_cache[key] = (time.monotonic(), copy.deepcopy(config["provenance"]))


def packs(describe=False):
    output = []
    for path in sorted((ROOT / "taskpacks").glob("*.json")):
        value = json.loads(path.read_text())
        value["content_hash"] = digest(value)
        if describe:
            _describe(value, cached=True)
            value["catalog_provenance_cache_seconds"] = 10
        output.append(value)
    return output


def rubric_catalog():
    """List the fixed local registry without accepting or returning file paths."""
    from .evaluation.schema import rubric_snapshot, score_range

    directory = ROOT / "rubrics"
    catalog = {"schema_version": "myeval.rubrics.v1", "registry": "repository",
               "available": directory.is_dir() and not directory.is_symlink(),
               "rubrics": [], "unavailable": [], "custom_rubric_supported": True}
    if not catalog["available"]:
        return catalog

    def declared_text(value, fallback="unknown"):
        return value if isinstance(value, str) and value.strip() else fallback

    for path in sorted(directory.glob("*.json")):
        # rubric_snapshot appends .json unless the supplied ID already has it.
        identifier = path.name if path.stem.endswith(".json") else path.stem
        if identifier.startswith(".") or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_." for c in identifier):
            continue
        if path.is_symlink() or not path.is_file():
            catalog["unavailable"].append({"id": identifier, "reason": "not_regular_registry_file"})
            continue
        try:
            if path.stat().st_size > 480000:
                raise ValueError("oversized rubric")
            # Validate a supplied object so the canonical validator never opens
            # another path. Its normalized hash matches the scoring snapshot.
            rubric = rubric_snapshot(value=json.loads(path.read_text()))
            dimensions = []
            for key, definition in rubric["dimensions"].items():
                low, high, anchors = score_range(definition)
                anchor_text = {}
                for label, value in definition.items():
                    try:
                        if low <= float(label) <= high:
                            anchor_text[label] = copy.deepcopy(value)
                    except (ValueError, TypeError):
                        pass
                dimensions.append({"id": key, "name": declared_text(definition.get("name"), key),
                                   "description": declared_text(definition.get("description"), ""),
                                   "min": low, "max": high, "anchor_values": sorted(anchors) if anchors else None,
                                   "anchors": anchor_text, "definition": copy.deepcopy(definition)})
            catalog["rubrics"].append({"id": identifier, "declared_id": rubric["id"],
                                       "version": rubric.get("version", "unknown"),
                                       "name": declared_text(rubric.get("name"), rubric["id"]),
                                       "scope": declared_text(rubric.get("scope")),
                                       "project": declared_text(rubric.get("project")),
                                       "dimensions": dimensions, "dimension_count": len(dimensions),
                                       "rubric_hash": digest(rubric)})
        except (OSError, ValueError, TypeError, AttributeError):
            # No exception text, absolute filename, invalid content or link target.
            catalog["unavailable"].append({"id": identifier, "reason": "invalid_or_unreadable_rubric"})
    return catalog


def case_family(case):
    return case.get("family_id") or case["id"]


def freeze(project, case_ids, baseline_id, candidate_id=None, taskpack_id=None, *,
           configuration_ids=None, pack_override=None, authorization=None):
    if not isinstance(case_ids, list) or not case_ids or any(not isinstance(c, str) for c in case_ids) or len(set(case_ids)) != len(case_ids):
        raise ValueError("请选择任务包中的案例，不能重复")
    configuration_ids = configuration_ids if configuration_ids is not None else [baseline_id, candidate_id]
    if (not isinstance(configuration_ids, list) or not 2 <= len(configuration_ids) <= 16
            or any(not isinstance(c, str) or not c for c in configuration_ids)
            or len(set(configuration_ids)) != len(configuration_ids) or baseline_id not in configuration_ids):
        raise ValueError("请选择 2–16 个不同的已注册配置，并指定其中一个为基线")
    # Keep baseline first so old consumers of configurations[0] remain meaningful.
    configuration_ids = [baseline_id, *[c for c in configuration_ids if c != baseline_id]]
    available = [pack_override] if pack_override is not None else [p for p in packs() if p["project"] == project]
    if any(p.get("project") != project for p in available):
        raise ValueError("数据集项目与实验项目不一致")
    if not available:
        raise ValueError("项目任务包尚不可用")
    if taskpack_id is not None:
        if not isinstance(taskpack_id, str) or not taskpack_id:
            raise ValueError("taskpack_id 必须为任务包 ID")
        matching = [p for p in available if p["id"] == taskpack_id]
        if not matching:
            raise ValueError("所选项目中不存在此任务包")
    else:
        matching = [p for p in available
                    if set(configuration_ids) <= {c["id"] for c in p["configurations"]}
                    and set(case_ids) <= {c["id"] for c in p["cases"]}]
        if not matching:
            raise ValueError("没有任务包同时包含所选配置与案例，请明确选择任务包")
    if len(matching) != 1:
        raise ValueError("匹配到多个任务包，请明确指定唯一的 taskpack_id")
    pack = matching[0]
    configs = {c["id"]: c for c in pack["configurations"]}
    cases = {c["id"]: c for c in pack["cases"]}
    if not set(configuration_ids) <= set(configs):
        raise ValueError("所选配置未在任务包或数据集版本中注册")
    if not case_ids or len(set(case_ids)) != len(case_ids) or any(c not in cases for c in case_ids):
        raise ValueError("请选择任务包中的案例，不能重复")
    selected = [cases[c] for c in case_ids]
    if any(c.get("family_id") is not None and (not isinstance(c["family_id"], str) or not c["family_id"].strip()) for c in selected):
        raise ValueError("案例 family_id 必须为非空字符串；未设置时使用案例 ID")
    from .datasets import validate_source
    for case in selected:
        # Preserve original source fields and hashes, including public media licences.
        case["source"] = validate_source(case, authorization or pack.get("authorization"), project)
    # Catalog rendering can cache descriptors. Freezing must always inspect the
    # selected real sources afresh; worker verifies them again before dispatch.
    _describe(pack, configuration_ids=configuration_ids)
    return pack, selected, [configs[c] for c in configuration_ids]
