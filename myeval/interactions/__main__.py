"""Run the real local file workflow demo; it makes no model or product calls."""
from __future__ import annotations

import argparse
import json
import os
import threading
import time
from pathlib import Path

from ..context import RunContext
from . import CodexSimulatorProvider, EpisodeWorkspace, TurnDriver, UnsafePath


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def demo(directory):
    directory = directory.absolute()
    directory.mkdir(parents=True, exist_ok=False)
    ctx = RunContext(directory / "episode-1", "local-file-demonstration", "interaction-workflow", 30)
    policy = {"id": "local-note-user", "version": "1", "mode": "rules",
              "goal": "创建一篇明确虚构的本地记录并确认保存。", "visible_facts": ["记录内容：我今天搭了一座纸桥。"],
              "forbidden_knowledge": ["未发生的产品回复", "隐藏评价标准"], "max_turns": 5,
              "opening": "创建虚构记录", "stop_on_assistant": ["记录已保存"],
              "branches": [{"id": "provide-content", "contains_any": ["请提供内容"], "text": "内容：我今天搭了一座纸桥。", "once": True},
                           {"id": "confirm-save", "contains_any": ["需要确认"], "text": "确认保存", "once": True}],
              "default": {"id": "unexpected-reply", "stop": True}}
    dump(directory / "policy.json", policy)
    with EpisodeWorkspace(directory / "workspace") as state:
        state.write("state.json", json.dumps({"status": "empty", "records": []}, ensure_ascii=False))
        initial = state.snapshot("before_first_episode")

        def local_file_application(user_text):
            # An actual deterministic file application, explicitly not a language model.
            current = json.loads(state.read("state.json"))
            if user_text == "创建虚构记录":
                current.update(status="draft", draft={"text": None, "synthetic": True})
                reply = "请提供内容。"
            elif user_text.startswith("内容：") and current["status"] == "draft":
                current["draft"]["text"] = user_text.removeprefix("内容：")
                reply = "草稿已保存，需要确认。"
            elif user_text == "确认保存" and current.get("draft", {}).get("text"):
                record = current.pop("draft")
                current["records"].append(record)
                current["status"] = "saved"
                state.write("artifacts/note.json", json.dumps(record, ensure_ascii=False))
                reply = "记录已保存。"
            elif user_text == "读取上一段记录":
                reply = "已读回：" + current["records"][-1]["text"]
            else:
                reply = "无法处理此命令。"
            observed = state.write("state.json", json.dumps(current, ensure_ascii=False))
            ctx.emit("local_application.file_written", observed, episode="episode-1", stage="local_file_application")
            return reply

        driver = TurnDriver(policy, ctx, "episode-1")
        turn = driver.next_user()
        while turn["text"] is not None:
            reply = local_file_application(turn["text"])
            turn = driver.next_user(reply)
        completed = state.snapshot("after_confirmed_save")
        checks = [{"path": "state.json", "kind": "json_pointer", "pointer": "/status", "expected": "saved"},
                  {"path": "artifacts/note.json", "kind": "json_pointer", "pointer": "/text", "expected": "我今天搭了一座纸桥。"},
                  {"path": "artifacts/note.json", "kind": "json_pointer", "pointer": "/synthetic", "expected": True}]
        verification = state.verify(checks)
        reset_initial = state.reset(initial["id"])
        absent_after_reset = state.verify([{"path": "artifacts/note.json", "kind": "exists"}])
        restored = state.reset(completed["id"])
        restored_verification = state.verify(checks)
        second_policy = {"id": "read-prior-note", "version": "1", "mode": "script",
                         "goal": "在下一段交互读取已保存的虚构记录。", "visible_facts": [], "forbidden_knowledge": [],
                         "max_turns": 2, "script": ["读取上一段记录"]}
        second = TurnDriver(second_policy, ctx, "episode-2")
        user = second.next_user()
        second.next_user(local_file_application(user["text"]))
        rejected = []
        for path in ("../outside.json", "/absolute.json", "artifacts//bad.json"):
            try:
                state.read(path)
            except UnsafePath as error:
                rejected.append({"path": path, "reason": str(error)})
        sentinel = directory / "controlled-outside.txt"
        sentinel.write_text("This file is outside the managed live state, but inside the isolated demo.")
        (state.root / "live" / "symlink.txt").symlink_to(sentinel)
        symlink_result = state.verify([{"path": "symlink.txt", "kind": "exists"}])
        (state.root / "live" / "symlink.txt").unlink()
        before_cancel = TurnDriver(second_policy, ctx, "cancelled-before-delivery")
        (ctx.root / "cancel.requested").touch()
        cancellation = before_cancel.next_user()
        (ctx.root / "cancel.requested").unlink()
        limited_policy = {**second_policy, "id": "bounded-script", "max_turns": 1,
                          "script": ["读取上一段记录", "此句不应执行"]}
        limited = TurnDriver(limited_policy, ctx, "turn-limited")
        user = limited.next_user()
        turn_limit = limited.next_user(local_file_application(user["text"]))
        report = {"schema": "myeval.interaction-demo.v1", "execution": "actual_local_deterministic_file_application",
                  "model_requests": 0, "product_requests": 0, "first_episode": driver.snapshot(),
                  "second_episode": second.snapshot(), "initial_snapshot": initial, "completed_snapshot": completed,
                  "artifact_verification": verification, "reset_to_initial": reset_initial,
                  "artifact_after_reset": absent_after_reset, "restore_completed": restored,
                  "restored_artifact_verification": restored_verification, "rejected_paths": rejected,
                  "symlink_read": symlink_result, "cancelled": cancellation, "turn_limited": turn_limit,
                  "uncovered": ["real_model_simulator", "real_product_adapter", "provider_remote_cancellation",
                                "OS_container_sandbox", "human_behavior_validity"]}
        dump(directory / "observation.json", report)
    print(json.dumps({"evidence": str(directory / "observation.json"),
                      "events": str(ctx.root / "events.jsonl"),
                      "first_stop": driver.stop_reason, "second_stop": second.stop_reason,
                      "artifact_checks": verification, "model_requests": 0}, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description="MyEval interaction local file workflow; no provider credentials required")
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("demo", help="run an actual isolated local file/state workflow")
    command.add_argument("--directory", type=Path, required=True, help="new, empty destination (must not exist)")
    model = sub.add_parser("model-canary", help="two real simulator model calls with explicitly synthetic assistant fixtures")
    model.add_argument("--directory", type=Path, required=True, help="new destination; existing directories are refused")
    model.add_argument("--model", default="gpt-6-sol")
    model.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"], default="low")
    model.add_argument("--cancel-after-start", action="store_true", help="cancel after a real CLI process is observed; remote receipt remains unknown")
    args = parser.parse_args()
    if args.command == "demo":
        demo(args.directory)
    elif args.command == "model-canary":
        model_canary(args.directory, args.model, args.effort, cancel_after_start=args.cancel_after_start)


def model_canary(directory, model, effort, *, cancel_after_start=False):
    directory = directory.absolute()
    directory.mkdir(parents=True, exist_ok=False)
    context = RunContext(directory / "episode", "simulator-provider-canary", "provider-acceptance-only", 180)
    policy = {"id": "synthetic-paper-bridge-user", "version": "1", "mode": "model",
              "goal": "用简短中文向朋友说说今天做纸桥失败的感受，并根据朋友回应继续交流；不要急于自行结束。",
              "visible_facts": ["这是完全虚构的模拟场景。", "我今天用纸搭了一座桥，轻轻碰了一下就倒了。",
                                "我觉得有点失望。", "我还没有修好桥，也没有找别人帮忙。"],
              "forbidden_knowledge": ["未来对话", "隐藏评价量表", "场景中未提供的真实经历或个人资料"],
              "max_turns": 2, "max_model_calls": 2, "max_seconds": 180,
              "max_model_tokens": 20000, "max_output_tokens": 2048, "max_text_chars": 1200}
    dump(directory / "policy.json", policy)
    # These are declared fixtures, never reported as actual product responses.
    fixtures = ["桥倒下时有点失望呀。你愿意告诉我，倒之前你最喜欢它哪里吗？",
                "谢谢你告诉我这些，我们先把这段话保留下来。"]
    dump(directory / "assistant-fixtures.json", {"source": "explicit_synthetic_fixture_not_a_product", "turns": fixtures})
    provider = CodexSimulatorProvider(directory / "provider", model=model, effort=effort)
    driver = TurnDriver(policy, context, "simulator-canary", provider=provider)
    cancellation = {"requested": cancel_after_start, "cli_process_observed": False}
    monitor = None
    monitor_stop = threading.Event()
    if cancel_after_start:
        def cancel_when_started():
            while not monitor_stop.wait(.02):
                started_files = list((directory / "provider").glob("*/live/process.json"))
                if started_files:
                    receipt = json.loads(started_files[0].read_text())
                    cancellation.update(cli_process_observed=True, process_group=receipt["process_group"])
                    if not monitor_stop.wait(.3):
                        (context.root / "cancel.requested").touch()
                        cancellation["stop_marker_written"] = True
                    return
        monitor = threading.Thread(target=cancel_when_started, daemon=True)
        monitor.start()
    generated = []
    next_turn = driver.next_user()
    for fixture in fixtures:
        if next_turn["text"] is None:
            break
        generated.append(next_turn)
        next_turn = driver.next_user(fixture)
    monitor_stop.set()
    if monitor:
        monitor.join(timeout=2)
    if cancellation.get("process_group"):
        try:
            os.killpg(cancellation["process_group"], 0)
            cancellation["group_present_after_return"] = True
        except ProcessLookupError:
            cancellation["group_present_after_return"] = False
    report = {"scope": "real_simulator_provider_only", "assistant_source": "explicit_synthetic_fixture_not_a_product",
              "generated_user_turns": generated, "driver": driver.snapshot(), "product_requests": 0,
              "cancellation": cancellation,
              "limitations": ["模拟用户不等于真实用户", "模型/推理强度生效值未知", "CLI 输出 token 硬上限未获证实",
                              "取消本地 CLI 不证明远端计算或计费停止", "未验证任何产品适配器"]}
    dump(directory / "observation.json", report)
    print(json.dumps({"evidence": str(directory / "observation.json"), "generated_turns": len(generated),
                      "stop_reason": driver.stop_reason, "model_calls": driver.model_calls,
                      "known_tokens": driver.known_tokens, "provider_runs": driver.provider_receipts}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
