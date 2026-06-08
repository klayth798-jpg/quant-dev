import argparse
import json

from quantdev.db import database
from quantdev.services.market import market_data_service


def main() -> None:
    parser = argparse.ArgumentParser(description="Quant Dev management CLI")
    parser.add_argument("command", choices=["migrate", "bootstrap"])
    parser.add_argument("--force", action="store_true")
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


if __name__ == "__main__":
    main()
