# Runbook - primary region down

Run from the repository root in Windows PowerShell. Owner: on-call operator. Every completed step is recorded in `reports/runbook-run.jsonl`.

| # | Action | Copy-paste command / verification | Done when | Owner |
|---|---|---|---|---|
| 1 | Confirm outage | `py -3 dr/runbook.py --primary a --target b --backend fs` | Three consecutive `/readyz` failures in A and three `/healthz` alive responses in B | On-call |
| 2 | Open incident | The runbook reads the latest `action=kill` for A from `chaos/chaos-events.jsonl`; enter `y` to authorize failover (`N` aborts) | `outage_ts`, later `operator_ts`, and `delay_s` are logged | On-call |
| 3 | Restore and scale | Runbook calls `dr.failover.failover("b", "fs", 60)` once | Step 3 outcome is `ok=true` | DR operator |
| 4 | Verify replica | Inspect step 4 in `reports/runbook-run.jsonl` | Vector count, weights, RPO, and docs lost are present | DR operator |
| 5 | Cut over DNS/LB | `Get-Content edge/active_region` | Output is `b`; step 5 has `ok=true` | DR operator |
| 6 | Verify golden signals | Runbook sends 10 direct requests to `http://127.0.0.1:8002/v1/infer` | All 10 are HTTP 200 and served by B; p95 and error rate logged | On-call |
| 7 | Measure RTO | `py -3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl` | Step 7 has elapsed seconds and the exact command; measurement returns an RTO verdict | Incident lead |

Rollback: verify A first: `Invoke-WebRequest http://127.0.0.1:8001/readyz`. If HTTP 200, atomically switch traffic with PowerShell:

```powershell
$tmp = Join-Path edge ('.active_region.' + [guid]::NewGuid().ToString() + '.tmp')
Set-Content -LiteralPath $tmp -Value 'a' -NoNewline
Move-Item -LiteralPath $tmp -Destination edge/active_region -Force
```

If A is not ready, do not switch; manual rollback is required. The incident lead authorizes rollback. Do not flap traffic between regions.
