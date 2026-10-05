"""End-to-end tests for time-limited license suspensions."""

from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.service import Store

WINDOW = {
    "valid_from": "2026-01-01T00:00:00Z",
    "valid_to": "2027-01-01T00:00:00Z",
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


def verify(client, key, start="2026-06-01T20:00:00Z", film="film-1", venue="hall-a"):
    return client.post(
        "/verifications",
        json={
            "film_id": film,
            "venue_id": venue,
            "start_time": start,
            "idempotency_key": key,
        },
    )


def suspend(client, license_id, frm, to, reason="版权方通知暂停"):
    return client.post(
        f"/licenses/{license_id}/suspensions",
        json={"suspend_from": frm, "suspend_to": to, "reason": reason},
    )


# ----------------------------------------------------------------------
# registration & validation
# ----------------------------------------------------------------------
def test_register_suspension_returns_id_interval_and_reason(client):
    lic = make_license(client)
    resp = suspend(
        client, lic["id"],
        "2026-06-01T12:00:00+02:00",  # == 10:00Z
        "2026-06-02T12:00:00+02:00",  # == 10:00Z next day
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["id"] >= 1
    assert data["license_id"] == lic["id"]
    assert data["reason"] == "版权方通知暂停"
    # stored normalised to UTC
    assert data["suspend_from"] == "2026-06-01T10:00:00.000000Z"
    assert data["suspend_to"] == "2026-06-02T10:00:00.000000Z"
    assert data["created_at"]


def test_register_suspension_unknown_license_is_404(client):
    resp = suspend(client, 999, "2026-06-01T00:00:00Z", "2026-06-02T00:00:00Z")
    assert resp.status_code == 404
    # rejection leaves no record
    assert client.get("/suspensions").json() == []


def test_register_suspension_requires_timezone(client):
    lic = make_license(client)
    resp = suspend(
        client, lic["id"],
        "2026-06-01T00:00:00", "2026-06-02T00:00:00",
    )
    assert resp.status_code == 422
    assert client.get(f"/licenses/{lic['id']}/suspensions").json() == []


def test_register_suspension_rejects_inverted_window(client):
    lic = make_license(client)
    resp = suspend(
        client, lic["id"],
        "2026-06-02T00:00:00Z", "2026-06-01T00:00:00Z",
    )
    assert resp.status_code == 422


def test_register_suspension_rejects_empty_interval(client):
    lic = make_license(client)
    resp = suspend(
        client, lic["id"],
        "2026-06-01T00:00:00Z", "2026-06-01T00:00:00Z",
    )
    assert resp.status_code == 422


def test_register_suspension_rejects_blank_reason(client):
    lic = make_license(client)
    resp = client.post(
        f"/licenses/{lic['id']}/suspensions",
        json={
            "suspend_from": "2026-06-01T00:00:00Z",
            "suspend_to": "2026-06-02T00:00:00Z",
            "reason": "   ",
        },
    )
    assert resp.status_code == 422


def test_register_suspension_rejects_unparseable_time(client):
    lic = make_license(client)
    resp = suspend(client, lic["id"], "not-a-time", "2026-06-02T00:00:00Z")
    assert resp.status_code == 422


# ----------------------------------------------------------------------
# half-open interval [from, to)
# ----------------------------------------------------------------------
def test_half_open_interval_start_included_end_excluded(client):
    lic = make_license(client)
    assert suspend(
        client, lic["id"],
        "2026-06-01T20:00:00Z", "2026-06-01T22:00:00Z",
    ).status_code == 201

    before = verify(client, "s-before", start="2026-06-01T19:59:59Z")
    at_start = verify(client, "s-start", start="2026-06-01T20:00:00Z")
    inside = verify(client, "s-mid", start="2026-06-01T21:00:00Z")
    at_end = verify(client, "s-end", start="2026-06-01T22:00:00Z")

    assert before.json()["decision"] == "approved"
    assert at_start.json()["reject_reason"] == "LICENSE_SUSPENDED"
    assert inside.json()["reject_reason"] == "LICENSE_SUSPENDED"
    assert at_end.json()["decision"] == "approved"


def test_overlapping_intervals_take_effect_as_union(client):
    lic = make_license(client)
    # [10:00,12:00) and [11:00,14:00) -> union [10:00,14:00)
    suspend(client, lic["id"], "2026-06-01T10:00:00Z", "2026-06-01T12:00:00Z")
    suspend(client, lic["id"], "2026-06-01T11:00:00Z", "2026-06-01T14:00:00Z")

    for key, t, suspended in [
        ("u-1", "2026-06-01T10:30:00Z", True),
        ("u-2", "2026-06-01T11:30:00Z", True),  # overlap zone
        ("u-3", "2026-06-01T13:30:00Z", True),  # only second interval
        ("u-4", "2026-06-01T14:00:00Z", False),
    ]:
        data = verify(client, key, start=t).json()
        if suspended:
            assert data["decision"] == "rejected"
            assert data["reject_reason"] == "LICENSE_SUSPENDED"
        else:
            assert data["decision"] == "approved"


# ----------------------------------------------------------------------
# interaction with verification, quota, revocation and pools
# ----------------------------------------------------------------------
def test_suspension_does_not_change_existing_flow_and_quota(client):
    lic = make_license(client, max_sessions=3)
    first = verify(client, "k-1").json()
    assert first["decision"] == "approved"
    assert first["remaining_quota"] == 2

    suspend(client, lic["id"], "2026-06-01T00:00:00Z", "2026-06-30T00:00:00Z")

    blocked = verify(client, "k-2").json()
    assert blocked["reject_reason"] == "LICENSE_SUSPENDED"
    assert blocked["license_id"] is None
    assert blocked["remaining_quota"] is None

    # the rejected verification consumed no quota; the historical row stands
    lic_after = client.get(f"/licenses/{lic['id']}").json()
    assert lic_after["used_sessions"] == 1
    assert lic_after["remaining_sessions"] == 2
    history = client.get("/verifications/k-1").json()
    assert history["decision"] == "approved"
    assert history["remaining_quota"] == 2


def test_suspension_only_matches_session_start(client):
    lic = make_license(client)
    suspend(client, lic["id"], "2026-06-01T20:00:00Z", "2026-06-01T22:00:00Z")
    outside = verify(client, "k-out", start="2026-07-01T20:00:00Z")
    assert outside.json()["decision"] == "approved"


def test_suspended_license_skipped_in_favour_of_other_valid_license(client):
    early = make_license(client, valid_to="2026-09-01T00:00:00Z", max_sessions=3)
    late = make_license(client, valid_to="2027-01-01T00:00:00Z", max_sessions=3)
    # suspend only the earlier-expiring license; the later one must still be picked
    suspend(client, early["id"], "2026-06-01T00:00:00Z", "2026-06-30T00:00:00Z")

    data = verify(client, "k-1").json()
    assert data["decision"] == "approved"
    assert data["license_id"] == late["id"]
    assert data["remaining_quota"] == 2


def test_suspension_is_license_scoped_not_film_scoped(client):
    hall_a = make_license(client, venue_id="hall-a")
    hall_b = make_license(client, venue_id="hall-b")
    suspend(client, hall_a["id"], "2026-06-01T00:00:00Z", "2026-06-30T00:00:00Z")

    a = verify(client, "k-a", venue="hall-a").json()
    b = verify(client, "k-b", venue="hall-b").json()
    assert a["reject_reason"] == "LICENSE_SUSPENDED"
    assert b["decision"] == "approved"
    assert b["license_id"] == hall_b["id"]


def test_all_covering_licenses_suspended_returns_license_suspended(client):
    l1 = make_license(client, max_sessions=5)
    l2 = make_license(client, max_sessions=5)
    suspend(client, l1["id"], "2026-06-01T00:00:00Z", "2026-06-30T00:00:00Z")
    suspend(client, l2["id"], "2026-06-01T00:00:00Z", "2026-06-30T00:00:00Z")

    data = verify(client, "k-1").json()
    assert data["decision"] == "rejected"
    assert data["reject_reason"] == "LICENSE_SUSPENDED"


def test_suspension_takes_precedence_over_quota_exhaustion(client):
    lic = make_license(client, max_sessions=1)
    verify(client, "k-1")  # own balance now 0
    suspend(client, lic["id"], "2026-06-01T00:00:00Z", "2026-06-30T00:00:00Z")
    data = verify(client, "k-2").json()
    assert data["reject_reason"] == "LICENSE_SUSPENDED"


def test_quota_exhausted_when_one_exhausted_suspended_other_exhausted(client):
    """Suspended license skipped; remaining usable-space license out of own
    balance -> legacy QUOTA_EXHAUSTED order is preserved."""
    suspended_lic = make_license(client, max_sessions=5)
    free_lic = make_license(client, max_sessions=1)
    suspend(client, suspended_lic["id"],
            "2026-06-01T00:00:00Z", "2026-06-30T00:00:00Z")
    verify(client, "k-1")  # consumes the single quota of the free license
    data = verify(client, "k-2").json()
    assert data["decision"] == "rejected"
    assert data["reject_reason"] == "QUOTA_EXHAUSTED"


def test_suspended_pool_bound_license_falls_through_to_pool_check(client):
    pool = client.post(
        "/pools", json={"film_id": "film-1", "max_sessions": 1}
    ).json()
    suspended_bound = make_license(
        client, max_sessions=5, pool_id=pool["id"],
        valid_to="2026-09-01T00:00:00Z",
    )
    later_bound = make_license(
        client, max_sessions=5, pool_id=pool["id"],
        valid_to="2027-06-01T00:00:00Z",
    )
    suspend(client, suspended_bound["id"],
            "2026-06-01T00:00:00Z", "2026-06-30T00:00:00Z")

    first = verify(client, "k-1").json()  # skips suspended license, consumes pool via later
    assert first["license_id"] == later_bound["id"]
    assert first["pool_remaining_quota"] == 0

    # the suspended license would otherwise have been earliest and pool-dry;
    # with it suspended only the later license is considered -> POOL_EXHAUSTED
    second = verify(client, "k-2").json()
    assert second["reject_reason"] == "POOL_EXHAUSTED"


def test_revocation_still_blocks_alongside_suspension(client):
    revoked = make_license(client, valid_to="2026-09-01T00:00:00Z")
    suspended_lic = make_license(client, valid_to="2027-06-01T00:00:00Z")
    client.post(f"/licenses/{revoked['id']}/revoke", json={"reason": "撤片"})
    suspend(client, suspended_lic["id"],
            "2026-06-01T00:00:00Z", "2026-06-30T00:00:00Z")

    data = verify(client, "k-1").json()
    # one covering license revoked, the other suspended -> LICENSE_SUSPENDED
    assert data["reject_reason"] == "LICENSE_SUSPENDED"


def test_all_covering_revoked_still_returns_license_revoked(client):
    l1 = make_license(client)
    l2 = make_license(client)
    client.post(f"/licenses/{l1['id']}/revoke", json={"reason": "x"})
    client.post(f"/licenses/{l2['id']}/revoke", json={"reason": "y"})
    suspend(client, l1["id"], "2026-06-01T00:00:00Z", "2026-06-30T00:00:00Z")
    data = verify(client, "k-1").json()
    assert data["reject_reason"] == "LICENSE_REVOKED"


def test_out_of_window_unaffected_by_suspension(client):
    lic = make_license(
        client,
        valid_from="2026-01-01T00:00:00Z",
        valid_to="2026-02-01T00:00:00Z",
    )
    suspend(client, lic["id"], "2026-01-01T00:00:00Z", "2026-12-31T00:00:00Z")
    data = verify(client, "k-1", start="2026-06-01T20:00:00Z").json()
    assert data["reject_reason"] == "TIME_OUT_OF_WINDOW"


def test_idempotent_replay_returns_first_result_after_suspension(client):
    lic = make_license(client)
    first = verify(client, "dup").json()
    assert first["decision"] == "approved"

    suspend(client, lic["id"], "2026-01-01T00:00:00Z", "2027-01-01T00:00:00Z")

    second = verify(client, "dup").json()
    assert second["replay"] is True
    assert second["id"] == first["id"]
    assert second["decision"] == "approved"
    assert second["license_id"] == lic["id"]
    # quota still consumed exactly once
    assert client.get(f"/licenses/{lic['id']}").json()["used_sessions"] == 1


def test_suspension_lifted_after_interval_end(client):
    lic = make_license(client)
    suspend(client, lic["id"], "2026-06-01T00:00:00Z", "2026-06-02T00:00:00Z")
    blocked = verify(client, "k-1", start="2026-06-01T12:00:00Z").json()
    assert blocked["reject_reason"] == "LICENSE_SUSPENDED"
    after = verify(client, "k-2", start="2026-06-02T00:00:00Z").json()
    assert after["decision"] == "approved"


# ----------------------------------------------------------------------
# queries & immutability
# ----------------------------------------------------------------------
def test_suspension_records_are_queryable(client):
    lic = make_license(client)
    s1 = suspend(
        client, lic["id"], "2026-06-01T00:00:00Z", "2026-06-02T00:00:00Z", "one"
    ).json()
    s2 = suspend(
        client, lic["id"], "2026-07-01T00:00:00Z", "2026-07-02T00:00:00Z", "two"
    ).json()

    by_id = client.get(f"/suspensions/{s1['id']}")
    assert by_id.status_code == 200
    assert by_id.json()["reason"] == "one"

    per_license = client.get(f"/licenses/{lic['id']}/suspensions").json()
    assert [s["id"] for s in per_license] == [s1["id"], s2["id"]]

    listing = client.get("/suspensions", params={"license_id": lic["id"]}).json()
    assert [s["id"] for s in listing] == [s1["id"], s2["id"]]
    assert client.get("/suspensions").json()  # global list works too


def test_list_suspensions_unknown_license_is_404(client):
    assert client.get("/licenses/999/suspensions").status_code == 404
    assert client.get("/suspensions/999").status_code == 404


def test_suspension_records_cannot_be_modified(client):
    lic = make_license(client)
    s = suspend(
        client, lic["id"], "2026-06-01T00:00:00Z", "2026-06-02T00:00:00Z"
    ).json()
    # no update/delete endpoints exist
    for method in ("PUT", "PATCH", "DELETE"):
        resp = client.request(
            method, f"/suspensions/{s['id']}", json={"reason": "changed"}
        )
        assert resp.status_code in (404, 405), (method, resp.status_code)
    # record content is unchanged
    assert client.get(f"/suspensions/{s['id']}").json()["reason"] == "版权方通知暂停"


# ----------------------------------------------------------------------
# concurrency: each verification decision is a complete before/after state
# ----------------------------------------------------------------------
def test_concurrent_verify_vs_suspension_registration(tmp_path):
    store = Store(str(tmp_path / "suspend-concurrent.db"))
    lic = store.create_license(
        film_id="film-x",
        venue_id="hall-1",
        valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        valid_to=datetime(2027, 1, 1, tzinfo=timezone.utc),
        max_sessions=100,
    )
    slot = datetime(2026, 6, 1, 20, 0, tzinfo=timezone.utc)
    window = (
        datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 6, 2, 0, 0, tzinfo=timezone.utc),
    )

    results, errors = [], []
    barrier = threading.Barrier(21)

    def verify_worker(i: int) -> None:
        barrier.wait()
        try:
            results.append(
                store.verify(
                    film_id="film-x",
                    venue_id="hall-1",
                    start_time=slot,
                    idempotency_key=f"ck-{i}",
                )
            )
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    def suspend_worker() -> None:
        barrier.wait()
        try:
            store.create_suspension(
                lic["id"],
                suspend_from=window[0],
                suspend_to=window[1],
                reason="concurrent pause",
            )
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=verify_worker, args=(i,)) for i in range(20)]
    threads.append(threading.Thread(target=suspend_worker))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    approved = [r for r in results if r["decision"] == "approved"]
    suspended = [r for r in results if r["reject_reason"] == "LICENSE_SUSPENDED"]
    assert len(approved) + len(suspended) == 20
    # decisions are consistent: approvals consumed sequential quota values,
    # nothing partially applied
    assert sorted(r["remaining_quota"] for r in approved) == list(
        range(100 - len(approved), 100)
    )
    assert len(store.list_suspensions(license_id=lic["id"])) == 1
    assert store.get_license(lic["id"])["used_sessions"] == len(approved)
