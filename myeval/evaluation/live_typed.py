"""Prepared live typed-evidence profile; deliberately NOT wired into the service.

Integration must wait until the owner closes every frozen v1 scoring job. This
module performs no model calls and changes no registry or database on import.
It preserves the public response schema and changes only the explicitly chosen
v2 prompt/validator contract. Historical v1 jobs retain their original identity.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from .schema import prompt_for as legacy_prompt_for
from .revalidation_schema import revalidate_judgment, validator_identity

PROFILE_ID = "live-strict-json-evidence-v1"
PROFILE_VERSION = "1.0.0"
SOURCE_GUARD_ADDITIONS = ("live_typed.py", "revalidation_schema.py", "../util.py")
EVALUATOR_PRESETS = (
    {"id": "codex-astra-v2", "version": "2.0.0", "name": "Astra 独立评阅 · 结构证据 v2",
     "provider": "codex_cli", "model": "gpt-6-astra", "reasoning_effort": "medium", "vision": True,
     "validation_profile": PROFILE_ID, "prompt_profile": PROFILE_ID},
    {"id": "codex-sol-v2", "version": "2.0.0", "name": "Sol 独立评阅 · 结构证据 v2",
     "provider": "codex_cli", "model": "gpt-6-sol", "reasoning_effort": "medium", "vision": True,
     "validation_profile": PROFILE_ID, "prompt_profile": PROFILE_ID},
)

_OLD_QUOTE_RULE = "quote 是该位置原文的逐字子串。"
_TYPED_QUOTE_RULE = (
    "quote 必须忠实定位在该 pointer，按下面的三种证据契约填写："
    "字符串值使用原文的连续逐字子串，不能删去中间片段后拼接，不能替换标点或撇号。"
    "非字符串 JSON 值使用该位置完整、合法的 JSON 字面量，必须类型与完整值相同，"
    "例如定位空数组填写 []，定位整数8填写 8，定位null填写 null，定位false填写 false；"
    "这些只是格式示例，不是本次材料的事实。"
    "不要将缺失字段当null，不要将false当0，不要将空数组当空对象，不要只引用对象的一部分。"
    "优先指向最具体的实际字段；/report/overall_score对应数值8时quote填8，"
    "不能指向/report后只写一个不完整的键值片段。"
    '例如/background/recentStories实际为[]时，pointer填/background/recentStories、quote填[]，'
    '不能填空quote，也不能填仅有"recentStories":[]的局部JSON片段。'
    "有内容的对象或数组，可把pointer指向其中真实字符串叶节点并引用连续逐字文字，"
    "或者保留指向整个容器的pointer并引用该容器完整的合法JSON；不能用空quote代替这两种证据。"
    "数字/数组/对象证据也不能留空quote；标准JSON不能使用NaN、Infinity或重复对象键。"
    "若指向浮点数字，保留数值和整数/小数表示类别，不靠四舍五入凑匹配。"
)
_LIVE_EXTRA = (
    "结构证据只能证明该值或容器状态存在，不能自动证明质量良好；仍须按任务与量表给出理由。"
    "对空输出引用其真实空容器，不凭空编造字符串；如果无法提供合格证据，相关维度标unavailable，"
    "只有量表明确不适用时才用not_applicable。"
    "absence=true只标明遗漏，不免除引用要求。要证明字段缺失，引用其最近真实存在的上级对象的完整JSON，"
    "并解释检查范围；不能指向不存在的字段后用null或空quote冒充证据。"
    "在evidence中，只有source=media且pointer匹配实际attached=true附件时允许quote留空；"
    "这是媒体观察的单独例外，不能套用于source=case或output。"
    "hard_violations目前只接受原交付中的逐字文本引文；纯结构或数字事实放在evidence和分项理由，"
    "不要为填hard_violations虚构文本或使用JSON片段作为文本引文。"
)


def evaluator_presets():
    return copy.deepcopy(list(EVALUATOR_PRESETS))


def profile_identity():
    """Explicit source read; the service must freeze/guard the same dependencies."""
    root = Path(__file__).parent
    names = ("live_typed.py", "revalidation_schema.py", "schema.py", "../util.py")
    identity = {"id": PROFILE_ID, "version": PROFILE_VERSION,
                "implementation": {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in names},
                "validator": validator_identity(), "response_schema": "myeval.judgment.v2"}
    identity["hash"] = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False,
                                               separators=(",", ":")).encode()).hexdigest()
    return identity


def prompt_for(rubric, payload):
    """Alter only trusted instructions; retain rubric and untrusted data bytes."""
    original = legacy_prompt_for(rubric, payload)
    prefix, marker, remainder = original.partition("\nRUBRIC_JSON:\n")
    if not marker or prefix.count(_OLD_QUOTE_RULE) != 1:
        raise ValueError("legacy_prompt_contract_changed; review_live_profile_before_use")
    return prefix.replace(_OLD_QUOTE_RULE, _TYPED_QUOTE_RULE, 1) + _LIVE_EXTRA + marker + remainder


def validate_judgment(value, rubric, payload):
    """Pure validation projection, without falsely labeling a live call as replay.

    Keep raw provider output and the original normalized judgment unchanged.
    The caller copies typed_evidence/transform_log into the unit and each side's
    assessment, with the frozen live profile identity. Real provider usage must
    stay attached to the new model job, not be replaced by revalidation's zeros.
    """
    result = revalidate_judgment(value, rubric, payload)
    return {"profile": {"id": PROFILE_ID, "version": PROFILE_VERSION},
            "operation_kind": "model_judgment", **result}
