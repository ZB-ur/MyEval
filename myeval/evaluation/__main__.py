"""Local standalone scoring commands; use the HTTP CLI when a server owns the worker."""
from __future__ import annotations
import argparse
import json
import sys
import time
from pathlib import Path

from ..store import Store
from . import EvaluationService, summarize


def main():
    parser = argparse.ArgumentParser(description="独立评分与历史重评（不运行产品）")
    parser.add_argument("--data-dir", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("evaluators")
    sub.add_parser("calibrations")
    listing = sub.add_parser("jobs"); listing.add_argument("--experiment-id")
    get = sub.add_parser("job"); get.add_argument("id")
    create = sub.add_parser("create"); create.add_argument("request", help="JSON 文件，或 - 从标准输入读取"); create.add_argument("--wait", action="store_true")
    calibrate = sub.add_parser("calibrate"); calibrate.add_argument("--evaluator-id", default="codex-astra-v1")
    cancel = sub.add_parser("cancel"); cancel.add_argument("id")
    analysis = sub.add_parser("analysis"); analysis.add_argument("experiment_id")
    review = sub.add_parser("review-queue"); review.add_argument("--experiment-id")
    args = parser.parse_args()
    store = Store(Path(args.data_dir))
    service = None
    try:
        if args.command == "analysis":
            print(json.dumps(summarize(store.export(args.experiment_id)), ensure_ascii=False, indent=2)); return
        owns_worker = args.command == "calibrate" or (args.command == "create" and args.wait)
        service = EvaluationService(store, autostart=owns_worker)
        if args.command == "evaluators":
            result = service.list_evaluators()
        elif args.command == "calibrations":
            result = service.list_calibrations()
        elif args.command == "jobs":
            result = service.list_jobs(args.experiment_id)
        elif args.command == "job":
            result = service.get_job(args.id)
        elif args.command == "cancel":
            result = service.cancel_job(args.id)
        elif args.command == "review-queue":
            result = service.review_queue(args.experiment_id)
        else:
            body = ({"calibration_id": "general-v2-internal-anchors-v1", "evaluator_id": args.evaluator_id}
                    if args.command == "calibrate" else json.loads(sys.stdin.read() if args.request == "-" else Path(args.request).read_text()))
            result = service.create_job(body)
            if owns_worker:
                print(json.dumps({"job_id": result["id"], "status": result["status"]}, ensure_ascii=False), file=sys.stderr)
                while result["status"] in {"queued", "running", "cancelling"}:
                    time.sleep(.5)
                    result = service.get_job(result["id"])
                if args.command == "calibrate":
                    result["calibration_report"] = service.calibration_report(result["id"])
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        if service is not None:
            service.close()
        store.db.close()


if __name__ == "__main__":
    main()
