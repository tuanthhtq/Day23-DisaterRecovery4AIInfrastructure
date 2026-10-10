import json
import time

import httpx

from dr import runbook


def setup(monkeypatch, tmp_path, *, confirm=True, golden=True):
    monkeypatch.chdir(tmp_path)
    chaos = tmp_path / "chaos/chaos-events.jsonl"
    chaos.parent.mkdir()
    chaos.write_text(json.dumps({"action": "kill", "region": "a", "ts": time.time() - 2,
                                 "other_alive": True, "forced_both": False}) + "\n")
    monkeypatch.setattr(runbook, "LOG", tmp_path / "reports/runbook.jsonl")
    monkeypatch.setattr(runbook, "CHAOS_LOG", chaos)
    monkeypatch.setattr(runbook, "confirm", lambda *args: confirm)
    monkeypatch.setattr(runbook.fo, "failover", lambda *args: {
        "ok": True, "replica": {"rpo_seconds": 1, "docs_lost": 0},
        "restored_state": {"count": 4, "weights": True},
        "cutover": {"ok": True, "active_region": "b"}})
    calls = []
    primary_readyz = 0

    def get(url, **kwargs):
        nonlocal primary_readyz
        calls.append(url)
        if url.endswith("/readyz"):
            if ":8001" in url:
                primary_readyz += 1
                status = 200 if primary_readyz > 3 else 503
            else:
                status = 200
            return httpx.Response(status, request=httpx.Request("GET", url))
        if url.endswith("/healthz"):
            return httpx.Response(200, request=httpx.Request("GET", url))
        if not golden:
            raise httpx.ConnectError("offline", request=httpx.Request("GET", url))
        return httpx.Response(200, json={"region": "b"}, request=httpx.Request("GET", url))

    monkeypatch.setattr(runbook.httpx, "get", get)
    return calls


def events(runbook):
    return [json.loads(line) for line in runbook.LOG.read_text(encoding="utf-8").splitlines()]


def test_success_seven_ordered_events_same_id_and_one_failover(monkeypatch, tmp_path):
    calls = setup(monkeypatch, tmp_path)
    failovers = []
    monkeypatch.setattr(runbook.fo, "failover", lambda *a: (failovers.append(a) or {
        "ok": True, "replica": {"rpo_seconds": 1}, "restored_state": {"count": 4},
        "cutover": {"active_region": "b"}}))

    result = runbook.run("a", "b", "fs", True)
    log = events(runbook)
    assert result["ok"] and len(failovers) == 1
    assert [e["step"] for e in log] == list(range(1, 8))
    assert len({e["run_id"] for e in log}) == 1
    assert log[0]["rounds"][-1]["target"][0] is True
    assert len([c for c in calls if c.endswith("/healthz")]) == 3
    assert len([c for c in calls if c.endswith("/v1/infer")]) == 10
    assert log[5]["p95_ms"] >= 0 and log[5]["error_rate"] == 0
    assert log[5]["served_by_target"] == 10


def test_rejected_confirmation_never_calls_failover(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path, confirm=False)
    failovers = []
    monkeypatch.setattr(runbook.fo, "failover", lambda *a: failovers.append(a))
    result = runbook.run("a", "b", "fs", False)
    assert not result["ok"] and failovers == []
    assert [e["step"] for e in events(runbook)] == [1, 2]


def test_request_errors_still_log_ten_attempts_and_guarded_rollback(monkeypatch, tmp_path):
    calls = setup(monkeypatch, tmp_path, golden=False)
    active = tmp_path / "edge/active_region"
    active.parent.mkdir()
    active.write_text("b")
    result = runbook.run("a", "b", "fs", True)
    log = events(runbook)
    assert not result["ok"]
    assert [e["step"] for e in log] == list(range(1, 8))
    assert len([c for c in calls if c.endswith("/v1/infer")]) == 10
    assert log[5]["error_rate"] == 1 and log[5]["served_by_target"] == 0
    assert log[5]["rollback"] == {"attempted": True, "active_region": "a"}
    assert active.read_text() == "a"
