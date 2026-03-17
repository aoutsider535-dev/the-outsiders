#!/usr/bin/env python3
"""
Run the Copy Trader Bot.

Usage:
    python -m src.copy_trader.run [--balance 500] [--live]
"""
import argparse
import sys
from . import config
from .monitor import LeaderMonitor


def main():
    parser = argparse.ArgumentParser(description="Polymarket Copy Trader")
    parser.add_argument("--balance", type=float, default=500.0,
                        help="Starting paper balance (default: $500)")
    parser.add_argument("--live", action="store_true",
                        help="Run in live mode (default: paper)")
    parser.add_argument("--poll", type=int, default=None,
                        help="Override poll interval in seconds")
    args = parser.parse_args()

    if args.live:
        config.PAPER_MODE = False
        confirm = input("⚠️  LIVE MODE — real money will be used. Type 'yes' to confirm: ")
        if confirm.strip().lower() != 'yes':
            print("Cancelled.")
            sys.exit(0)

    if args.poll:
        config.POLL_INTERVAL_SEC = args.poll

    monitor = LeaderMonitor()
    monitor.run(paper_balance=args.balance)


if __name__ == "__main__":
    main()
