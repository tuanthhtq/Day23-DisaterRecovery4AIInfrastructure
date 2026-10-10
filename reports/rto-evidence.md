# RTO/RPO Evidence - Lab 23

Every measured value below is traceable to the committed drill logs.

## 1. Drill 1 - no DR baseline

| Metric | Value | Evidence |
|---|---:|---|
| Outage start | `2026-10-09T04:35:17Z` | `chaos/chaos-events.jsonl:1` |
| First failed request | `+0.2s` | `reports/drill-1-nodr.jsonl:50` |
| Recovery | None | `reports/measure-drill-1.json` |
| RTO | `NO_RECOVERY` | `reports/measure-drill-1.json` |

## 2. Drill 2 - DR enabled

| Milestone | Seconds from outage | Evidence |
|---|---:|---|
| Outage start | `0s` | `chaos/chaos-events.jsonl:5` |
| First user-visible failure | `0.4s` | `reports/drill-2-withdr.jsonl:26` |
| Health check detects Region A unhealthy | `14.8s` | `reports/health-events.jsonl:2` |
| Snapshot restore complete | `63.2s` | `reports/failover-events.jsonl:2` |
| Region B ready | `69.5s` | `reports/failover-events.jsonl:4` |
| DNS cutover | `69.5s` | `reports/failover-events.jsonl:5` |
| First successful request from Region B | `71.0s` | `reports/drill-2-withdr.jsonl:61` |

| Metric | Measured | Target | Verdict |
|---|---:|---:|---|
| RTO - inference API | `71.0s` | `300s` (5 minutes) | PASS |
| RPO - vector DB | `0.0s / 0 docs` | `300s` (5 minutes) | PASS |

## 3. RTO component breakdown

| Component | Seconds | Source | Reduction |
|---|---:|---|---|
| Health-check detection | `14.8s` | `interval_s=5`, `threshold=3`, `reports/health-events.jsonl:2` | Lower interval or threshold, with flapping risk |
| Operator/runbook invocation delay | `48.4s` | Health detection at `reports/health-events.jsonl:2` (`15:32:35.957`); runbook's first recorded state at `reports/failover-events.jsonl:1` (`15:33:24.364`) | Alert the on-call operator promptly; automate runbook invocation |
| Snapshot restore | `0.017s` | `reports/failover-events.jsonl:1` initial state to `:2` restore completion | Already negligible in this drill |
| GPU pool warm-up | `6.3s` | `waited_s=6.227`, `reports/failover-events.jsonl:4` | Keep a warm standby pool |
| DNS/LB TTL | `1.5s` | cutover `reports/failover-events.jsonl:5` to first B success `reports/drill-2-withdr.jsonl:61` | Reduce TTL or use health-routed LB |
| **Total RTO** | **`71.0s`** | `reports/measure-drill-2.json` | **Within target** |

The observed split is `14.8s` detection + `48.4s` operator/runbook invocation delay + `0.017s` snapshot restore + `6.3s` pool readiness + `1.5s` cutover to first successful request = `71.0s` (rounded). The 48.4 seconds is human response/runbook invocation time, not restore duration.
