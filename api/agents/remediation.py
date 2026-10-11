"""Remediation: the standard fix for the diagnosed category, recorded as a proposal. Rules only; no LLM.

Nothing here executes anything. The service's identity has no write access to the target system, so it could not.
"""

from __future__ import annotations

from api.schemas import Category, Proposal

STANDARD_FIXES: dict[Category, list[Proposal]] = {
    Category.AUTHORIZATION_FAILURE: [
        Proposal(
            action="Restore the pipeline service account's write role (BigQuery Data Editor) on the target dataset, "
            "after confirming from the audit log who removed it and why.",
            risk="Low: restores a previously held, dataset-scoped permission.",
            blast_radius="The target dataset only.",
            rollback="Remove the role binding again.",
            confidence=0.85,
        ),
        Proposal(
            action="Re-run the job once access is restored; the load is idempotent on (state, ts).",
            risk="Low.",
            blast_radius="One job execution.",
            rollback="Not needed: a re-run inserts only missing rows.",
            confidence=0.8,
        ),
    ],
    Category.WAREHOUSE_SCHEMA_MISMATCH: [
        Proposal(
            action="Compare the target table's columns with the pipeline's staging schema. If a column was added to "
            "the table and nothing depends on it, drop it; otherwise change the load to name its columns.",
            risk="Medium: dropping a column discards any data written to it.",
            blast_radius="The target table and anything that reads the changed column.",
            rollback="Re-add the column (data in it is lost) or revert the pipeline change.",
            confidence=0.8,
        ),
        Proposal(
            action="Re-run the job after the schemas agree; the load is idempotent on (state, ts).",
            risk="Low.",
            blast_radius="One job execution.",
            rollback="Not needed.",
            confidence=0.8,
        ),
    ],
    Category.RESOURCE_LIMIT_EXCEEDED: [
        Proposal(
            action="Raise the job's task timeout back to a value above the normal run time (a healthy run needs "
            "well under a minute of work; the previous setting was 900 s), after confirming who lowered it.",
            risk="Low: a longer timeout only matters for a run that hangs.",
            blast_radius="The one Cloud Run job.",
            rollback="Set the timeout back to its current value.",
            confidence=0.85,
        ),
        Proposal(
            action="Re-run the job; a killed run leaves no partial rows in the main table.",
            risk="Low.",
            blast_radius="One job execution.",
            rollback="Not needed.",
            confidence=0.8,
        ),
    ],
    Category.DNS_OR_ENDPOINT_UNREACHABLE: [
        Proposal(
            action="Check the upstream URL the job is configured with. If it was changed to a host that does not "
            "resolve, restore the previous value (or remove the override so the default applies).",
            risk="Low: restores a known-good endpoint.",
            blast_radius="The one Cloud Run job.",
            rollback="Set the variable back to its current value.",
            confidence=0.85,
        ),
        Proposal(
            action="If the URL is correct, check the upstream service's status and retry later.",
            risk="Low.",
            blast_radius="None.",
            rollback="Not needed.",
            confidence=0.5,
        ),
    ],
}


def propose(category: Category) -> list[Proposal]:
    return STANDARD_FIXES[category]


def run(incident_id: str, deps) -> dict:
    stages = {e["stage"]: e["payload"] for e in deps.store.events(incident_id)}
    category = Category(stages["root_cause"]["root_cause_category"])
    proposals = [p.model_dump(mode="json") for p in propose(category)]
    deps.store.write_verdict(incident_id, "remediation", category=category.value, proposals=proposals)
    deps.store.append_event(
        incident_id,
        "remediation",
        "remediation",
        {"category": category.value, "proposals": proposals, "executed": False},
    )
    return {"llm_calls": 0, "proposals": proposals}
