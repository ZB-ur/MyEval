"""A deliberately scripted, offline Agent for exploring the real MyEval workflow.

The responses are public fixtures, not model output or evidence of AI quality.
Execution, source freezing, events, persistence and review use the normal runner.
"""
from __future__ import annotations

import copy
import hashlib

from ..util import ROOT

VERSION = "scripted-demo-1.0.0"
STYLES = {"summary", "actions", "evidence"}
NOTICE = "预编写的虚构演示输出；不是模型生成，也不是产品优化证据。"


def _root():
    return ROOT


def describe(configuration):
    files = ("myeval/adapters/demo.py", "taskpacks/demo.json")
    hashes = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in files}
    style = configuration.get("parameters", {}).get("style")
    return {
        "adapter": VERSION,
        "adapter_sha256": hashes["myeval/adapters/demo.py"],
        "available": style in STYLES,
        "source": {"project_root": str(ROOT), "entrypoint": "myeval/adapters/demo.py", "files": hashes},
        "requested": {"model": None, "reasoning_effort": None, "style": style},
        "effective": {"model": None, "reasoning_effort": None, "style": style if style in STYLES else None},
        "capabilities": {"tasks": ["office_reply"], "fixed_context_rerun": True,
                         "isolated_full_run": False, "trace_rescore": "unsupported",
                         "external_requests": False, "model_generation": False,
                         "cancel": "cooperative_local", "environment_reset": True},
        "mode_definition": "通过普通 worker 读取冻结的虚构案例，返回随仓库提供的预编写回复。",
        "limitations": [NOTICE, "运行耗时仅为本地演示执行耗时，不代表模型延迟。",
                        "不运行模型评分；模型用量与费用保留为空。"],
    }


def run(case, configuration, ctx):
    ctx.check_cancelled()
    style = configuration.get("parameters", {}).get("style")
    result = {
        "output": None, "review_output": None,
        "observed_configuration": {"model": None, "reasoning_effort": None,
                                   "runtime": "scripted-local-demo", "style": style},
        "usage": None, "cost": None, "assessments": [],
        "limitations": describe(configuration)["limitations"],
    }
    if (configuration.get("mode") != "fixed_context_rerun"
            or case.get("task") != "office_reply" or style not in STYLES):
        return {**result, "status": "unsupported",
                "error": {"code": "demo_unsupported_configuration", "message": "演示只支持已登记的办公回复配置。"}}

    ctx.emit("demo.input_loaded", {"case_id": case["id"], "synthetic": True,
                                   "external_requests": 0}, stage="input")
    if case.get("demo_failure") is True:
        ctx.emit("demo.failure_demonstrated", {"reason": "explicit_fixture_failure",
                                               "notice": "这是任务包明确声明的故障演练，不是模型失败。"}, stage="delivery")
        return {**result, "status": "failed",
                "error": {"code": "demo_intentional_failure",
                          "message": "演示故障：附件未提供。该案例按设计失败，用于查看错误、轨迹和重试保留。"}}

    templates = case.get("demo_outputs", {})
    if style not in templates:
        return {**result, "status": "unsupported",
                "error": {"code": "demo_fixture_missing", "message": "冻结案例未提供该风格的演示回复。"}}
    answer = copy.deepcopy(templates[style])
    output = {"演示说明": NOTICE, "回复": answer["reply"], "引用": answer.get("quotes", [])}
    source_text = case.get("input", {}).get("material", "")
    quotes = answer.get("quotes", [])
    result["assessments"] = [
        {"kind": "rule", "evaluator": "demo-observable-rules", "version": "1",
         "dimension": "非空回复", "status": "pass" if answer.get("reply", "").strip() else "fail",
         "reason": "只检查是否有文本，不评价回复质量。"},
    ]
    if quotes:
        quotes_match = all(isinstance(quote, str) and bool(quote) and quote in source_text for quote in quotes)
        result["assessments"].append(
            {"kind": "rule", "evaluator": "demo-observable-rules", "version": "1",
             "dimension": "引用原文匹配", "status": "pass" if quotes_match else "fail",
             "reason": "只核对引用是否为本案例原文的连续子串，不判断语义或充分性。"})
    else:
        result["limitations"].append("本输出没有引用，未执行引用原文匹配核对。")
    ctx.check_cancelled()
    ctx.emit("demo.output_prepared", {"scripted_fixture": True, "model_calls": 0,
                                      "output": output}, stage="delivery")
    return {**result, "status": "succeeded", "output": output, "review_output": output}
