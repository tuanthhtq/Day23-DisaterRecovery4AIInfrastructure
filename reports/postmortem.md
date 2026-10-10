# Postmortem - DR Drill Lab 23

Blameless review of the Region A outage and failover to Region B.

## 0. Baseline before DR

Region B began cold: `pool_state=warm`, `weights=false`, and `count=0` in `reports/failover-events.jsonl:1`. The snapshot restore supplied the vector data and model version with `rpo_seconds=0.0` and `docs_lost=0` in `reports/failover-events.jsonl:2`.

## 1. Timeline

| ISO time | Event | Evidence |
|---|---|---|
| 2026-10-10T15:32:21Z | Region A outage starts | `chaos/chaos-events.jsonl:5` |
| 2026-10-10T15:32:21.510700Z | First user request fails | `reports/drill-2-withdr.jsonl:26` |
| 2026-10-10T15:32:35.957270Z | Health checker marks A unhealthy | `reports/health-events.jsonl:2` |
| 2026-10-10T15:33:24.381556Z | Snapshot restore completes | `reports/failover-events.jsonl:2` |
| 2026-10-10T15:33:30.642462Z | Operator confirms DNS cutover to B | `reports/failover-events.jsonl:5` |
| 2026-10-10T15:33:32.154723Z | First successful request from B | `reports/drill-2-withdr.jsonl:61` |

## 2. RTO/RPO versus target

- RTO target: 300s; measured RTO: `71.0s`; gap: `229.0s` headroom.
- RPO target: 300s; measured RPO: `0.0s`; gap: `300.0s`; `0 docs` lost.
- Largest step: operator/runbook invocation delay, `48.4s` of the `71.0s` total. Snapshot restore took about `0.017s`; pool readiness took `6.227s`.

## 3. Root cause (5 whys)

1. Users saw errors because Region A was stopped and traffic remained there until failover.
2. Traffic remained there because detection requires three failed checks at a five-second interval.
3. Recovery waited `48.4s` for the operator to invoke and authorize the runbook after detection.
4. The snapshot restore itself took about `0.017s`; Region B then needed `6.227s` to become ready.
5. The system had no prompt alert-to-runbook handoff, so human response dominated this drill's RTO.

## 4. Action items

| # | Action item | Owner | Deadline | Expected RTO/RPO effect |
|---|---|---|---|---|
| 1 | Alert and invoke the approved runbook promptly after detection; track operator response time. | On-call lead | 2026-10-24 | Reduce the 48.4s operator delay. |
| 2 | Pre-warm a minimum GPU pool in Region B. | Platform team | 2026-10-31 | Reduce the 6.2s readiness wait. |

## 5. Required questions

1. With `interval=5s` and `threshold=3`, the detection floor is `15s`, or `21.1%` of the measured `71.0s` RTO. The recorded detection was `14.8s`, within timestamp sampling/rounding of that floor.
2. At `interval=1s` with the same threshold, detection floor drops from 15s to 3s, so RTO can fall by up to `12s`. The cost is more health-check traffic and false positives from transient faults (flapping), requiring debounce or alert confirmation.
3. `docs_lost` is the count of customer documents absent after restore. If the outage lasts six hours and Region A data is permanently lost, `docs_lost` reports the number of customer documents that cannot be recovered; it is a concrete data-loss count, not elapsed outage time.
