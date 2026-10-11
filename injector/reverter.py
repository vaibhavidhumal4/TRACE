"""Dead-man's switch: restore anything the ledger says is still injected.

Run on a schedule as its own job, it reverts every fault whose revert_at has passed, whether or not the process
that injected it is still alive.  `--fault-id` reverts one fault immediately (used at the end of each incident).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime

from injector import ledger
from injector.actions import Clients, revert
from injector.config import Settings


def log(severity: str, message: str, **fields: object) -> None:
    entry = {"severity": severity, "message": message, "time": datetime.now(UTC).isoformat(), **fields}
    sys.stdout.write(json.dumps(entry, default=str) + "\n")
    sys.stdout.flush()


def revert_open_faults(c: Clients, *, due_only: bool = True, fault_id: str | None = None) -> tuple[int, int]:
    """Returns (reverted, failed)."""
    reverted = failed = 0
    for fault in ledger.open_faults(c, due_only=due_only, fault_id=fault_id):
        try:
            revert(c, fault["revert_plan"])
            ledger.mark_reverted(c, fault["fault_id"])
            reverted += 1
            log("INFO", "restored", item=fault["fault_id"], kind=fault["revert_plan"]["kind"])
        except Exception as exc:
            failed += 1
            log("ERROR", f"restore failed: {type(exc).__name__}: {exc}", item=fault["fault_id"])
    return reverted, failed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fault-id", help="revert this one fault now, even if its revert_at has not passed")
    args = parser.parse_args(argv)
    c = Clients(Settings.from_env())
    reverted, failed = revert_open_faults(c, due_only=args.fault_id is None, fault_id=args.fault_id)
    log("INFO", f"maintenance pass finished: {reverted} restored, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
