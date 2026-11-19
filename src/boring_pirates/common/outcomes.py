"""Pirate binding of Common's failure presentation policy."""
from __future__ import annotations
from typing import Any
import logging
from boring_worker_kit.outcomes import failure_data as worker_failure_data


def failure_data(error: Exception, *, log_error: bool = True) -> dict[str, Any]:
    return worker_failure_data(error, component="pirate", fallback_code="pirate_failed",
                               logger=logging.getLogger("boring_pirates") if log_error else None)
