from __future__ import annotations

import importlib
import json

from .util import ROOT, digest, safe_error


def adapter(project):
    # Only locally installed task packages are executable; no user-supplied module paths.
    if project not in {p.stem for p in (ROOT / "taskpacks").glob("*.json")}:
        raise ValueError("未知项目")
    return importlib.import_module("myeval.adapters." + project)


def packs(describe=False):
    output = []
    for path in sorted((ROOT / "taskpacks").glob("*.json")):
        value = json.loads(path.read_text())
        value["content_hash"] = digest(value)
        if describe:
            for config in value["configurations"]:
                try:
                    config["provenance"] = adapter(value["project"]).describe(config)
                except Exception as error:
                    config["provenance"] = {"unavailable": safe_error(error)}
        output.append(value)
    return output


def freeze(project, case_ids, baseline_id, candidate_id):
    pack = next((p for p in packs(True) if p["project"] == project), None)
    if not pack:
        raise ValueError("项目任务包尚不可用")
    configs = {c["id"]: c for c in pack["configurations"]}
    cases = {c["id"]: c for c in pack["cases"]}
    if baseline_id == candidate_id or baseline_id not in configs or candidate_id not in configs:
        raise ValueError("请选择两个不同的已注册配置")
    if not case_ids or len(set(case_ids)) != len(case_ids) or any(c not in cases for c in case_ids):
        raise ValueError("请选择任务包中的案例，不能重复")
    selected = [cases[c] for c in case_ids]
    if any(c.get("source", {}).get("synthetic") is not True for c in selected):
        raise ValueError("首版执行仅允许已明确标记 synthetic=true 的虚构材料")
    return pack, selected, [configs[baseline_id], configs[candidate_id]]
