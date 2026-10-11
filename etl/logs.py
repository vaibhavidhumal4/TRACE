"""Structured JSON logs on stdout, in the shape Cloud Logging parses."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime

_context: dict[str, object] = {}


def bind(**fields: object) -> None:
    """Fields added to every later log line (run_id, execution_name)."""
    _context.update(fields)


def log(
    severity: str,
    message: str,
    *,
    phase: str | None = None,
    state: str | None = None,
    attempt: int | None = None,
    error_class: str | None = None,
    **extra: object,
) -> None:
    entry: dict[str, object] = {
        "severity": severity,
        "message": message,
        "time": datetime.now(UTC).isoformat(),
        **_context,
        "phase": phase,
        "state": state,
        "attempt": attempt,
        "error_class": error_class,
        **extra,
    }
    sys.stdout.write(json.dumps(entry, default=str) + "\n")
    sys.stdout.flush()
