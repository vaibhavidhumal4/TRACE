"""Phase 3 check: inject each fault once on the DEV target, run the job, record what it looks like, restore.

Prints one JSON object per fault. Refuses to run against prod.
    GCP_PROJECT=... TRACE_ENV=dev OPS_IMPERSONATE=ops-automation@... python -m injector.signatures
"""

from __future__ import annotations

import json
import sys

from injector.actions import Clients
from injector.catalog import CATALOG
from injector.config import Settings
from injector.runner import end_fault, execute_and_wait, run_rows, snapshot, start_fault


def main(argv: list[str] | None = None) -> int:
    only = set(argv if argv is not None else sys.argv[1:])
    settings = Settings.from_env()
    if settings.env != "dev":
        print("signatures run on the dev target only (TRACE_ENV=dev)", file=sys.stderr)
        return 2
    c = Clients(settings)
    before = snapshot(c)
    print(json.dumps({"snapshot_before": before}), flush=True)
    all_restored = True
    for fault in CATALOG.values():
        if only and fault.fault_type not in only:
            continue
        params = dict(fault.variants[0])
        fault_id = start_fault(c, fault.fault_type, params, campaign_id="signature-check", seed=None)
        try:
            result = execute_and_wait(c)
        finally:
            end_fault(c, fault_id)
        rows = run_rows(c, result["execution"])
        restored = snapshot(c) == before
        all_restored = all_restored and restored
        print(
            json.dumps(
                {
                    "fault_type": fault.fault_type,
                    "params": params,
                    **result,
                    "pipeline_runs": rows,
                    "restored": restored,
                },
                default=str,
            ),
            flush=True,
        )
    return 0 if all_restored else 1


if __name__ == "__main__":
    sys.exit(main())
