"""Business logic: license registry, quota pools, revocation and idempotent verification."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator, Optional

from .db import connect, init_schema
from .timeutil import now_iso, to_iso

DECISION_APPROVED = "approved"
DECISION_REJECTED = "rejected"

REASON_NO_LICENSE = "NO_LICENSE"             # 该影片+场馆没有任何授权
REASON_TIME_OUT_OF_WINDOW = "TIME_OUT_OF_WINDOW"  # 有授权但开始时间不在任何有效时段内
REASON_LICENSE_REVOKED = "LICENSE_REVOKED"   # 覆盖该时段的授权均已撤销
REASON_LICENSE_SUSPENDED = "LICENSE_SUSPENDED"  # 覆盖该时段且未撤销的授权全部处于暂停中
REASON_QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"   # 授权有效但场次额度已用完
REASON_POOL_EXHAUSTED = "POOL_EXHAUSTED"     # 授权自身有余额，但所绑影片级共享池已耗尽
REASON_NOT_APPROVED = "VERIFICATION_NOT_APPROVED"  # 流水非核准（被拒绝），不可取消
REASON_ALREADY_STARTED = "SCREENING_ALREADY_STARTED"  # 场次已开始（或已过），不可取消

# 已取消的核准流水不再计入实时已用次数（授权与所绑池各回退一次）；
# NOT EXISTS 同时兼容取消表尚未在旧库中创建前的历史数据路径。
_LICENSE_SELECT = """
SELECT
    l.id,
    l.film_id,
    l.venue_id,
    l.valid_from,
    l.valid_to,
    l.max_sessions,
    l.pool_id,
    l.note,
    l.created_at,
    p.film_id AS pool_film_id,
    p.max_sessions AS pool_max_sessions,
    p.note AS pool_note,
    p.created_at AS pool_created_at,
    CASE WHEN r.id IS NULL THEN 0 ELSE 1 END AS revoked,
    r.reason     AS revoke_reason,
    r.revoked_at AS revoked_at,
    (
        SELECT COUNT(*)
        FROM verifications v
        WHERE v.license_id = l.id AND v.decision = 'approved'
          AND NOT EXISTS (
              SELECT 1 FROM cancellations c WHERE c.verification_id = v.id
          )
    ) AS used_sessions,
    (
        SELECT COUNT(*)
        FROM verifications v
        WHERE v.pool_id = l.pool_id AND v.decision = 'approved'
          AND NOT EXISTS (
              SELECT 1 FROM cancellations c WHERE c.verification_id = v.id
          )
    ) AS pool_used_sessions
FROM licenses l
LEFT JOIN revocations r ON r.license_id = l.id
LEFT JOIN quota_pools p ON p.id = l.pool_id
"""

_POOL_SELECT = """
SELECT
    p.id,
    p.film_id,
    p.max_sessions,
    p.note,
    p.created_at,
    (
        SELECT COUNT(*)
        FROM verifications v
        WHERE v.pool_id = p.id AND v.decision = 'approved'
          AND NOT EXISTS (
              SELECT 1 FROM cancellations c WHERE c.verification_id = v.id
          )
    ) AS used_sessions,
    (
        SELECT COUNT(*)
        FROM licenses l
        WHERE l.pool_id = p.id
    ) AS license_count
FROM quota_pools p
"""

# 核验流水 + 其取消登记（一对一；未取消时 c.* 为 NULL）。取消不改写原行，
# 原核验决定与扣减时余额快照始终从 v.* 读取，取消状态由 LEFT JOIN 附加。
_VERIFICATION_SELECT = """
SELECT
    v.*,
    CASE WHEN c.id IS NULL THEN 0 ELSE 1 END AS is_cancelled,
    c.id            AS cancellation_id,
    c.reason        AS cancellation_reason,
    c.cancelled_at  AS cancelled_at,
    c.license_id    AS cancellation_license_id,
    c.pool_id       AS cancellation_pool_id
FROM verifications v
LEFT JOIN cancellations c ON c.verification_id = v.id
"""


class ConflictError(Exception):
    """409 — e.g. idempotency key reused with a different payload."""

    def __init__(self, message: str, existing: Optional[dict] = None):
        super().__init__(message)
        self.existing = existing


class NotFoundError(Exception):
    """404 — referenced resource does not exist."""


class UnprocessableError(Exception):
    """422 — semantically invalid request that passes schema validation,
    e.g. binding a license to a missing pool or to a pool of another film."""

    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


def _hash_request(film_id: str, venue_id: str, start_iso: str) -> str:
    payload = json.dumps(
        {"film_id": film_id, "venue_id": venue_id, "start_time": start_iso},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _pool_brief(row: sqlite3.Row, used: Optional[int] = None) -> Optional[dict]:
    """Pool snapshot attached to a license row; ``None`` when unbound."""
    if row["pool_id"] is None:
        return None
    if used is None:
        used = row["pool_used_sessions"]
    max_sessions = row["pool_max_sessions"]
    return {
        "id": row["pool_id"],
        "film_id": row["pool_film_id"],
        "max_sessions": max_sessions,
        "used_sessions": used,
        "remaining_sessions": max_sessions - used,
    }


def _license_out(row: sqlite3.Row) -> dict:
    used = row["used_sessions"]
    revoked = bool(row["revoked"])
    return {
        "id": row["id"],
        "film_id": row["film_id"],
        "venue_id": row["venue_id"],
        "valid_from": row["valid_from"],
        "valid_to": row["valid_to"],
        "max_sessions": row["max_sessions"],
        "used_sessions": used,
        "remaining_sessions": row["max_sessions"] - used,
        "pool_id": row["pool_id"],
        "pool": _pool_brief(row),
        "revoked": revoked,
        "revocation": (
            {"reason": row["revoke_reason"], "revoked_at": row["revoked_at"]}
            if revoked
            else None
        ),
        "note": row["note"],
        "created_at": row["created_at"],
    }


def _pool_out(row: sqlite3.Row) -> dict:
    used = row["used_sessions"]
    return {
        "id": row["id"],
        "film_id": row["film_id"],
        "max_sessions": row["max_sessions"],
        "used_sessions": used,
        "remaining_sessions": row["max_sessions"] - used,
        "bound_licenses": row["license_count"],
        "note": row["note"],
        "created_at": row["created_at"],
    }


def _suspension_out(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "license_id": row["license_id"],
        "suspend_from": row["suspend_from"],
        "suspend_to": row["suspend_to"],
        "reason": row["reason"],
        "created_at": row["created_at"],
    }


def _cancellation_brief(row: sqlite3.Row) -> Optional[dict]:
    """Cancellation attached to a verification row; ``None`` when active."""
    if not row["is_cancelled"]:
        return None
    return {
        "id": row["cancellation_id"],
        "reason": row["cancellation_reason"],
        "cancelled_at": row["cancelled_at"],
        "license_id": row["cancellation_license_id"],
        "pool_id": row["cancellation_pool_id"],
    }


def _verification_out(row: sqlite3.Row, replay: bool) -> dict:
    # 取消不改动原核验行：decision / *_remaining_quota 等首次决定与
    # 扣减时余额快照一律原样返回；取消状态只通过附加字段暴露。
    return {
        "id": row["id"],
        "idempotency_key": row["idempotency_key"],
        "film_id": row["film_id"],
        "venue_id": row["venue_id"],
        "start_time": row["start_time"],
        "decision": row["decision"],
        "license_id": row["license_id"],
        "pool_id": row["pool_id"],
        "reject_reason": row["reject_reason"],
        "remaining_quota": row["remaining_quota"],
        "pool_remaining_quota": row["pool_remaining_quota"],
        "verified_at": row["created_at"],
        "replay": replay,
        "cancelled": bool(row["is_cancelled"]),
        "cancellation": _cancellation_brief(row),
    }


def _cancellation_out(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "idempotency_key": row["idempotency_key"],
        "verification_id": row["verification_id"],
        "license_id": row["license_id"],
        "pool_id": row["pool_id"],
        "reason": row["reason"],
        "cancelled_at": row["cancelled_at"],
    }


def _decide(candidates: list, start_iso: str, suspended_ids: Optional[set] = None):
    """Pick the license to consume, or the reason to reject.

    ``candidates`` is ordered by ``valid_to ASC`` so the first usable entry
    is the earliest-expiring license.  A license is usable only when it
    covers the slot, is neither revoked nor suspended at the slot, **and**
    still has balance of both its own quota and its bound pool.  Exactly
    one unit of the chosen license and one unit of its pool are consumed
    per approval.

    Suspended licenses are skipped just like revoked ones, but they get
    their own reason: ``LICENSE_SUSPENDED`` is reported when at least one
    unrevoked license covers the slot yet every such license is suspended
    at the slot (overlapping suspension intervals count as their union).
    Otherwise the decision falls back to the legacy precedence with
    suspended licenses removed from consideration, so the original
    reasons keep their original order.

    ``POOL_EXHAUSTED`` is reported when in-window, non-revoked,
    non-suspended licenses still have their own balance but every such
    license is blocked only because its bound pool is exhausted.
    """
    suspended_ids = suspended_ids or set()
    if not candidates:
        return DECISION_REJECTED, None, None, REASON_NO_LICENSE, None, None
    in_window = [c for c in candidates if c["valid_from"] <= start_iso <= c["valid_to"]]
    if not in_window:
        return DECISION_REJECTED, None, None, REASON_TIME_OUT_OF_WINDOW, None, None
    active = [c for c in in_window if not c["revoked"]]
    if not active:
        return DECISION_REJECTED, None, None, REASON_LICENSE_REVOKED, None, None
    if all(c["id"] in suspended_ids for c in active):
        return DECISION_REJECTED, None, None, REASON_LICENSE_SUSPENDED, None, None

    # skip suspended licenses; every remaining reason keeps the old order
    effective = [c for c in active if c["id"] not in suspended_ids]
    own_balance = [c for c in effective if c["used_sessions"] < c["max_sessions"]]
    if not own_balance:
        return DECISION_REJECTED, None, None, REASON_QUOTA_EXHAUSTED, None, None

    # earliest-expiring license with own balance; pool balance re-checked
    # against the pool actually bound to it (pool usage is film-global).
    for cand in own_balance:
        if cand["pool_id"] is None:
            return (
                DECISION_APPROVED, cand["id"], None, None,
                cand["max_sessions"] - cand["used_sessions"] - 1, None,
            )
        pool_max = cand["pool_max_sessions"]
        pool_used = cand["pool_used_sessions"]
        if pool_used < pool_max:
            return (
                DECISION_APPROVED, cand["id"], cand["pool_id"], None,
                cand["max_sessions"] - cand["used_sessions"] - 1,
                pool_max - pool_used - 1,
            )

    # every license that still has its own balance is blocked solely
    # because the pool it is bound to has run dry
    return DECISION_REJECTED, None, None, REASON_POOL_EXHAUSTED, None, None


class Store:
    """Thread-safe facade over the SQLite database.

    A fresh connection is opened per operation; the schema is created
    lazily on first use so importing the module has no side effects.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_lock = threading.Lock()
        self._ready = False

    def ensure(self) -> None:
        """Create the schema if it does not exist yet (idempotent)."""
        if self._ready:
            return
        with self._init_lock:
            if self._ready:
                return
            conn = connect(self.db_path)
            try:
                init_schema(conn)
            finally:
                conn.close()
            self._ready = True

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        self.ensure()
        conn = connect(self.db_path)
        try:
            yield conn
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # quota pools (film-level shared session cap across venues)
    # ------------------------------------------------------------------
    def create_pool(
        self,
        *,
        film_id: str,
        max_sessions: int,
        note: Optional[str] = None,
    ) -> dict:
        with self._connection() as conn:
            cur = conn.execute(
                """
                INSERT INTO quota_pools (film_id, max_sessions, note, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (film_id, max_sessions, note, now_iso()),
            )
            pool_id = cur.lastrowid
        return self.get_pool(pool_id)

    def get_pool(self, pool_id: int) -> dict:
        with self._connection() as conn:
            row = conn.execute(
                _POOL_SELECT + " WHERE p.id = ?", (pool_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"quota pool {pool_id} not found")
        return _pool_out(row)

    def list_pools(self, *, film_id: Optional[str] = None) -> list:
        sql, params = _POOL_SELECT, []
        if film_id is not None:
            sql += " WHERE p.film_id = ?"
            params.append(film_id)
        sql += " ORDER BY p.id ASC"
        with self._connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_pool_out(r) for r in rows]

    # ------------------------------------------------------------------
    # licenses
    # ------------------------------------------------------------------
    def create_license(
        self,
        *,
        film_id: str,
        venue_id: str,
        valid_from: datetime,
        valid_to: datetime,
        max_sessions: int,
        note: Optional[str] = None,
        pool_id: Optional[int] = None,
    ) -> dict:
        valid_from_iso, valid_to_iso = to_iso(valid_from), to_iso(valid_to)
        if valid_to_iso <= valid_from_iso:
            raise ValueError("valid_to must be later than valid_from")
        with self._connection() as conn:
            if pool_id is not None:
                pool = conn.execute(
                    "SELECT id, film_id FROM quota_pools WHERE id = ?",
                    (pool_id,),
                ).fetchone()
                if pool is None:
                    raise UnprocessableError(
                        f"quota pool {pool_id} does not exist",
                        code="POOL_NOT_FOUND",
                    )
                if pool["film_id"] != film_id:
                    raise UnprocessableError(
                        f"quota pool {pool_id} belongs to film "
                        f"'{pool['film_id']}', not '{film_id}'",
                        code="POOL_FILM_MISMATCH",
                    )
            cur = conn.execute(
                """
                INSERT INTO licenses
                    (film_id, venue_id, valid_from, valid_to, max_sessions,
                     pool_id, note, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (film_id, venue_id, valid_from_iso, valid_to_iso,
                 max_sessions, pool_id, note, now_iso()),
            )
            license_id = cur.lastrowid
        return self.get_license(license_id)

    def get_license(self, license_id: int) -> dict:
        with self._connection() as conn:
            row = conn.execute(
                _LICENSE_SELECT + " WHERE l.id = ?", (license_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"license {license_id} not found")
        return _license_out(row)

    def list_licenses(
        self,
        *,
        film_id: Optional[str] = None,
        venue_id: Optional[str] = None,
        pool_id: Optional[int] = None,
    ) -> list:
        sql, params = _LICENSE_SELECT, []
        clauses = []
        if film_id is not None:
            clauses.append("l.film_id = ?")
            params.append(film_id)
        if venue_id is not None:
            clauses.append("l.venue_id = ?")
            params.append(venue_id)
        if pool_id is not None:
            clauses.append("l.pool_id IS ?")
            params.append(pool_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY l.id ASC"
        with self._connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_license_out(r) for r in rows]

    def count_licenses(self) -> int:
        with self._connection() as conn:
            return conn.execute("SELECT COUNT(*) FROM licenses").fetchone()[0]

    def revoke_license(self, license_id: int, reason: str) -> dict:
        """Revoke a license.  Only future verifications are blocked;
        previously approved sessions (and their pool consumption) keep
        their records untouched and are not refunded."""
        with self._connection() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                lic = conn.execute(
                    "SELECT id FROM licenses WHERE id = ?", (license_id,)
                ).fetchone()
                if lic is None:
                    conn.execute("ROLLBACK")
                    raise NotFoundError(f"license {license_id} not found")
                existing = conn.execute(
                    "SELECT * FROM revocations WHERE license_id = ?", (license_id,)
                ).fetchone()
                if existing is not None:
                    conn.execute("ROLLBACK")
                    raise ConflictError(
                        f"license {license_id} is already revoked",
                        existing=dict(existing),
                    )
                revoked_at = now_iso()
                cur = conn.execute(
                    "INSERT INTO revocations (license_id, reason, revoked_at) VALUES (?, ?, ?)",
                    (license_id, reason, revoked_at),
                )
                conn.execute("COMMIT")
                return {
                    "id": cur.lastrowid,
                    "license_id": license_id,
                    "reason": reason,
                    "revoked_at": revoked_at,
                }
            except (NotFoundError, ConflictError):
                raise
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    # ------------------------------------------------------------------
    # time-limited suspensions
    # ------------------------------------------------------------------
    def create_suspension(
        self,
        license_id: int,
        *,
        suspend_from: datetime,
        suspend_to: datetime,
        reason: str,
    ) -> dict:
        """Register an immutable, half-open ``[from, to)`` suspension.

        Overlapping intervals are allowed and take effect as their union;
        unknown licenses and inverted windows are rejected without leaving
        a record.  Runs under ``BEGIN IMMEDIATE`` so a suspension insert
        serialises against concurrent verifications: each verification
        decision then corresponds either to the complete pre-registration
        state or to the complete post-registration state.
        """
        from_iso, end_iso = to_iso(suspend_from), to_iso(suspend_to)
        if end_iso <= from_iso:  # defensive; the request schema validates this too
            raise ValueError("suspend_to must be later than suspend_from")
        with self._connection() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                lic = conn.execute(
                    "SELECT id FROM licenses WHERE id = ?", (license_id,)
                ).fetchone()
                if lic is None:
                    conn.execute("ROLLBACK")
                    raise NotFoundError(f"license {license_id} not found")
                created_at = now_iso()
                cur = conn.execute(
                    """
                    INSERT INTO suspensions
                        (license_id, suspend_from, suspend_to, reason, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (license_id, from_iso, end_iso, reason, created_at),
                )
                suspension_id = cur.lastrowid
                conn.execute("COMMIT")
            except NotFoundError:
                raise
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
        return self.get_suspension(suspension_id)

    def get_suspension(self, suspension_id: int) -> dict:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM suspensions WHERE id = ?", (suspension_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"suspension {suspension_id} not found")
        return _suspension_out(row)

    def list_suspensions(self, *, license_id: Optional[int] = None) -> list:
        sql = "SELECT * FROM suspensions"
        params: list = []
        if license_id is not None:
            sql += " WHERE license_id = ?"
            params.append(license_id)
        sql += " ORDER BY id ASC"
        with self._connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_suspension_out(r) for r in rows]

    # ------------------------------------------------------------------
    # verifications
    # ------------------------------------------------------------------
    def verify(
        self,
        *,
        film_id: str,
        venue_id: str,
        start_time: datetime,
        idempotency_key: str,
    ) -> dict:
        """Verify a screening request idempotently.

        The whole read-check-write sequence runs inside a single
        ``BEGIN IMMEDIATE`` transaction, so concurrent requests are
        serialised by SQLite and neither license quotas nor pool caps
        can ever be exceeded.
        """
        start_iso = to_iso(start_time)
        request_hash = _hash_request(film_id, venue_id, start_iso)
        with self._connection() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")

                existing = conn.execute(
                    _VERIFICATION_SELECT + " WHERE v.idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if existing is not None:
                    conn.execute("COMMIT")
                    if existing["request_hash"] != request_hash:
                        raise ConflictError(
                            "idempotency key "
                            f"'{idempotency_key}' was already used with a "
                            "different request payload",
                            existing=_verification_out(existing, replay=True),
                        )
                    # 同键重放返回首次决定与首次余额快照（原样不变）；
                    # 若该流水此后被取消，仅附加取消状态，不重新评估、不重复释放。
                    return _verification_out(existing, replay=True)

                candidates = conn.execute(
                    _LICENSE_SELECT + """
                    WHERE l.film_id = ? AND l.venue_id = ?
                    ORDER BY l.valid_to ASC, l.id ASC
                    """,
                    (film_id, venue_id),
                ).fetchall()

                # Suspensions are read inside the same IMMEDIATE transaction
                # as the decision, so an insert concurrent with this verify
                # either commits before the transaction starts (fully visible)
                # or waits until it ends (fully absent).  Half-open union:
                # the slot is covered when suspend_from <= t < suspend_to.
                suspended_ids: set = set()
                if candidates:
                    placeholders = ",".join("?" for _ in candidates)
                    hit_rows = conn.execute(
                        f"""
                        SELECT DISTINCT license_id FROM suspensions
                        WHERE license_id IN ({placeholders})
                          AND suspend_from <= ? AND ? < suspend_to
                        """,
                        [c["id"] for c in candidates] + [start_iso, start_iso],
                    ).fetchall()
                    suspended_ids = {row["license_id"] for row in hit_rows}

                (
                    decision, license_id, pool_id, reason,
                    remaining, pool_remaining,
                ) = _decide(candidates, start_iso, suspended_ids)

                created_at = now_iso()
                cur = conn.execute(
                    """
                    INSERT INTO verifications
                        (idempotency_key, request_hash, film_id, venue_id, start_time,
                         decision, license_id, pool_id, reject_reason,
                         remaining_quota, pool_remaining_quota, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (idempotency_key, request_hash, film_id, venue_id, start_iso,
                     decision, license_id, pool_id, reason,
                     remaining, pool_remaining, created_at),
                )
                conn.execute("COMMIT")
                return {
                    "id": cur.lastrowid,
                    "idempotency_key": idempotency_key,
                    "film_id": film_id,
                    "venue_id": venue_id,
                    "start_time": start_iso,
                    "decision": decision,
                    "license_id": license_id,
                    "pool_id": pool_id,
                    "reject_reason": reason,
                    "remaining_quota": remaining,
                    "pool_remaining_quota": pool_remaining,
                    "verified_at": created_at,
                    "replay": False,
                }
            except ConflictError:
                raise
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    def get_verification(self, idempotency_key: str) -> dict:
        with self._connection() as conn:
            row = conn.execute(
                _VERIFICATION_SELECT + " WHERE v.idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"verification '{idempotency_key}' not found")
        return _verification_out(row, replay=False)

    def list_verifications(
        self,
        *,
        film_id: Optional[str] = None,
        venue_id: Optional[str] = None,
        decision: Optional[str] = None,
        license_id: Optional[int] = None,
        pool_id: Optional[int] = None,
        cancelled: Optional[bool] = None,
        limit: int = 200,
    ) -> list:
        sql = _VERIFICATION_SELECT
        clauses, params = [], []
        if film_id is not None:
            clauses.append("v.film_id = ?")
            params.append(film_id)
        if venue_id is not None:
            clauses.append("v.venue_id = ?")
            params.append(venue_id)
        if decision is not None:
            clauses.append("v.decision = ?")
            params.append(decision)
        if license_id is not None:
            clauses.append("v.license_id = ?")
            params.append(license_id)
        if pool_id is not None:
            clauses.append("v.pool_id = ?")
            params.append(pool_id)
        if cancelled is not None:
            clauses.append("c.id IS " + ("NOT NULL" if cancelled else "NULL"))
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY v.id DESC LIMIT ?"
        params.append(limit)
        with self._connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_verification_out(r, replay=False) for r in rows]

    # ------------------------------------------------------------------
    # cancellations of approved screenings
    # ------------------------------------------------------------------
    def cancel_verification(self, idempotency_key: str, reason: str) -> dict:
        """Cancel a previously approved screening before its start time.

        The whole lookup-check-insert sequence runs inside one
        ``BEGIN IMMEDIATE`` transaction, so it serialises against both
        new verifications and other cancellations: the released quota can
        be consumed at most once and the cancellation itself can be
        recorded at most once (the unique key rejects double releases).

        The original verification row is never modified — its decision and
        deduction-time balance snapshots stay as they were; only a new
        ``cancellations`` row is inserted, which the live usage counters
        exclude.  Re-submitting the same key returns the first record
        (idempotent replay); a re-submission with a different reason is a
        409 conflict.
        """
        now = now_iso()  # one canonical "now": start-check and record agree
        with self._connection() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")

                verification = conn.execute(
                    "SELECT * FROM verifications WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if verification is None:
                    conn.execute("ROLLBACK")
                    raise NotFoundError(
                        f"verification '{idempotency_key}' not found"
                    )

                existing_cancel = conn.execute(
                    "SELECT * FROM cancellations WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if existing_cancel is not None:
                    conn.execute("ROLLBACK")
                    if existing_cancel["reason"] != reason:
                        raise ConflictError(
                            f"verification '{idempotency_key}' was already "
                            "cancelled with a different reason",
                            existing=_cancellation_out(existing_cancel),
                        )
                    # same key + same reason -> first record, quota released once
                    return _cancellation_out(existing_cancel)

                # a rejected verification never consumed any quota to release
                if verification["decision"] != DECISION_APPROVED:
                    conn.execute("ROLLBACK")
                    raise UnprocessableError(
                        f"verification '{idempotency_key}' is "
                        f"{verification['decision']} and cannot be cancelled",
                        code="VERIFICATION_NOT_APPROVED",
                    )

                # only screenings that have not started may be cancelled
                if now >= verification["start_time"]:
                    conn.execute("ROLLBACK")
                    raise UnprocessableError(
                        f"screening '{idempotency_key}' has already started "
                        f"at {verification['start_time']} and cannot be cancelled",
                        code="SCREENING_ALREADY_STARTED",
                    )

                cur = conn.execute(
                    """
                    INSERT INTO cancellations
                        (idempotency_key, verification_id, license_id,
                         pool_id, reason, cancelled_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        idempotency_key,
                        verification["id"],
                        verification["license_id"],
                        verification["pool_id"],
                        reason,
                        now,
                    ),
                )
                cancellation_id = cur.lastrowid
                conn.execute("COMMIT")
            except (NotFoundError, ConflictError, UnprocessableError):
                raise
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
        return self.get_cancellation(cancellation_id)

    def get_cancellation(self, cancellation_id: int) -> dict:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM cancellations WHERE id = ?", (cancellation_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"cancellation {cancellation_id} not found")
        return _cancellation_out(row)

    def get_cancellation_by_key(self, idempotency_key: str) -> dict:
        """Return the cancellation registered for a verification key.

        404 both when the verification is unknown and when it was never
        cancelled.
        """
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM cancellations WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        if row is None:
            raise NotFoundError(
                f"cancellation for verification '{idempotency_key}' not found"
            )
        return _cancellation_out(row)

    def list_cancellations(
        self,
        *,
        license_id: Optional[int] = None,
        pool_id: Optional[int] = None,
        film_id: Optional[str] = None,
        venue_id: Optional[str] = None,
        limit: int = 200,
    ) -> list:
        sql = (
            "SELECT c.* FROM cancellations c "
            "JOIN verifications v ON v.id = c.verification_id"
        )
        clauses, params = [], []
        if license_id is not None:
            clauses.append("c.license_id = ?")
            params.append(license_id)
        if pool_id is not None:
            clauses.append("c.pool_id = ?")
            params.append(pool_id)
        if film_id is not None:
            clauses.append("v.film_id = ?")
            params.append(film_id)
        if venue_id is not None:
            clauses.append("v.venue_id = ?")
            params.append(venue_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY c.id DESC LIMIT ?"
        params.append(limit)
        with self._connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_cancellation_out(r) for r in rows]
