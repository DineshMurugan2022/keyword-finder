import argparse
import asyncio
import json
import logging
from pathlib import Path
from uuid import uuid4

from .browser import collect as browser_collect
from .config import data_dir
from .evidence import verify
from .models import CheckRequest
from .store import Store


def main():
    parser = argparse.ArgumentParser(description="Chrome ranking checks and evidence")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("demo", help="Capture a clearly labelled local fixture in Chrome (no live Google)")

    for name in ("check", "enqueue"):
        command = commands.add_parser(name)
        command.add_argument("--config", type=Path, default=Path("project.json"))
        if name == "enqueue":
            command.add_argument("--key", default=None)

    worker = commands.add_parser("worker")
    worker.add_argument("--once", action="store_true")

    sched = commands.add_parser("scheduler", help="Run the daily auto-enqueue scheduler")
    sched.add_argument("--config", type=Path, default=Path("project.json"))

    status = commands.add_parser("status")
    status.add_argument("job_id")

    verification = commands.add_parser("verify")
    verification.add_argument("directory", type=Path)

    args = parser.parse_args()

    if args.command == "worker":
        from .worker import run
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
        asyncio.run(run(args.once))
        return

    if args.command == "scheduler":
        from .scheduler import run as scheduler_run
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
        scheduler_run(args.config)
        return

    if args.command == "verify":
        ok = verify(args.directory)
        print("Evidence hashes match" if ok else "Evidence verification FAILED")
        raise SystemExit(0 if ok else 1)

    if args.command == "status":
        print(json.dumps(Store().get(args.job_id), indent=2))
        return

    if args.command == "demo":
        request = CheckRequest(website="example.com", keyword="example keyword", max_results=3)
        result = asyncio.run(browser_collect(request, demo=True))
    else:
        request = CheckRequest.model_validate_json(args.config.read_text(encoding="utf-8"))
        if args.command == "enqueue":
            print(json.dumps(Store().enqueue(request, args.key or uuid4().hex), indent=2))
            return
        # Production check via SerpAPI
        from .serpapi_collector import collect as serpapi_collect
        result = asyncio.run(serpapi_collect(request))

    print(json.dumps(result, indent=2))
    if result.get("source") != "fixture":
        loc_status = "verified" if result.get("location_verified") else "NOT verified"
        print(f"Location: {loc_status} | Observed: {result.get('observed_location')}")
    print(f"Report: {data_dir() / 'evidence' / result['run_id'] / 'report.html'}")
    if result["status"] not in {"found", "not_found_within_depth"}:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
