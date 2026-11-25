from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

from .catalog import adapter
from .datasets import validate_source
from .context import CancelledError, RunContext
from .util import TERMINAL, digest, now, safe_error, write_json


def main():
    request = json.loads(Path(sys.argv[1]).read_text())
    ctx = RunContext(Path(sys.argv[1]).parent, request["id"], request["experiment_id"], request["timeout_seconds"])
    ctx.emit("worker.started", {"project": request["project"], "execution_attempt": request["attempt"]})
    try:
        # Long-lived queues may outlast a scoped grant. Recheck before touching
        # the product boundary; old synthetic cases remain backward compatible.
        checked_source = validate_source(request["case"], request.get("authorization"), request["project"])
        ctx.emit("source.authorization_verified", {"source_type": checked_source["type"],
                                                   "authorization_hash": checked_source.get("authorization_hash")})
        implementation = adapter(request["project"])
        observed = implementation.describe(request["configuration"])
        expected = request["configuration"].get("provenance")
        ctx.emit("source.verified", {"frozen_hash": digest(expected), "current_hash": digest(observed), "matches": expected == observed})
        if expected is not None and expected != observed:
            result = {"status": "failed", "error": {"code": "source_changed_since_freeze", "message": "业务源码或适配器在实验建立后改变；未发送产品请求，请用当前版本建立新实验"},
                      "observed_configuration": observed, "review_output": {"说明": "版本检查未通过，未开始业务请求"}, "usage": None, "cost": None}
        else:
            result = implementation.run(request["case"], request["configuration"], ctx)
        if not isinstance(result, dict) or result.get("status") not in TERMINAL:
            raise ValueError("Adapter returned an invalid terminal status")
    except CancelledError as error:
        result = {"status": "cancelled", "error": {"code": "cooperative_cancel", "message": safe_error(error)},
                  "limitations": ["本地执行已响应停止；供应商是否停止计算及计费未知。"]}
    except Exception as error:
        # Deliberately omit stack locals, provider bodies, and raw stderr.
        result = {"status": "failed", "error": {"code": type(error).__name__, "message": safe_error(error)},
                  "error_frames": [{"file": Path(f.filename).name, "line": f.lineno, "function": f.name}
                                   for f in traceback.extract_tb(error.__traceback__)]}
    result.setdefault("usage", None)
    result.setdefault("cost", None)
    result.setdefault("observed_configuration", {"model": None, "reasoning_effort": None})
    result.setdefault("assessments", [])
    result.setdefault("limitations", [])
    result.setdefault("review_output", {"说明": "本次未获得可交付结果"})
    ctx.emit("worker.finished", {"status": result["status"], "error": result.get("error")})
    result["recorded_at"] = now()
    write_json(ctx.root / "result.json", result)


if __name__ == "__main__":
    main()
