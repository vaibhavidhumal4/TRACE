"""Build the fixed, ordered injection list for a campaign from a seed."""

from __future__ import annotations

import random

from injector.catalog import CATALOG

PER_FAULT = 3


def build_plan(seed: int, per_fault: int = PER_FAULT) -> list[dict]:
    """`per_fault` injections of every fault, each with its own parameter variant, in a seeded random order."""
    rng = random.Random(seed)
    items = []
    for fault in CATALOG.values():
        if per_fault > len(fault.variants):
            raise ValueError(f"{fault.fault_type} has only {len(fault.variants)} variants")
        for params in rng.sample(list(fault.variants), per_fault):
            items.append(
                {"fault_type": fault.fault_type, "expected_category": fault.expected_category, "params": dict(params)}
            )
    rng.shuffle(items)
    return items
