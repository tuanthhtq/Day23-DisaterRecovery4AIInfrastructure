"""BƯỚC 3b — SINH VIÊN VIẾT. Cutover sang region phụ.

5 bước, THỨ TỰ QUAN TRỌNG (§2 Kiến Trúc Tham Chiếu: DNS/LB, compute, state là 3 lớp riêng):
  1_verify_target    — /v1/state của region phụ: weights? vector count? pool_state?
  2_restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
                       Log BẮT BUỘC: rpo_seconds, docs_lost, embed_model_version.
                       (§3: "backup index nhưng quên backup embedding model version
                        -> index không tương thích khi restore")
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state (warm -> full)
  4_wait_ready       — POLL /readyz tới khi 200. Region phụ có WARMUP_SECONDS —
                       đây là GPU pool warm-up của §4, nó nằm trong RTO của bạn.
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu bạn đổi edge/active_region TRƯỚC bước 4, user sẽ nhận 503 từ CẢ HAI region
và RTO của bạn dài hơn, không ngắn hơn. Nếu bước 4 timeout -> ABORT, KHÔNG cutover.

Mỗi bước ghi 1 dòng vào reports/failover-events.jsonl với ts + step.
Không có dòng 5_dns_cutover = tools/measure_rto.py không tìm được t_cutover = mất điểm.

Chạy:  python dr/failover.py --target b --backend fs
"""
import argparse
import json
import math
import os
import pathlib
import sys
import tempfile
import time
from datetime import datetime, timezone

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")


def emit(**kw):
    """Append one timestamped event to the failover log and stdout."""
    event = {"ts": time.time(), "iso": datetime.now(timezone.utc).isoformat(), **kw}
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as log:
        log.write(json.dumps(event) + "\n")
    print(json.dumps(event))
    return event


def state_of(region: str) -> dict:
    response = httpx.get(f"{URL[region]}/v1/state", timeout=2)
    response.raise_for_status()
    return response.json()


def failover(target: str, backend: str, wait: float) -> dict:
    """Restore and ready the target before changing the active region."""
    if target not in URL or backend not in {"fs", "minio"} or not math.isfinite(wait) or wait <= 0:
        raise ValueError("target must be a|b, backend fs|minio, wait > 0")
    events = []
    try:
        replica = state_of(target)
        events.append(emit(step="1_verify_target", target=target, state=replica))

        restored = snapshot.get(target, backend)
        primary = "b" if target == "a" else "a"
        rpo = snapshot.rpo(pathlib.Path(f"state/region-{primary}/vectors.sqlite"),
                          pathlib.Path(f"state/region-{target}/vectors.sqlite"))
        restored.update(rpo)
        events.append(emit(step="2_restore_snapshot", target=target, **{
            key: restored.get(key) for key in ("rpo_seconds", "docs_lost", "embed_model_version")
        }))

        pathlib.Path(f"state/region-{target}/pool_state").write_text("full\n", encoding="utf-8")
        events.append(emit(step="3_scale_pool", target=target, pool_state="full"))

        started = time.monotonic()
        deadline = started + wait
        last_error = "no readiness response"
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"target {target} not ready: {last_error}")
                try:
                    response = httpx.get(f"{URL[target]}/readyz", timeout=min(2, remaining))
                    if response.status_code == 200:
                        ready = response.json()
                        break
                    last_error = f"HTTP {response.status_code}"
                except httpx.RequestError as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    time.sleep(min(0.2, remaining))
        except Exception as exc:
            waited_s = round(time.monotonic() - started, 3)
            events.append(emit(step="4_wait_ready", target=target, ready=False,
                               waited_s=waited_s, error=f"{type(exc).__name__}: {exc}"))
            raise
        waited_s = round(time.monotonic() - started, 3)
        restored_state = state_of(target)
        events.append(emit(step="4_wait_ready", target=target, ready=ready,
                           waited_s=waited_s))

        active_path = pathlib.Path("edge/active_region")
        active_path.parent.mkdir(parents=True, exist_ok=True)
        previous_region = (active_path.read_text(encoding="utf-8")
                           if active_path.exists() else None)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=active_path.parent,
                                             prefix=f".{active_path.name}.", delete=False) as temp:
                temp.write(target)
                temp_path = pathlib.Path(temp.name)
            os.replace(temp_path, active_path)
        finally:
            if temp_path and temp_path.exists():
                temp_path.unlink()
        try:
            cutover = emit(step="5_dns_cutover", target=target, active_region=target)
        except Exception:
            if previous_region is None:
                active_path.unlink(missing_ok=True)
                raise
            rollback_path = None
            try:
                with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=active_path.parent,
                                                 prefix=f".{active_path.name}.", delete=False) as temp:
                    temp.write(previous_region)
                    rollback_path = pathlib.Path(temp.name)
                os.replace(rollback_path, active_path)
            finally:
                if rollback_path and rollback_path.exists():
                    rollback_path.unlink()
            raise
        events.append(cutover)
        return {"ok": True, "target": target, "replica": restored,
                "initial_state": replica, "restored_state": restored_state,
                "ready": ready, "cutover": cutover, "steps": events}
    except Exception as exc:
        return {"ok": False, "target": target, "error": f"{type(exc).__name__}: {exc}",
                "steps": events}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    result = failover(a.target, a.backend, a.wait)
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["ok"] else 1)
