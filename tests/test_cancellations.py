"""End-to-end tests for cancelling approved screenings before they start."""

from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.service import Store

# fixed far-future / far-past slots so the "not started yet" rule does not
# depend on the wall clock
FUTURE = "2030-06-01T20:00:00Z"
FUTURE_2 = "2030-06-02T20:00:00Z"
PAST = "2020-06-01T20:00:00Z"
WINDOW = {
    "valid_from": "2020-01-01T00:00:00Z",
    "valid_to": "2031-01-01T00:00:00Z",
}


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def make_license(client, **overrides):
    body = {
        "film_id": "film-1",
        "venue_id": "hall-a",
        "valid_from": WINDOW["valid_from"],
        "valid_to": WINDOW["valid_to"],
        "max_sessions": 2,
    }
    body.update(overrides)
    resp = client.post("/licenses", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def verify(client, key, start=FUTURE, film="film-1", venue="hall-a"):
    return client.post(
        "/verifications",
        json={
            "film_id": film,
            "venue_id": venue,
            "start_time": start,
            "idempotency_key": key,
        },
    )


def cancel(client, key, reason="放映设备故障，场次取消"):
    return client.post(f"/verifications/{key}/cancel", json={"reason": reason})


# ----------------------------------------------------------------------
# happy path & quota release
# ----------------------------------------------------------------------
def test_cancel_approved_returns_record_and_releases_license_quota(client):
    lic = make_license(client, max_sessions=1)
    approved = verify(client, "c-1").json()
    assert approved["decision"] == "approved"
    assert approved["cancelled"] is False
    assert approved["cancellation"] is None

    resp = cancel(client, "c-1")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["id"] >= 1
    assert data["idempotency_key"] == "c-1"
    assert data["verification_id"] == approved["id"]
    assert data["license_id"] == lic["id"]
    assert data["pool_id"] is None
    assert data["reason"] == "放映设备故障，场次取消"
    assert data["cancelled_at"]
    assert data["replay"] is False

    # live usage of the license rolls back once
    lic_after = client.get(f"/licenses/{lic['id']}").json()
    assert lic_after["used_sessions"] == 0
    assert lic_after["remaining_sessions"] == 1


def test_cancel_releases_bound_pool_quota(client):
    pool = client.post(
        "/pools", json={"film_id": "film-1", "max_sessions": 1}
    ).json()
    lic = make_license(client, max_sessions=5, pool_id=pool["id"])
    approved = verify(client, "cp-1").json()
    assert approved["pool_id"] == pool["id"]
    assert approved["pool_remaining_quota"] == 0

    data = cancel(client, "cp-1").json()
    assert data["pool_id"] == pool["id"]

    pool_after = client.get(f"/pools/{pool['id']}").json()
    assert pool_after["used_sessions"] == 0
    assert pool_after["remaining_sessions"] == 1
    lic_after = client.get(f"/licenses/{lic['id']}").json()
    assert lic_after["used_sessions"] == 0
    assert lic_after["pool"]["used_sessions"] == 0


def test_released_quota_is_reusable_by_new_verification(client):
    make_license(client, max_sessions=1)
    assert verify(client, "c-1").json()["decision"] == "approved"
    exhausted = verify(client, "c-2").json()
    assert exhausted["reject_reason"] == "QUOTA_EXHAUSTED"

    cancel(client, "c-1")

    # the freed unit can be consumed by a later verification
    again = verify(client, "c-3").json()
    assert again["decision"] == "approved"
    assert again["remaining_quota"] == 0
    # and it is really gone afterwards
    last = verify(client, "c-4").json()
    assert last["reject_reason"] == "QUOTA_EXHAUSTED"


def test_released_pool_quota_is_reusable_across_venues(client):
    pool = client.post(
        "/pools", json={"film_id": "film-1", "max_sessions": 1}
    ).json()
    make_license(client, venue_id="hall-a", max_sessions=5, pool_id=pool["id"])
    lb = make_license(client, venue_id="hall-b", max_sessions=5, pool_id=pool["id"])
    assert verify(client, "cp-1", venue="hall-a").json()["decision"] == "approved"
    dry = verify(client, "cp-2", venue="hall-b").json()
    assert dry["reject_reason"] == "POOL_EXHAUSTED"

    cancel(client, "cp-1")

    reused = verify(client, "cp-3", venue="hall-b").json()
    assert reused["decision"] == "approved"
    assert reused["license_id"] == lb["id"]
    assert reused["pool_remaining_quota"] == 0


# ----------------------------------------------------------------------
# validation & error cases
# ----------------------------------------------------------------------
def test_cancel_unknown_key_is_404(client):
    resp = cancel(client, "no-such-key")
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"


def test_cancel_rejected_verification_is_422(client):
    rejected = verify(client, "c-rej", film="unknown", venue="nowhere").json()
    assert rejected["decision"] == "rejected"
    resp = cancel(client, "c-rej")
    assert resp.status_code == 422
    assert resp.json()["code"] == "VERIFICATION_NOT_APPROVED"


def test_cancel_started_session_is_422(client):
    lic = make_license(client, max_sessions=5)
    approved = verify(client, "c-past", start=PAST).json()
    assert approved["decision"] == "approved"
    resp = cancel(client, "c-past")
    assert resp.status_code == 422
    assert resp.json()["code"] == "SESSION_ALREADY_STARTED"
    # no release happened
    assert client.get(f"/licenses/{lic['id']}").json()["used_sessions"] == 1


def test_cancel_requires_non_blank_reason(client):
    make_license(client)
    verify(client, "c-1")
    assert cancel(client, "c-1", reason="").status_code == 422
    assert cancel(client, "c-1", reason="   ").status_code == 422
    assert client.post(
        "/verifications/c-1/cancel", json={}
    ).status_code == 422
    # rejected attempts leave the verification cancellable
    assert cancel(client, "c-1").status_code == 200


# ----------------------------------------------------------------------
# idempotency of the cancellation itself
# ----------------------------------------------------------------------
def test_duplicate_cancel_returns_first_record_without_double_release(client):
    lic = make_license(client, max_sessions=2)
    verify(client, "c-1")
    verify(client, "c-2", start=FUTURE_2)

    first = cancel(client, "c-1").json()
    second = cancel(client, "c-1").json()
    assert second["replay"] is True
    assert second["id"] == first["id"]
    assert second["reason"] == first["reason"]
    assert second["cancelled_at"] == first["cancelled_at"]

    # quota released exactly once: used went 2 -> 1, not 0
    assert client.get(f"/licenses/{lic['id']}").json()["used_sessions"] == 1


def test_cancel_same_key_with_different_reason_conflicts(client):
    make_license(client)
    verify(client, "c-1")
    first = cancel(client, "c-1", reason="密钥交付失败").json()

    resp = cancel(client, "c-1", reason="换个理由")
    assert resp.status_code == 409
    body = resp.json()
    assert body["code"] == "CONFLICT"
    assert body["existing"]["id"] == first["id"]
    assert body["existing"]["reason"] == "密钥交付失败"


# ----------------------------------------------------------------------
# the original verification stays untouched; queries expose the status
# ----------------------------------------------------------------------
def test_original_decision_snapshot_and_replay_survive_cancellation(client):
    make_license(client, max_sessions=2)
    approved = verify(client, "c-1").json()
    assert approved["remaining_quota"] == 1
    cancel(client, "c-1")

    # query shows the cancellation, original decision & snapshot unchanged
    fetched = client.get("/verifications/c-1").json()
    assert fetched["decision"] == "approved"
    assert fetched["remaining_quota"] == 1
    assert fetched["cancelled"] is True
    assert fetched["cancellation"]["reason"] == "放映设备故障，场次取消"
    assert fetched["cancellation"]["cancelled_at"]

    # same-key replay of the verification still returns the first result
    replay = verify(client, "c-1").json()
    assert replay["replay"] is True
    assert replay["decision"] == "approved"
    assert replay["remaining_quota"] == 1
    assert replay["cancelled"] is True

    # idempotency conflict payload carries the cancellation status too
    conflict = verify(client, "c-1", start=FUTURE_2)
    assert conflict.status_code == 409
    assert conflict.json()["existing"]["cancelled"] is True


def test_verification_list_exposes_cancellation_status(client):
    make_license(client, max_sessions=5)
    verify(client, "c-1")
    verify(client, "c-2", start=FUTURE_2)
    cancel(client, "c-1")

    rows = client.get("/verifications", params={"decision": "approved"}).json()
    assert len(rows) == 2  # cancelled flow keeps its approved decision
    by_key = {r["idempotency_key"]: r for r in rows}
    assert by_key["c-1"]["cancelled"] is True
    assert by_key["c-1"]["cancellation"]["reason"] == "放映设备故障，场次取消"
    assert by_key["c-2"]["cancelled"] is False
    assert by_key["c-2"]["cancellation"] is None


# ----------------------------------------------------------------------
# revoke / suspension rules still apply to new verifications
# ----------------------------------------------------------------------
def test_revoke_still_blocks_new_verifications_after_cancel(client):
    lic = make_license(client, max_sessions=1)
    verify(client, "c-1")
    cancel(client, "c-1")  # quota freed, but revocation wins afterwards
    client.post(f"/licenses/{lic['id']}/revoke", json={"reason": "撤片"})
    data = verify(client, "c-2", start=FUTURE_2).json()
    assert data["reject_reason"] == "LICENSE_REVOKED"


def test_suspension_still_blocks_new_verifications_after_cancel(client):
    lic = make_license(client, max_sessions=1)
    verify(client, "c-1")
    cancel(client, "c-1")
    client.post(
        f"/licenses/{lic['id']}/suspensions",
        json={
            "suspend_from": "2030-06-01T00:00:00Z",
            "suspend_to": "2030-07-01T00:00:00Z",
            "reason": "暂停",
        },
    )
    data = verify(client, "c-2", start=FUTURE_2).json()
    assert data["reject_reason"] == "LICENSE_SUSPENDED"


# ----------------------------------------------------------------------
# concurrency: no oversell, no double release
# ----------------------------------------------------------------------
def test_concurrent_cancel_and_verify_never_exceed_quota(tmp_path):
    store = Store(str(tmp_path / "cancel-concurrent.db"))
    lic = store.create_license(
        film_id="film-x",
        venue_id="hall-1",
        valid_from=datetime(2020, 1, 1, tzinfo=timezone.utc),
        valid_to=datetime(2031, 1, 1, tzinfo=timezone.utc),
        max_sessions=1,
    )
    seed = store.verify(
        film_id="film-x",
        venue_id="hall-1",
        start_time=datetime(2030, 6, 1, 20, 0, tzinfo=timezone.utc),
        idempotency_key="seed",
    )
    assert seed["decision"] == "approved"

    results, errors = [], []
    barrier = threading.Barrier(21)

    def cancel_worker() -> None:
        barrier.wait()
        try:
            results.append(
                ("cancel", store.cancel_verification("seed", reason="场次取消"))
            )
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    def verify_worker(i: int) -> None:
        barrier.wait()
        try:
            results.append(
                (
                    "verify",
                    store.verify(
                        film_id="film-x",
                        venue_id="hall-1",
                        start_time=datetime(2030, 6, 1, 20, 0, tzinfo=timezone.utc),
                        idempotency_key=f"new-{i}",
                    ),
                )
            )
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=verify_worker, args=(i,)) for i in range(20)]
    threads.append(threading.Thread(target=cancel_worker))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    cancels = [r for kind, r in results if kind == "cancel"]
    assert len(cancels) == 1 and cancels[0]["replay"] is False
    approved = [
        r for kind, r in results
        if kind == "verify" and r["decision"] == "approved"
    ]
    # at most one newcomer grabs the freed unit; live usage always matches
    assert len(approved) <= 1
    if approved:
        assert approved[0]["remaining_quota"] == 0
    assert store.get_license(lic["id"])["used_sessions"] == len(approved)
    assert store.get_verification("seed")["cancelled"] is True


def test_concurrent_duplicate_cancel_releases_quota_once(tmp_path):
    store = Store(str(tmp_path / "cancel-dup.db"))
    lic = store.create_license(
        film_id="film-x",
        venue_id="hall-1",
        valid_from=datetime(2020, 1, 1, tzinfo=timezone.utc),
        valid_to=datetime(2031, 1, 1, tzinfo=timezone.utc),
        max_sessions=2,
    )
    for key in ("seed-1", "seed-2"):
        store.verify(
            film_id="film-x",
            venue_id="hall-1",
            start_time=datetime(2030, 6, 1, 20, 0, tzinfo=timezone.utc),
            idempotency_key=key,
        )
    assert store.get_license(lic["id"])["used_sessions"] == 2

    results, errors = [], []

    def worker() -> None:
        try:
            results.append(
                store.cancel_verification("seed-1", reason="同键并发取消")
            )
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(results) == 10
    assert sum(1 for r in results if not r["replay"]) == 1
    assert len({r["id"] for r in results}) == 1
    # released exactly once: 2 approved flows, one cancelled -> used == 1
    assert store.get_license(lic["id"])["used_sessions"] == 1
