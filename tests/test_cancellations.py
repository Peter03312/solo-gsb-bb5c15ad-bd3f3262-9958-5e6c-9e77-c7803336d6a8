"""End-to-end tests for cancellation of approved screenings."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.service import Store
from app.timeutil import to_iso

FUTURE = "2030-06-01T20:00:00Z"
PAST = "2020-06-01T20:00:00Z"
WINDOW = {
    "valid_from": "2026-01-01T00:00:00Z",
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
        "max_sessions": 10,
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


def cancel(client, key, reason="台风预警，场次临时取消"):
    return client.post(
        f"/verifications/{key}/cancellation", json={"reason": reason}
    )


# ----------------------------------------------------------------------
# basic cancellation
# ----------------------------------------------------------------------
def test_cancel_approved_returns_time_reason_license_and_pool(client):
    lic = make_license(client)
    approved = verify(client, "k-1").json()
    assert approved["decision"] == "approved"
    assert approved["cancelled"] is False
    assert approved["cancellation"] is None

    resp = cancel(client, "k-1")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["id"] >= 1
    assert data["idempotency_key"] == "k-1"
    assert data["verification_id"] == approved["id"]
    assert data["license_id"] == lic["id"]
    assert data["pool_id"] is None
    assert data["reason"] == "台风预警，场次临时取消"
    assert data["cancelled_at"]


def test_cancel_refunds_license_quota_and_verification_keeps_snapshot(client):
    lic = make_license(client, max_sessions=2)
    approved = verify(client, "k-1").json()
    assert approved["remaining_quota"] == 1
    assert client.get(f"/licenses/{lic['id']}").json()["used_sessions"] == 1

    assert cancel(client, "k-1").status_code == 200

    # live counters rolled back by one
    lic_after = client.get(f"/licenses/{lic['id']}").json()
    assert lic_after["used_sessions"] == 0
    assert lic_after["remaining_sessions"] == 2

    # the original row keeps its decision and deduction-time snapshot
    row = client.get("/verifications/k-1").json()
    assert row["decision"] == "approved"
    assert row["remaining_quota"] == 1
    assert row["cancelled"] is True
    assert row["cancellation"]["reason"] == "台风预警，场次临时取消"
    assert row["cancellation"]["license_id"] == lic["id"]


def test_released_quota_is_usable_by_a_following_verification(client):
    make_license(client, max_sessions=1)
    first = verify(client, "k-1").json()
    assert first["decision"] == "approved"
    # quota exhausted now
    assert verify(client, "k-2").json()["reject_reason"] == "QUOTA_EXHAUSTED"

    cancel(client, "k-1")

    again = verify(client, "k-3").json()
    assert again["decision"] == "approved"
    assert again["remaining_quota"] == 0
    lic = client.get("/licenses").json()[0]
    assert lic["used_sessions"] == 1
    assert lic["remaining_sessions"] == 0


# ----------------------------------------------------------------------
# error cases
# ----------------------------------------------------------------------
def test_cancel_unknown_key_is_404_and_leaves_no_record(client):
    make_license(client)
    resp = cancel(client, "no-such-key")
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"
    assert client.get("/cancellations").json() == []


def test_cancel_rejected_verification_is_422_not_approved(client):
    make_license(client, max_sessions=1)
    verify(client, "k-1")
    rejected = verify(client, "k-2")
    assert rejected.json()["decision"] == "rejected"

    resp = cancel(client, "k-2")
    assert resp.status_code == 422
    assert resp.json()["code"] == "VERIFICATION_NOT_APPROVED"
    # rejected verifications still show no cancellation
    assert client.get("/verifications/k-2").json()["cancelled"] is False


def test_cancel_started_screening_is_422(client):
    make_license(
        client,
        valid_from="2019-01-01T00:00:00Z",
        valid_to="2027-01-01T00:00:00Z",
    )
    verify(client, "k-old", start=PAST)

    resp = cancel(client, "k-old")
    assert resp.status_code == 422
    assert resp.json()["code"] == "SCREENING_ALREADY_STARTED"
    # no quota released
    lic = client.get("/licenses").json()[0]
    assert lic["used_sessions"] == 1


def test_cancel_boundary_now_equal_start_is_rejected(tmp_path):
    """Half-open availability: the slot is cancellable only while now < start."""
    store = Store(str(tmp_path / "boundary.db"))
    start = datetime.now(timezone.utc) - timedelta(seconds=1)
    store.create_license(
        film_id="film-1", venue_id="hall-a",
        valid_from=datetime(2020, 1, 1, tzinfo=timezone.utc),
        valid_to=datetime(2030, 1, 1, tzinfo=timezone.utc),
        max_sessions=5,
    )
    approved = store.verify(
        film_id="film-1", venue_id="hall-a",
        start_time=start, idempotency_key="k-late",
    )
    assert approved["decision"] == "approved"
    with pytest.raises(Exception) as exc:
        store.cancel_verification("k-late", "too late")
    assert exc.value.code == "SCREENING_ALREADY_STARTED"


def test_cancel_requires_non_blank_reason(client):
    make_license(client)
    verify(client, "k-1")
    resp = client.post(
        "/verifications/k-1/cancellation", json={"reason": "   "}
    )
    assert resp.status_code == 422


def test_cancel_after_license_revoked_still_refunds(client):
    """Cancellation and revocation are independent: revoking after approval
    does not prevent cancelling the pre-start approved screening."""
    lic = make_license(client, max_sessions=3)
    verify(client, "k-1")
    client.post(f"/licenses/{lic['id']}/revoke", json={"reason": "撤片"})

    resp = cancel(client, "k-1")
    assert resp.status_code == 200
    assert client.get(f"/licenses/{lic['id']}").json()["used_sessions"] == 0


# ----------------------------------------------------------------------
# idempotency & conflict
# ----------------------------------------------------------------------
def test_repeated_cancel_same_reason_returns_first_record_and_releases_once(client):
    make_license(client, max_sessions=3)
    verify(client, "k-1")
    first = cancel(client, "k-1", reason="same reason").json()
    second = cancel(client, "k-1", reason="same reason")
    assert second.status_code == 200
    assert second.json()["id"] == first["id"]
    assert second.json()["cancelled_at"] == first["cancelled_at"]

    lic = client.get("/licenses").json()[0]
    assert lic["used_sessions"] == 0  # released once, not twice
    assert len(client.get("/cancellations").json()) == 1


def test_repeated_cancel_different_reason_is_409(client):
    make_license(client)
    verify(client, "k-1")
    cancel(client, "k-1", reason="first reason")
    resp = cancel(client, "k-1", reason="different reason")
    assert resp.status_code == 409
    body = resp.json()
    assert body["code"] == "CONFLICT"
    assert body["existing"]["reason"] == "first reason"
    assert body["existing"]["idempotency_key"] == "k-1"
    # still only one cancellation record, quota released once
    assert len(client.get("/cancellations").json()) == 1
    assert client.get("/licenses").json()[0]["used_sessions"] == 0


def test_replay_after_cancel_keeps_decision_and_snapshot(client):
    make_license(client, max_sessions=2)
    first = verify(client, "dup").json()
    cancel(client, "dup")

    replay = verify(client, "dup").json()
    assert replay["replay"] is True
    assert replay["id"] == first["id"]
    assert replay["decision"] == "approved"
    assert replay["remaining_quota"] == first["remaining_quota"] == 1
    assert replay["cancelled"] is True
    assert replay["cancellation"]["reason"] == "台风预警，场次临时取消"
    # released quota stays released (replay neither deducts nor re-cancels)
    assert client.get("/licenses").json()[0]["used_sessions"] == 0


# ----------------------------------------------------------------------
# shared pool
# ----------------------------------------------------------------------
def test_cancel_refunds_bound_pool_too(client):
    pool = client.post(
        "/pools", json={"film_id": "film-1", "max_sessions": 2}
    ).json()
    lic = make_license(client, pool_id=pool["id"], max_sessions=5)
    approved = verify(client, "k-1").json()
    assert approved["pool_remaining_quota"] == 1

    resp = cancel(client, "k-1")
    assert resp.status_code == 200
    data = resp.json()
    assert data["license_id"] == lic["id"]
    assert data["pool_id"] == pool["id"]

    pool_after = client.get(f"/pools/{pool['id']}").json()
    assert pool_after["used_sessions"] == 0
    assert pool_after["remaining_sessions"] == 2
    lic_after = client.get(f"/licenses/{lic['id']}").json()
    assert lic_after["used_sessions"] == 0
    # the verification snapshot still shows the post-deduction pool balance
    row = client.get("/verifications/k-1").json()
    assert row["pool_remaining_quota"] == 1
    assert row["cancellation"]["pool_id"] == pool["id"]


def test_released_pool_slot_usable_cross_venue(client):
    pool = client.post(
        "/pools", json={"film_id": "film-1", "max_sessions": 1}
    ).json()
    make_license(client, venue_id="hall-a", pool_id=pool["id"], max_sessions=10)
    make_license(client, venue_id="hall-b", pool_id=pool["id"], max_sessions=10)

    a = verify(client, "k-a", venue="hall-a").json()
    assert a["decision"] == "approved"
    b = verify(client, "k-b", venue="hall-b").json()
    assert b["reject_reason"] == "POOL_EXHAUSTED"

    cancel(client, "k-a")

    b2 = verify(client, "k-b2", venue="hall-b").json()
    assert b2["decision"] == "approved"
    assert b2["pool_id"] == pool["id"]
    assert client.get(f"/pools/{pool['id']}").json()["used_sessions"] == 1


# ----------------------------------------------------------------------
# suspension interaction
# ----------------------------------------------------------------------
def test_suspension_blocks_new_verification_until_cancel_releases(client):
    lic = make_license(client, max_sessions=1)
    verify(client, "k-1")  # consumes the single quota
    # suspend the license for the window; even with quota it would be unusable
    client.post(
        f"/licenses/{lic['id']}/suspensions",
        json={
            "suspend_from": "2030-05-01T00:00:00Z",
            "suspend_to": "2030-07-01T00:00:00Z",
            "reason": "暂停",
        },
    )
    blocked = verify(client, "k-2").json()
    assert blocked["reject_reason"] == "LICENSE_SUSPENDED"

    # cancellation releases the quota but suspension still blocks new checks
    cancel(client, "k-1")
    still_blocked = verify(client, "k-3").json()
    assert still_blocked["reject_reason"] == "LICENSE_SUSPENDED"
    assert client.get(f"/licenses/{lic['id']}").json()["used_sessions"] == 0


# ----------------------------------------------------------------------
# queries
# ----------------------------------------------------------------------
def test_cancellation_queries_and_filters(client):
    pool = client.post(
        "/pools", json={"film_id": "film-1", "max_sessions": 5}
    ).json()
    lic = make_license(client, pool_id=pool["id"], max_sessions=5)
    verify(client, "k-1")
    verify(client, "k-2")
    rec = cancel(client, "k-1", reason="one").json()

    # by id
    by_id = client.get(f"/cancellations/{rec['id']}")
    assert by_id.status_code == 200
    assert by_id.json()["reason"] == "one"

    # by verification key
    by_key = client.get("/verifications/k-1/cancellation")
    assert by_key.status_code == 200
    assert by_key.json()["id"] == rec["id"]

    # unknown key / never cancelled -> 404
    assert client.get("/verifications/nope/cancellation").status_code == 404
    assert client.get("/verifications/k-2/cancellation").status_code == 404
    assert client.get("/cancellations/999").status_code == 404

    # global list + filters
    assert [c["idempotency_key"] for c in client.get("/cancellations").json()] == ["k-1"]
    assert len(client.get("/cancellations", params={"license_id": lic["id"]}).json()) == 1
    assert len(client.get("/cancellations", params={"pool_id": pool["id"]}).json()) == 1
    assert client.get("/cancellations", params={"film_id": "other"}).json() == []
    assert client.get("/cancellations", params={"venue_id": "hall-a"}).json()

    # verification list cancelled filter
    only_cancelled = client.get("/verifications", params={"cancelled": True}).json()
    assert [v["idempotency_key"] for v in only_cancelled] == ["k-1"]
    active = client.get("/verifications", params={"cancelled": False}).json()
    assert {v["idempotency_key"] for v in active} == {"k-2"}


# ----------------------------------------------------------------------
# concurrency: never over-consume nor double-release
# ----------------------------------------------------------------------
def test_concurrent_cancel_and_verify_never_over_consume(tmp_path):
    """cap-1 license: 1 approval + 1 cancellation racing with new verifications.

    At most one screening may hold the single quota at any time, and the
    cancellation must release it exactly once.  All write transactions are
    serialised by BEGIN IMMEDIATE, so every decision is consistent.
    """
    store = Store(str(tmp_path / "cancel-concurrent.db"))
    store.create_license(
        film_id="film-x",
        venue_id="hall-1",
        valid_from=datetime(2029, 1, 1, tzinfo=timezone.utc),
        valid_to=datetime(2031, 1, 1, tzinfo=timezone.utc),
        max_sessions=1,
    )
    slot = datetime(2030, 6, 1, 20, 0, tzinfo=timezone.utc)
    first = store.verify(
        film_id="film-x", venue_id="hall-1",
        start_time=slot, idempotency_key="orig",
    )
    assert first["decision"] == "approved"

    results, cancels, errors = [], [], []
    barrier = threading.Barrier(21)

    def verify_worker(i: int) -> None:
        barrier.wait()
        try:
            results.append(
                store.verify(
                    film_id="film-x", venue_id="hall-1",
                    start_time=slot, idempotency_key=f"new-{i}",
                )
            )
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    def cancel_worker() -> None:
        barrier.wait()
        try:
            cancels.append(store.cancel_verification("orig", "concurrent cancel"))
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=verify_worker, args=(i,)) for i in range(20)]
    threads.append(threading.Thread(target=cancel_worker))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(cancels) == 1
    # verifications before the release see QUOTA_EXHAUSTED; afterwards exactly
    # one new verification may consume the released quota
    approved = [r for r in results if r["decision"] == "approved"]
    rejected = [r for r in results if r["decision"] == "rejected"]
    assert len(approved) == 1
    assert len(rejected) == 19
    assert all(r["reject_reason"] == "QUOTA_EXHAUSTED" for r in rejected)
    # live usage never exceeds the cap; released exactly once
    assert store.get_license(1)["used_sessions"] == 1
    assert len(store.list_cancellations()) == 1
    # the cancelled row and the one new approval are distinct
    assert approved[0]["idempotency_key"] != "orig"
    assert store.get_verification("orig")["cancelled"] is True


def test_concurrent_cancel_and_verify_with_pool(tmp_path):
    """cap-1 pool across venues: released pool slot is consumed at most once."""
    store = Store(str(tmp_path / "cancel-pool.db"))
    store.create_pool(film_id="film-x", max_sessions=1)
    pool_id = store.list_pools(film_id="film-x")[0]["id"]
    for venue in ("hall-1", "hall-2"):
        store.create_license(
            film_id="film-x", venue_id=venue,
            valid_from=datetime(2029, 1, 1, tzinfo=timezone.utc),
            valid_to=datetime(2031, 1, 1, tzinfo=timezone.utc),
            max_sessions=100, pool_id=pool_id,
        )
    slot = datetime(2030, 6, 1, 20, 0, tzinfo=timezone.utc)
    store.verify(film_id="film-x", venue_id="hall-1",
                 start_time=slot, idempotency_key="orig")

    results, errors = [], []
    barrier = threading.Barrier(21)

    def verify_worker(i: int) -> None:
        barrier.wait()
        try:
            results.append(
                store.verify(
                    film_id="film-x", venue_id=f"hall-{i % 2 + 1}",
                    start_time=slot, idempotency_key=f"new-{i}",
                )
            )
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    def cancel_worker() -> None:
        barrier.wait()
        try:
            store.cancel_verification("orig", "release pool slot")
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=verify_worker, args=(i,)) for i in range(20)]
    threads.append(threading.Thread(target=cancel_worker))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    approved = [r for r in results if r["decision"] == "approved"]
    rejected = [r for r in results if r["decision"] == "rejected"]
    assert len(approved) == 1
    assert len(rejected) == 19
    assert all(r["reject_reason"] == "POOL_EXHAUSTED" for r in rejected)
    pool = store.get_pool(pool_id)
    assert pool["used_sessions"] == 1
    assert pool["remaining_sessions"] == 0


def test_concurrent_duplicate_cancel_single_release(tmp_path):
    """Same key cancelled from many threads: one record, one release."""
    store = Store(str(tmp_path / "cancel-dup.db"))
    store.create_license(
        film_id="film-x", venue_id="hall-1",
        valid_from=datetime(2029, 1, 1, tzinfo=timezone.utc),
        valid_to=datetime(2031, 1, 1, tzinfo=timezone.utc),
        max_sessions=3,
    )
    slot = datetime(2030, 6, 1, 20, 0, tzinfo=timezone.utc)
    store.verify(film_id="film-x", venue_id="hall-1",
                 start_time=slot, idempotency_key="same")

    records, errors = [], []

    def worker() -> None:
        try:
            records.append(store.cancel_verification("same", "same reason"))
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(15)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(records) == 15
    assert len({r["id"] for r in records}) == 1
    assert len(store.list_cancellations()) == 1
    assert store.get_license(1)["used_sessions"] == 0
