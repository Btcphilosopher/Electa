"""
Electa Systems — Extended Test Suite
Covers: admin router, proposal auto-close scheduler,
rate limiter, ownership validation, and participation reports.
"""

import time
import pytest
import pytest_asyncio


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _u(client, name, role="shareholder"):
    r = await client.post("/users", json={"name": name, "role": role})
    assert r.status_code == 201
    return r.json()


async def _e(client, name, shares=1_000_000):
    import random, string
    ticker = "".join(random.choices(string.ascii_uppercase, k=5))
    r = await client.post("/entities",
                          json={"name": name, "ticker": ticker,
                                "total_shares": shares})
    assert r.status_code == 201
    return r.json()


async def _own(client, uid, eid, shares, mult=1.0):
    r = await client.put("/entities/ownership",
                         json={"user_id": uid, "entity_id": eid,
                               "shares": shares, "role_weight_multiplier": mult})
    assert r.status_code == 200


async def _prop(client, eid, uid, pid, quorum=0.40, closes_at=None):
    body = {"id": pid, "entity_id": eid, "title": f"Prop {pid}",
            "quorum_pct": quorum}
    if closes_at:
        body["closes_at"] = closes_at
    r = await client.post("/proposals", params={"creator_id": uid}, json=body)
    assert r.status_code == 201
    return r.json()


async def _vote(client, pid, uid, choice="YES"):
    r = await client.post("/votes",
                          json={"proposal_id": pid, "voter_id": uid,
                                "action_type": "vote", "choice": choice})
    assert r.status_code == 201
    return r.json()


# ── Admin stats ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_admin_stats_shape(client):
    r = await client.get("/admin/stats")
    assert r.status_code == 200
    data = r.json()
    for key in ["entities", "users", "proposals", "votes_cast",
                "total_weight_cast", "audit_log_entries",
                "active_webhooks", "event_bus_subscribers"]:
        assert key in data, f"Missing key: {key}"
    assert isinstance(data["proposals"], dict)
    assert "open" in data["proposals"]


# ── Admin audit search ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_admin_audit_returns_list(client):
    r = await client.get("/admin/audit")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


@pytest.mark.asyncio
async def test_admin_audit_filtered_by_proposal(client):
    u = await _u(client, "AuditSearch")
    e = await _e(client, "AuditSearchCorp")
    await _own(client, u["id"], e["id"], 300_000)
    await _prop(client, e["id"], u["id"], "P-AS-1")
    await _vote(client, "P-AS-1", u["id"], "YES")

    r = await client.get("/admin/audit",
                         params={"proposal_id": "P-AS-1"})
    assert r.status_code == 200
    assert any(entry["action"] == "vote.cast" for entry in r.json())


@pytest.mark.asyncio
async def test_admin_audit_time_bounds(client):
    now = int(time.time())
    r = await client.get("/admin/audit",
                         params={"since": now - 3600, "until": now + 3600})
    assert r.status_code == 200
    assert isinstance(r.json(), list)


# ── Ownership validation ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ownership_within_cap_valid(client):
    u1 = await _u(client, "OV1")
    u2 = await _u(client, "OV2")
    e  = await _e(client, "OVValidCorp", shares=1_000_000)
    await _own(client, u1["id"], e["id"], 400_000)
    await _own(client, u2["id"], e["id"], 200_000)

    r = await client.get(f"/admin/entities/{e['id']}/ownership/validate")
    assert r.status_code == 200
    data = r.json()
    assert data["valid"]          is True
    assert data["total_assigned"] == pytest.approx(600_000)
    assert data["unassigned"]     == pytest.approx(400_000)
    assert data["pct_assigned"]   == pytest.approx(60.0)
    assert data["over_cap"]       is False


@pytest.mark.asyncio
async def test_ownership_over_cap_flagged(client):
    u1 = await _u(client, "OE1")
    u2 = await _u(client, "OE2")
    e  = await _e(client, "OExceedCorp", shares=500_000)
    await _own(client, u1["id"], e["id"], 350_000)
    await _own(client, u2["id"], e["id"], 350_000)  # total = 700k > 500k

    r = await client.get(f"/admin/entities/{e['id']}/ownership/validate")
    data = r.json()
    assert data["valid"]    is False
    assert data["over_cap"] is True


# ── Participation report ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_participation_report_basic(client):
    u1, u2, u3 = [await _u(client, f"PR{i}") for i in range(1, 4)]
    e = await _e(client, "PRCorp")
    await _own(client, u1["id"], e["id"], 500_000)
    await _own(client, u2["id"], e["id"], 300_000)
    await _own(client, u3["id"], e["id"], 200_000)

    await _prop(client, e["id"], u1["id"], "P-PR-1", quorum=0.50)
    await _vote(client, "P-PR-1", u1["id"], "YES")
    await _vote(client, "P-PR-1", u2["id"], "NO")

    r = await client.get("/admin/proposals/P-PR-1/participation")
    assert r.status_code == 200
    data = r.json()
    assert data["eligible_count"]    == 3
    assert data["voter_count"]       == 2
    assert data["participation_pct"] == pytest.approx(80.0)

    non_voters = [p for p in data["participants"] if not p["voted"]]
    assert len(non_voters) == 1
    assert non_voters[0]["user_name"] == f"PR3"


@pytest.mark.asyncio
async def test_participation_report_weights_correct(client):
    board = await _u(client, "PRBoard", role="board")
    sh    = await _u(client, "PRShareholder")
    e     = await _e(client, "PRWeightCorp")
    await _own(client, board["id"], e["id"], 100_000, mult=1.5)  # 150k effective
    await _own(client, sh["id"],    e["id"], 200_000, mult=1.0)  # 200k effective

    await _prop(client, e["id"], board["id"], "P-PRW-1")
    await _vote(client, "P-PRW-1", board["id"], "YES")

    r = await client.get("/admin/proposals/P-PRW-1/participation")
    data = r.json()
    board_entry = next(p for p in data["participants"]
                       if p["user_name"] == "PRBoard")
    assert board_entry["effective_weight"] == pytest.approx(150_000)
    assert board_entry["voted"]  is True
    assert board_entry["choice"] == "YES"


# ── Overdue proposals ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_overdue_list_includes_past_close(client):
    u = await _u(client, "OD1")
    e = await _e(client, "ODCorp")
    await _own(client, u["id"], e["id"], 100_000)
    past = int(time.time()) - 3600
    await _prop(client, e["id"], u["id"], "P-OD-1", closes_at=past)

    r = await client.get("/admin/proposals/overdue")
    assert r.status_code == 200
    ids = [p["id"] for p in r.json()["proposals"]]
    assert "P-OD-1" in ids


@pytest.mark.asyncio
async def test_overdue_list_excludes_future_close(client):
    u = await _u(client, "NOD1")
    e = await _e(client, "NotODCorp")
    await _own(client, u["id"], e["id"], 100_000)
    future = int(time.time()) + 3600
    await _prop(client, e["id"], u["id"], "P-NOD-1", closes_at=future)

    r = await client.get("/admin/proposals/overdue")
    ids = [p["id"] for p in r.json()["proposals"]]
    assert "P-NOD-1" not in ids


@pytest.mark.asyncio
async def test_overdue_includes_overdue_by_seconds(client):
    u = await _u(client, "ODS1")
    e = await _e(client, "ODSCorp")
    await _own(client, u["id"], e["id"], 100_000)
    past = int(time.time()) - 120
    await _prop(client, e["id"], u["id"], "P-ODS-1", closes_at=past)

    r = await client.get("/admin/proposals/overdue")
    proposal = next(p for p in r.json()["proposals"] if p["id"] == "P-ODS-1")
    assert proposal["overdue_by_seconds"] >= 120


# ── Scheduler ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_scheduler_closes_expired_proposal(client):
    """Create with future close, vote, backdate, run scheduler → closed."""
    u = await _u(client, "SC1")
    e = await _e(client, "SCCorp")
    await _own(client, u["id"], e["id"], 500_000)

    future = int(time.time()) + 3600
    await _prop(client, e["id"], u["id"], "P-SC-1", quorum=0.20, closes_at=future)
    await _vote(client, "P-SC-1", u["id"], "YES")

    # Backdate the closes_at directly in the DB
    from tests.conftest import _Session
    from models.db_models import Proposal
    async with _Session() as db:
        p = await db.get(Proposal, "P-SC-1")
        p.closes_at = int(time.time()) - 60
        await db.commit()

    r = await client.post("/admin/scheduler/run")
    assert r.status_code == 200

    p_resp = (await client.get("/proposals/P-SC-1")).json()
    assert p_resp["status"]           == "closed"
    assert p_resp["result"]["passed"] is True


@pytest.mark.asyncio
async def test_scheduler_ignores_future_proposals(client):
    u = await _u(client, "SC2")
    e = await _e(client, "SCFutureCorp")
    await _own(client, u["id"], e["id"], 300_000)
    future = int(time.time()) + 3600
    await _prop(client, e["id"], u["id"], "P-SCF-1", closes_at=future)

    await client.post("/admin/scheduler/run")
    p = (await client.get("/proposals/P-SCF-1")).json()
    assert p["status"] == "open"


@pytest.mark.asyncio
async def test_scheduler_ignores_already_closed(client):
    """Scheduler must not error if proposal is already closed."""
    u = await _u(client, "SC3")
    e = await _e(client, "SCDoneCorp")
    await _own(client, u["id"], e["id"], 200_000)

    future = int(time.time()) + 3600
    await _prop(client, e["id"], u["id"], "P-SCA-1", closes_at=future)
    await _vote(client, "P-SCA-1", u["id"], "YES")
    await client.post("/proposals/P-SCA-1/close")  # manual close

    # Backdate so scheduler would pick it up if not already closed
    from tests.conftest import _Session
    from models.db_models import Proposal
    async with _Session() as db:
        p = await db.get(Proposal, "P-SCA-1")
        p.closes_at = int(time.time()) - 60
        await db.commit()

    r = await client.post("/admin/scheduler/run")
    assert r.status_code == 200
    # Should still be closed, not errored
    p_resp = (await client.get("/proposals/P-SCA-1")).json()
    assert p_resp["status"] == "closed"


# ── Rate limiter unit tests ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rate_limiter_allows_within_limit():
    from middleware.rate_limiter import SlidingWindowRateLimiter
    lim = SlidingWindowRateLimiter(max_requests=5, window_seconds=10.0)
    for i in range(5):
        allowed, remaining = await lim.is_allowed("ip-ok")
        assert allowed  is True
        assert remaining == 4 - i


@pytest.mark.asyncio
async def test_rate_limiter_blocks_on_breach():
    from middleware.rate_limiter import SlidingWindowRateLimiter
    lim = SlidingWindowRateLimiter(max_requests=3, window_seconds=60.0)
    for _ in range(3):
        await lim.is_allowed("ip-breach")
    allowed, remaining = await lim.is_allowed("ip-breach")
    assert allowed   is False
    assert remaining == 0


@pytest.mark.asyncio
async def test_rate_limiter_ips_are_independent():
    from middleware.rate_limiter import SlidingWindowRateLimiter
    lim = SlidingWindowRateLimiter(max_requests=2, window_seconds=60.0)
    await lim.is_allowed("ip-A")
    await lim.is_allowed("ip-A")
    blocked, _ = await lim.is_allowed("ip-A")
    assert blocked is False

    allowed, remaining = await lim.is_allowed("ip-B")
    assert allowed   is True
    assert remaining == 1


@pytest.mark.asyncio
async def test_rate_limiter_window_expires():
    """Requests outside the window should be evicted, allowing new ones."""
    import asyncio
    from middleware.rate_limiter import SlidingWindowRateLimiter
    lim = SlidingWindowRateLimiter(max_requests=2, window_seconds=0.1)
    await lim.is_allowed("ip-exp")
    await lim.is_allowed("ip-exp")
    blocked, _ = await lim.is_allowed("ip-exp")
    assert blocked is False

    await asyncio.sleep(0.15)  # let the window expire
    allowed, _ = await lim.is_allowed("ip-exp")
    assert allowed is True


# ── Admin events ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_admin_recent_events(client):
    r = await client.get("/admin/events/recent", params={"limit": 20})
    assert r.status_code == 200
    data = r.json()
    assert "events"           in data
    assert "subscriber_count" in data
    assert "count"            in data
    assert isinstance(data["events"], list)


@pytest.mark.asyncio
async def test_admin_recent_events_filter(client):
    # Cast a vote to generate an event, then filter
    u = await _u(client, "EvFilt")
    e = await _e(client, "EvFiltCorp")
    await _own(client, u["id"], e["id"], 100_000)
    await _prop(client, e["id"], u["id"], "P-EVFILT-1")
    await _vote(client, "P-EVFILT-1", u["id"], "YES")

    r = await client.get("/admin/events/recent",
                         params={"filter": "governance.vote", "limit": 50})
    assert r.status_code == 200
    for ev in r.json()["events"]:
        assert ev["event"].startswith("governance.vote")
