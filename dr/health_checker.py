"""BƯỚC 3a — SINH VIÊN VIẾT. Health checker cho 2 region.

Yêu cầu (đọc §4 "Kiến Trúc Health-Check-Based Failover" + §2 "DNS Failover"):
  1. Poll /readyz của CẢ HAI region mỗi `interval` giây (mặc định 5s).
     Dùng /readyz, KHÔNG dùng /healthz. /healthz chỉ nói "process còn sống" —
     region có process sống nhưng vector DB rỗng thì vẫn không serve được.
  2. Chỉ đổi trạng thái sau `threshold` lần fail LIÊN TIẾP (mặc định 3).
     Một lần fail không phải outage. Đây là chống flapping (§4 Anti-Patterns).
  3. Ghi 1 dòng JSONL MỖI LẦN ĐỔI TRẠNG THÁI (không ghi mỗi lần poll — log sẽ ngập).
     Dòng bắt buộc có: ts, region, to (HEALTHY|UNHEALTHY), reason,
     interval_s, threshold. Thiếu interval_s/threshold thì tools/measure_rto.py
     không tính được detect floor -> mất điểm.

Chạy:  python dr/health_checker.py --interval 5 --threshold 3 --duration 300 \
              --out reports/health-events.jsonl

CÂU HỎI PHẢI TRẢ LỜI TRƯỚC KHI VIẾT (ghi câu trả lời vào reports/postmortem.md):
  interval=5s, threshold=3 -> sớm nhất bạn có thể phát hiện outage là bao nhiêu giây?
  Con số đó nằm TRONG RTO của bạn. Muốn RTO 5 phút thì được phép chọn interval bao nhiêu?
"""
import argparse
import json
import pathlib
import time

import httpx

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def probe(region: str, timeout: float) -> tuple[bool, str]:
    """Trả về trạng thái readiness và lý do, luôn giới hạn thời gian request."""
    try:
        response = httpx.get(f"{URL[region]}/readyz", timeout=timeout)
        if response.status_code == 200:
            return True, "HTTP 200"
        return False, f"HTTP {response.status_code}"
    except httpx.RequestError as exc:
        return False, f"{type(exc).__name__}: {exc}"


def run(interval: float, timeout: float, threshold: int, duration: float, out: pathlib.Path):
    """Poll cả hai region và chỉ ghi JSONL khi trạng thái đổi."""
    if not interval > 0 or not timeout > 0 or threshold < 1 or duration < 0:
        raise ValueError("interval/timeout must be > 0, threshold >= 1, duration >= 0")
    failures = dict.fromkeys(URL, 0)
    unhealthy = dict.fromkeys(URL, False)
    deadline = time.monotonic() + duration
    next_poll = time.monotonic()
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as log:
        while time.monotonic() < deadline:
            for region in URL:
                ready, reason = probe(region, timeout)
                if ready:
                    failures[region] = 0
                    if unhealthy[region]:
                        unhealthy[region] = False
                        event = {
                            "event": "state_change", "ts": time.time(),
                            "region": region, "to": "HEALTHY", "reason": reason,
                            "interval_s": interval, "threshold": threshold,
                            "consecutive_fails": 0,
                        }
                        log.write(json.dumps(event) + "\n")
                        log.flush()
                else:
                    failures[region] += 1
                    if failures[region] >= threshold and not unhealthy[region]:
                        unhealthy[region] = True
                        event = {
                            "event": "state_change", "ts": time.time(),
                            "region": region, "to": "UNHEALTHY", "reason": reason,
                            "interval_s": interval, "threshold": threshold,
                            "consecutive_fails": failures[region],
                        }
                        log.write(json.dumps(event) + "\n")
                        log.flush()
            next_poll += interval
            delay = min(next_poll, deadline) - time.monotonic()
            if delay > 0:
                time.sleep(delay)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--timeout", type=float, default=2.0)
    p.add_argument("--threshold", type=int, default=3)
    p.add_argument("--duration", type=float, default=300)
    p.add_argument("--out", default="reports/health-events.jsonl")
    a = p.parse_args()
    run(a.interval, a.timeout, a.threshold, a.duration, pathlib.Path(a.out))
