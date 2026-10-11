# TRACE

**Triage and Root-cause Analysis with Coordinated Evaluation agents**
*Multi-Agent Incident Investigation & Root-Cause Analysis System*

TRACE watches a real, scheduled data pipeline, deliberately breaks it in controlled ways, and uses a team of AI agents
(Triage, Root-Cause, Remediation, Postmortem, Evaluation) to diagnose each failure. Because every fault is injected on
purpose, the true cause is known, so diagnostic accuracy is a **measured number**, not a claim.

> **Status:** Phase 0 (cloud workspace and guardrails). No application code yet.

## How it works

```
Cloud Scheduler --(4h)--> [Cloud Run Job: target ETL] --> BigQuery (hourly air-quality data, run ledger)
                                   ^        |
        [Fault injector] --breaks--+        +--> Cloud Logging
               |                                      |
               +--> ground truth (restricted)         v
                                          log sink / watchdog --> Pub/Sub --> [agents on Cloud Run]
                                                                   Triage -> Root-Cause -> Remediation -> Postmortem
                                          [Evaluation] compares verdicts with ground truth --> accuracy
```

- **Target system:** an hourly air-quality ETL for 36 Indian states and union territories, loading into BigQuery.
- **Fault injection:** a catalog of reversible, cloud-native faults (timeouts, revoked permissions, upstream rate limits,
  schema drift, partial data, unreachable endpoints), each with a dead-man's-switch revert.
- **Agents:** orchestrated as a LangGraph graph. Root-Cause must answer from a closed nine-category taxonomy, so accuracy is
  a deterministic equality check.
- **Propose-only remediation:** agents suggest fixes and never execute them. This is enforced by IAM, not by prompt.
- **Evaluation integrity:** ground truth lives in a separate dataset the agent identity cannot read.
- **Memory ablation:** accuracy is reported on held-out incidents with retrieval of similar past incidents on and off.

## Design constraints

- Runs entirely on Google Cloud's free tier (Cloud Run, BigQuery, Pub/Sub, Cloud Scheduler, Cloud Logging).
- BigQuery is the only database.
- No secrets in git and no service-account keys: Secret Manager and Workload Identity Federation only.

## Roadmap

| Phase | Scope |
|---|---|
| 0 | Cloud workspace and guardrails |
| 1 | Target ETL live on a schedule |
| 2 | Detection and incident ledger |
| 3 | Fault injector and ground truth |
| 4 | Agents and orchestrator |
| 5 | Evaluation harness and campaigns |
| 6 | Dashboard and read-only MCP access |
| 7 | Hardening and release |

## Data attribution

Air-quality data comes from the [Open-Meteo Air Quality API](https://open-meteo.com/), which is based on CAMS
(Copernicus Atmosphere Monitoring Service) data.

## License

No license file is provided. All rights reserved.
