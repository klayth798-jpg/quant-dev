import argparse
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

from quantdev.config import PROJECT_ROOT
from quantdev.db import database
from quantdev.models import TushareSyncRequest
from quantdev.services.market import market_data_service
from quantdev.services.tushare_sync import tushare_sync_service
from quantdev.store import store


def main() -> None:
    parser = argparse.ArgumentParser(description="Quant Dev management CLI")
    parser.add_argument(
        "command",
        choices=[
            "migrate",
            "migrate-sqlite-to-postgres",
            "bootstrap",
            "reset-real",
            "sync-market",
            "worker",
            "paper-runner",
            "live-runner",
            "verify-broker",
        ],
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--source", type=Path)
    parser.add_argument("--start-date", type=date.fromisoformat)
    parser.add_argument("--end-date", type=date.fromisoformat)
    args = parser.parse_args()

    database.migrate()
    if args.command == "migrate":
        print(json.dumps({"status": "ok", "database": database.display_name}))
    elif args.command == "migrate-sqlite-to-postgres":
        if not args.source:
            parser.error("migrate-sqlite-to-postgres 必须提供 --source")
        if database.backend != "postgresql":
            parser.error(
                "请先设置 QUANTDEV_DATABASE_BACKEND=postgresql 和 QUANTDEV_DATABASE_URL"
            )
        from quantdev.data_migration import migrate_sqlite_to_postgres

        print(
            json.dumps(
                migrate_sqlite_to_postgres(args.source),
                ensure_ascii=False,
                indent=2,
            )
        )
    elif args.command == "bootstrap":
        print(
            json.dumps(
                market_data_service.bootstrap_demo_data(force=args.force),
                ensure_ascii=False,
                indent=2,
            )
        )
    elif args.command == "reset-real":
        print(
            json.dumps(
                market_data_service.reset_for_real_data(),
                ensure_ascii=False,
                indent=2,
            )
        )
    elif args.command == "sync-market":
        if not args.start_date or not args.end_date:
            parser.error("sync-market 必须提供 --start-date 和 --end-date")
        request = TushareSyncRequest(
            start_date=args.start_date,
            end_date=args.end_date,
        )
        queued = tushare_sync_service.create_run(request)
        result = tushare_sync_service.run(queued["run_id"], request)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "worker":
        from quantdev.alerting import install_event_alerts
        from quantdev.services.tasks import task_service

        install_event_alerts()
        if args.once:
            result = task_service.run_once(timeout=1)
            print(json.dumps(result or {"status": "idle"}, ensure_ascii=False, indent=2))
        else:
            task_service.loop()
    elif args.command == "paper-runner":
        from quantdev.alerting import install_event_alerts
        from quantdev.services.strategy import strategy_runner_service

        install_event_alerts()
        if args.once:
            print(
                json.dumps(
                    strategy_runner_service.run_enabled(),
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            strategy_runner_service.loop()
    elif args.command == "live-runner":
        from quantdev.alerting import install_event_alerts
        from quantdev.services.strategy import strategy_runner_service

        install_event_alerts()
        if args.once:
            print(
                json.dumps(
                    strategy_runner_service.run_enabled(),
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            strategy_runner_service.loop()
    elif args.command == "verify-broker":
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "tests/test_broker_stress.py"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        record = store.save_verification(
            "mock_broker_stress",
            "PASSED" if completed.returncode == 0 else "FAILED",
            {
                "return_code": completed.returncode,
                "output": (completed.stdout + completed.stderr)[-4000:],
            },
        )
        print(json.dumps(record, ensure_ascii=False, indent=2))
        if completed.returncode:
            raise SystemExit(completed.returncode)


if __name__ == "__main__":
    main()
