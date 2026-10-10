"""Seven step primary-region failover runbook."""
import argparse, json, math, os, pathlib, sys, tempfile, time, uuid
from datetime import datetime, timezone
import httpx
sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
CHAOS_LOG = pathlib.Path("chaos/chaos-events.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}

def step(n, name, **kw):
    event = {"ts": time.time(), "iso": datetime.now(timezone.utc).isoformat(), "step": n, "name": name, **kw}
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as log: log.write(json.dumps(event) + "\n")
    return event

def confirm(auto: bool, msg: str) -> bool:
    return True if auto else input(f"{msg} [y/N] ").strip().lower() in {"y", "yes"}

def _probe(region, path):
    try:
        response = httpx.get(f"{URL[region]}{path}", timeout=2)
        return response.status_code == 200, f"HTTP {response.status_code}"
    except httpx.RequestError as exc:
        return False, f"{type(exc).__name__}: {exc}"

def _chaos(primary, started):
    if not CHAOS_LOG.exists(): raise RuntimeError("chaos event log missing")
    events = [json.loads(line) for line in CHAOS_LOG.read_text(encoding="utf-8").splitlines() if line.strip()]
    kills = [e for e in events if e.get("action") == "kill" and e.get("region") == primary and e.get("forced_both") is not True and e.get("other_alive") is True]
    if not kills: raise RuntimeError(f"no valid kill event for primary {primary}")
    event, ts = kills[-1], float(kills[-1]["ts"])
    if ts > started or started - ts > 600: raise RuntimeError("latest kill event is stale or from the future")
    return event

def _rollback(primary):
    ready, reason = _probe(primary, "/readyz")
    if not ready: return {"attempted": False, "manual_required": True, "reason": reason}
    path, tmp = pathlib.Path("edge/active_region"), None
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
            handle.write(primary); tmp = pathlib.Path(handle.name)
        os.replace(tmp, path)
        return {"attempted": True, "active_region": primary}
    finally:
        if tmp and tmp.exists(): tmp.unlink()

def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    if primary not in URL or target not in URL or primary == target: return {"ok": False, "error": "primary and target must be different known regions"}
    if backend not in {"fs", "minio"}: return {"ok": False, "error": "backend must be fs or minio"}
    run_id, started = uuid.uuid4().hex, time.time()
    try:
        rounds = []
        for _ in range(3): rounds.append({"primary": _probe(primary, "/readyz"), "target": _probe(target, "/healthz")})
        if not all(not r["primary"][0] for r in rounds) or not all(r["target"][0] for r in rounds): raise RuntimeError("outage probe did not confirm primary failure and target alive")
        step(1, "xac_nhan_outage", run_id=run_id, rounds=rounds, primary=primary, target=target)
        outage = _chaos(primary, started); outage_ts, operator_ts = float(outage["ts"]), time.time()
        if operator_ts <= outage_ts: raise RuntimeError("operator timestamp must be later than outage timestamp")
        step(2, "thong_bao_incident", run_id=run_id, outage_ts=outage_ts, operator_ts=operator_ts, delay_s=operator_ts-outage_ts, primary=primary, chaos_event=outage)
        if not confirm(auto, "Authorize failover? Enter y to proceed; N aborts"): return {"ok": False, "error": "operator rejected failover", "steps": 2, "run_id": run_id}
        result = fo.failover(target, backend, 60); step(3, "scale_gpu_pool", run_id=run_id, outcome=result)
        if not result.get("ok"): return {"ok": False, "error": result.get("error", "failover failed"), "steps": 3, "run_id": run_id}
        replica, restored = result.get("replica") or {}, result.get("restored_state") or {}
        count = restored.get("count", restored.get("vectors", replica.get("count"))); count = count.get("count") if isinstance(count, dict) else count
        step(4, "verify_state_replica", run_id=run_id, vector_count=count, weights=restored.get("weights"), rpo_seconds=replica.get("rpo_seconds"), docs_lost=replica.get("docs_lost"))
        cutover = result.get("cutover") or {}; step(5, "dns_cutover", run_id=run_id, ok=cutover.get("active_region") == target, active_region=cutover.get("active_region"))
        latencies, successes, served = [], 0, 0
        for _ in range(10):
            started_request = time.perf_counter()
            try:
                response = httpx.get(f"{URL[target]}/v1/infer", timeout=5); status = response.status_code
                try: body = response.json()
                except (ValueError, AttributeError): body = None
                identity = body.get("region", body.get("served_by")) if isinstance(body, dict) else None
                successes += status == 200 and body is not None
                served += status == 200 and identity == target
            except httpx.RequestError: status = None
            latencies.append((time.perf_counter()-started_request)*1000)
        p95 = sorted(latencies)[math.ceil(len(latencies)*.95)-1]; metrics = {"requests": 10, "p95_ms": round(p95, 3), "error_rate": 1-successes/10, "served_by_target": served, "ok": successes == 10 and served == 10}
        rollback = _rollback(primary) if not metrics["ok"] else None; step(6, "verify_golden_signals", run_id=run_id, **metrics, rollback=rollback)
        elapsed = round(time.time()-outage_ts, 3); measure = "py -3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl"; step(7, "post_incident", run_id=run_id, elapsed_s=elapsed, measure_command=measure)
        return {"ok": metrics["ok"], "steps": 7, "metrics": metrics, "elapsed_s": elapsed, "run_id": run_id}
    except Exception as exc: return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "run_id": run_id}

if __name__ == "__main__":
    p = argparse.ArgumentParser(); p.add_argument("--primary", default="a"); p.add_argument("--target", default="b"); p.add_argument("--backend", default="fs", choices=["fs", "minio"]); p.add_argument("--auto", action="store_true"); a = p.parse_args(); result = run(a.primary, a.target, a.backend, a.auto); print(json.dumps(result, indent=2)); sys.exit(0 if result.get("ok") else 1)
