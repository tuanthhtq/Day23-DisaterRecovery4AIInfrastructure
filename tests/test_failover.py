"""Test đơn vị cho 3 file sinh viên viết. Chạy được KHÔNG cần server đang chạy."""
import itertools
import json
import pathlib
import sqlite3
import sys
import time

import pytest

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402


def _db(tmp, ts_list):
    tmp.mkdir(parents=True, exist_ok=True)
    p = tmp / "v.sqlite"
    con = sqlite3.connect(p)
    con.executescript(
        "CREATE TABLE docs(doc_id TEXT PRIMARY KEY, body TEXT, embedding BLOB, ingested_at REAL);")
    con.executemany("INSERT INTO docs VALUES (?,?,?,?)",
                    [(f"d{i}", "x", b"e", t) for i, t in enumerate(ts_list)])
    con.commit()
    con.close()
    return p


def test_rpo_dem_dung_so_doc_bi_mat(tmp_path):
    """RPO không phải 'tuổi của snapshot' — là dữ liệu primary có mà bản restore không có."""
    prim = _db(tmp_path / "a", [100, 110, 120, 130])
    rest = _db(tmp_path / "b", [100, 110])
    r = snapshot.rpo(prim, rest)
    assert r["rpo_seconds"] == 20.0
    assert r["docs_lost"] == 2


def test_health_checker_can_threshold_lien_tiep(monkeypatch):
    """1 lần fail KHÔNG phải outage. Chống flapping là yêu cầu, không phải tuỳ chọn."""
    hc = pytest.importorskip("dr.health_checker")
    # True/False xen ke roi fail lien tiep mai mai -> checker phai chiu duoc ca 2 giai doan
    seq = itertools.chain([True, True, False, True, False], itertools.repeat(False))
    monkeypatch.setattr(hc, "probe", lambda r, t: (next(seq), "mock"))
    out = pathlib.Path("reports/_test-health.jsonl")
    out.unlink(missing_ok=True)
    hc.run(interval=0.01, timeout=0.01, threshold=3, duration=0.2, out=out)
    ev = [json.loads(l) for l in out.read_text().splitlines()]
    changes = [e for e in ev if e.get("event") == "state_change"]
    assert changes, "khong ghi state_change nao"
    assert changes[0]["to"] == "UNHEALTHY"
    assert changes[0]["consecutive_fails"] >= 3


def test_health_checker_cadence_recovery_and_invalid_args(monkeypatch, tmp_path):
    hc = pytest.importorskip("dr.health_checker")
    now = [0.0]
    starts = []
    seq = iter([(False, "down"), (True, "ok"), (True, "ok"), (True, "ok"),
                (True, "ok"), (True, "ok")])

    def probe(region, timeout):
        starts.append((region, now[0]))
        result = next(seq)
        now[0] += 0.3
        return result

    monkeypatch.setattr(hc.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(hc.time, "sleep", lambda delay: now.__setitem__(0, now[0] + delay))
    monkeypatch.setattr(hc, "probe", probe)
    out = tmp_path / "events.jsonl"
    hc.run(interval=1, timeout=1, threshold=1, duration=2.1, out=out)
    assert [t for r, t in starts if r == "a"] == [0.0, 1.0, 2.0]
    changes = [json.loads(line) for line in out.read_text().splitlines()]
    assert [e["to"] for e in changes] == ["UNHEALTHY", "HEALTHY"]

    for kwargs in ({"interval": 0}, {"timeout": 0}, {"threshold": 0}, {"duration": -1}):
        invalid_out = tmp_path / f"invalid-{len(kwargs)}.jsonl"
        with pytest.raises(ValueError):
            hc.run(**({"interval": 1, "timeout": 1, "threshold": 1,
                       "duration": 1, "out": invalid_out} | kwargs))
        assert not invalid_out.exists()


def test_failover_khong_cutover_khi_target_chua_ready(monkeypatch, tmp_path):
    """Đổi DNS trước khi region phụ ready = 503 từ cả hai phía.

    Test này PHẢI tự mô phỏng "target không bao giờ ready" bằng monkeypatch, không
    được dựa vào việc port 8002 tình cờ không có ai lắng nghe -- trong lab thật, stack
    bare mode gần như luôn đang chạy song song (Bước 0-4 đều cần nó sống), nên nếu test
    chỉ trông chờ ConnectError từ việc "không có server", nó sẽ liên hệ nhầm vào server
    THẬT đang chạy và cho kết quả sai tuỳ máy/tuỳ thời điểm.
    """
    fo = pytest.importorskip("dr.failover")
    monkeypatch.chdir(tmp_path)
    active = pathlib.Path("edge/active_region")
    active.parent.mkdir()
    active.write_text("a")
    pathlib.Path("state/region-b").mkdir(parents=True)
    monkeypatch.setattr(fo, "LOG", tmp_path / "reports/events.jsonl")
    monkeypatch.setattr(fo.snapshot, "get", lambda *a, **k: {
        "snapshot_at": time.time(), "latest_doc_ts": time.time(),
        "embed_model_version": "test"})
    monkeypatch.setattr(fo.snapshot, "rpo", lambda *a: {"rpo_seconds": 0, "docs_lost": 0})
    # `state_of` la mot helper NOI BO minh dat ten khi viet reference — docstring cua
    # dr/failover.py khong bat buoc ban phai co ham nay voi dung ten do. Chi patch neu
    # no ton tai, de test khong fail oan mot implementation dung nhung to chuc code khac.
    if hasattr(fo, "state_of"):
        monkeypatch.setattr(fo, "state_of", lambda r: {"region": r, "pool_state": "warm"})
    clock = [0.0]
    monkeypatch.setattr(fo.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(fo.time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
    timeouts = []

    def never_ready(*a, **k):
        timeouts.append(k["timeout"])
        raise fo.httpx.ConnectError("mocked: target khong bao gio ready")

    monkeypatch.setattr(fo.httpx, "get", never_ready)
    r = fo.failover("b", "fs", wait=1.0)
    assert not r.get("ok")
    assert active.read_text() == "a", \
        "da doi active_region du target chua ready"
    assert len(timeouts) > 1 and timeouts[0] == 1.0 and timeouts[-1] <= 0.2
    events = [json.loads(line) for line in fo.LOG.read_text().splitlines()]
    ready_events = [event for event in events if event["step"] == "4_wait_ready"]
    assert len(ready_events) == 1
    assert ready_events[0]["ready"] is False
    assert ready_events[0]["waited_s"] >= 0
    assert "error" in ready_events[0]
    assert not any(event["step"] == "5_dns_cutover" for event in events)


def test_measure_rpo_uses_last_restore_before_cutover(tmp_path):
    from tools.measure_rto import measure

    def write(name, events):
        path = tmp_path / name
        path.write_text("".join(json.dumps(event) + "\n" for event in events))
        return path

    loadgen = write("loadgen.jsonl", [{"ts": 10, "ok": False}, {"ts": 11, "ok": True,
                                                    "served_by": "b"}])
    chaos = write("chaos.jsonl", [{"ts": 10, "action": "kill", "region": "a",
                                  "other_alive": True}])
    failover = write("failover.jsonl", [
        {"ts": 10.2, "step": "2_restore_snapshot", "rpo_seconds": 99, "docs_lost": 9},
        {"ts": 10.5, "step": "2_restore_snapshot", "rpo_seconds": 2, "docs_lost": 1},
        {"ts": 10.7, "step": "5_dns_cutover"},
    ])
    result = measure(loadgen, chaos, write("health.jsonl", []), failover, 300)
    assert result["rpo_at_restore_s"] == 2
    assert result["docs_lost"] == 1


def test_failover_success_restores_readiness_then_cutover(monkeypatch, tmp_path):
    fo = pytest.importorskip("dr.failover")
    monkeypatch.chdir(tmp_path)
    pathlib.Path("state/region-b").mkdir(parents=True)
    pathlib.Path("edge").mkdir()
    monkeypatch.setattr(fo, "LOG", tmp_path / "reports/events.jsonl")
    states = iter([{"region": "b", "pool_state": "warm"},
                   {"region": "b", "pool_state": "full", "vectors": 3}])
    monkeypatch.setattr(fo, "state_of", lambda region: next(states))
    monkeypatch.setattr(fo.snapshot, "get", lambda *a: {"embed_model_version": "v1"})
    monkeypatch.setattr(fo.snapshot, "rpo", lambda *a: {"rpo_seconds": 4, "docs_lost": 2})
    responses = iter([fo.httpx.Response(200, json={"ready": True})])
    monkeypatch.setattr(fo.httpx, "get", lambda *a, **k: next(responses))

    result = fo.failover("b", "fs", wait=1)

    assert result["ok"]
    assert result["replica"] == {"embed_model_version": "v1", "rpo_seconds": 4,
                                 "docs_lost": 2}
    assert result["initial_state"] == {"region": "b", "pool_state": "warm"}
    assert result["restored_state"] == {"region": "b", "pool_state": "full", "vectors": 3}
    assert result["cutover"]["active_region"] == "b"
    events = [json.loads(line) for line in fo.LOG.read_text().splitlines()]
    assert [event["step"] for event in events] == [
        "1_verify_target", "2_restore_snapshot", "3_scale_pool", "4_wait_ready",
        "5_dns_cutover",
    ]
    assert all("ts" in event and "iso" in event for event in events)
    assert events[3]["waited_s"] >= 0
    assert (tmp_path / "edge/active_region").read_text() == "b"


def test_failover_restores_active_region_when_cutover_event_fails(monkeypatch, tmp_path):
    fo = pytest.importorskip("dr.failover")
    monkeypatch.chdir(tmp_path)
    pathlib.Path("state/region-b").mkdir(parents=True)
    active = pathlib.Path("edge/active_region")
    active.parent.mkdir()
    active.write_text("a")
    monkeypatch.setattr(fo, "LOG", tmp_path / "reports/events.jsonl")
    states = iter([{"region": "b", "pool_state": "warm"},
                   {"region": "b", "pool_state": "full"}])
    monkeypatch.setattr(fo, "state_of", lambda region: next(states))
    monkeypatch.setattr(fo.snapshot, "get", lambda *a: {"embed_model_version": "v1"})
    monkeypatch.setattr(fo.snapshot, "rpo", lambda *a: {"rpo_seconds": 0, "docs_lost": 0})
    monkeypatch.setattr(fo.httpx, "get", lambda *a, **k: fo.httpx.Response(200, json={"ready": True}))
    original_emit = fo.emit

    def fail_cutover(**kwargs):
        if kwargs.get("step") == "5_dns_cutover":
            raise OSError("event log unavailable")
        return original_emit(**kwargs)

    monkeypatch.setattr(fo, "emit", fail_cutover)
    result = fo.failover("b", "fs", wait=1)

    assert result["ok"] is False
    assert active.read_text() == "a"
    assert not any(event["step"] == "5_dns_cutover"
                   for event in (json.loads(line) for line in fo.LOG.read_text().splitlines()))
