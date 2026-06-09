import argparse
import json
from datetime import date

from quantdev.db import database
from quantdev.models import TushareSyncRequest
from quantdev.services.market import market_data_service
from quantdev.services.tushare_sync import tushare_sync_service


def main() -> None:
    parser = argparse.ArgumentParser(description="Quant Dev management CLI")
    parser.add_argument(
        "command",
        choices=["migrate", "bootstrap", "reset-real", "sync-market"],
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--start-date", type=date.fromisoformat)
    parser.add_argument("--end-date", type=date.fromisoformat)
    args = parser.parse_args()

    database.migrate()
    if args.command == "migrate":
        print(json.dumps({"status": "ok", "database": str(database.path)}))
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


if __name__ == "__main__":
    main()
