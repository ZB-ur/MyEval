"""Small disclosed normative anchors, never represented as human gold."""
from __future__ import annotations
import copy

_ANCHORS = {
    "id": "general-v2-internal-anchors-v1", "version": "1.0.0", "kind": "internal_ai_anchor",
    "name": "通用评分规范锚点", "rubric_id": "general-v2",
    "provenance": "2026-11-18 AI作者按通用量表编写的合成规范性样例；无教师/用户金标。",
    "limitations": ["只检查明显正确/错误及分项分离，不证明评分器业务有效性。", "模型可能合理偏离锚点，分歧需要复核，不能自动改成目标分。"],
    "cases": [
        {"id": "faithful-summary", "family_id": "anchor-summary", "split": "calibration",
         "task": "用一句话忠实整理给定事实，不补充未说明的动机。", "source": {"type": "synthetic", "synthetic": True},
         "input": {"facts": "小舟把蓝色盒子放在桌上。盒里有两支铅笔。"},
         "output": {"text": "小舟把装着两支铅笔的蓝色盒子放在桌上。"},
         "expected_scores": {"faithfulness": 2, "instruction_following": 2, "usefulness": 2, "communication": 2}},
        {"id": "invented-summary", "family_id": "anchor-summary", "split": "calibration",
         "task": "用一句话忠实整理给定事实，不补充未说明的动机。", "source": {"type": "synthetic", "synthetic": True},
         "input": {"facts": "小舟把蓝色盒子放在桌上。盒里有两支铅笔。"},
         "output": {"text": "小舟丢掉了三支红色铅笔，因为他生气了。"},
         "expected_scores": {"faithfulness": 0, "instruction_following": 0, "usefulness": 0, "communication": 2}}
    ]
}


def list_calibrations():
    return [{k: copy.deepcopy(v) for k, v in _ANCHORS.items() if k != "cases"} | {"case_count": len(_ANCHORS["cases"]), "family_count": 1}]


def calibration(identifier):
    if identifier != _ANCHORS["id"]:
        raise ValueError("未知校准集")
    return copy.deepcopy(_ANCHORS)
