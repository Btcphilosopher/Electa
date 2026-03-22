"""
Electa Systems — Core Governance Test Suite
Covers: proposal lifecycle, vote casting, weighted tallying,
delegation, audit trail, webhooks, and event streaming.
"""

import pytest
import pytest_asyncio


# ── Test helpers ──────────────────────────────────────────────────────────────

async def make_user(client, name, role="shareholder"):
    r = await client.post("/users", json={"name": name, "role": role})
    assert r.status_code == 201, r.text
    return r.json()


async def make_entity(client, name, shares=1_000_000):
    import random, string
    ticker = "".join(random.choices(string.ascii_uppercase, k=5))
    r = await client.post("/entities",
                          json={"name": name, "ticker": ticker, "total_shares": shares})
    assert r.status_code == 201, r.text
    return r.json()


async def set_ownership(client, uid, eid, shares, mult=1.0):
    r = await client.put("/entities/ownership",
                         json={"user_id": uid, "entity_id": eid,
                               "shares": shares, "role_weight_multiplier": mult})
    assert r.status_code == 200, r.text
    return r.json()


async def make_proposal(client, eid, uid, pid,
                        threshold="simple_majority", quorum=0.40,
                        closes_at=None):
    body = {"id": pid, "entity_id": eid, "title": f"Proposal {pid}",
            "threshold_type": threshold, "quorum_pct": quorum}
    if closes_at is not None:
        body["closes_at"] = closes_at
    r = await client.post("/proposals", params={"creator_id": uid}, json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def cast_vote(client, pid, uid, choice="YES", action="vote"):
    r = await client.post("/votes",
                          json={"proposal_id": pid, "voter_id": uid,
                                "action_type": action, "choice": choice})
    assert r.status_code == 201, r.text
    return r.json()


# ── System ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_health(client):
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "healthy"


@pytest.mark.asyncio
async def test_root(client):
    r = await client.get("/")
    assert r.status_code == 200
    assert "Electa Systems" in r.json()["system"]


# ── Users ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_user_returns_api_key(client):
    r = await client.post("/users", json={"name": "Alice", "role": "shareholder"})
    assert r.status_code == 201
    data = r.json()
    assert data["name"] == "Alice"
    assert data["role"] == "shareholder"
    assert "api_key" in data
    assert data["is_active"] is True


@pytest.mark.asyncio
async def test_create_user_invalid_role(client):
    r = await client.post("/users", json={"name": "Bad", "role": "wizard"})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_list_users(client):
    await make_user(client, "ListUser1")
    await make_user(client, "ListUser2")
    r = await client.get("/users")
    assert r.status_code == 200
    assert len(r.json()) >= 2


@pytest.mark.asyncio
async def test_get_user(client):
    u = await make_user(client, "GetMe")
    r = await client.get(f"/users/{u['id']}")
    assert r.status_code == 200
    assert r.json()["name"] == "GetMe"


@pytest.mark.asyncio
async def test_deactivate_user(client):
    u = await make_user(client, "DeleteMe")
    r = await client.delete(f"/users/{u['id']}")
    assert r.status_code == 200
    r2 = await client.get(f"/users/{u['id']}")
    assert r2.status_code == 404


# ── Entities ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_entity(client):
    r = await client.post("/entities",
                          json={"name": "MegaCorp", "ticker": "MEGAX",
                                "total_shares": 500_000})
    assert r.status_code == 201
    assert r.json()["ticker"] == "MEGAX"
    assert r.json()["total_shares"] == 500_000


@pytest.mark.asyncio
async def test_duplicate_ticker_rejected(client):
    await client.post("/entities",
                      json={"name": "DupA", "ticker": "DUPAA", "total_shares": 1000})
    r = await client.post("/entities",
                          json={"name": "DupB", "ticker": "DUPAA", "total_shares": 1000})
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_upsert_ownership(client):
    u = await make_user(client, "OwnUser")
    e = await make_entity(client, "OwnCorp")
    r = await set_ownership(client, u["id"], e["id"], 250_000, mult=1.0)
    assert r["shares"] == 250_000

    # Update
    r2 = await set_ownership(client, u["id"], e["id"], 300_000, mult=1.0)
    assert r2["shares"] == 300_000


# ── Full lifecycle ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_full_lifecycle_simple_majority(client):
    """YES=600k, NO=400k → 60% > 50% → PASSES with quorum met."""
    ua = await make_user(client, "LC_A")
    ub = await make_user(client, "LC_B")
    e  = await make_entity(client, "LifecycleCorp")
    await set_ownership(client, ua["id"], e["id"], 600_000)
    await set_ownership(client, ub["id"], e["id"], 400_000)

    p = await make_proposal(client, e["id"], ua["id"], "P-LC-1", quorum=0.50)
    assert p["status"] == "open"

    va = await cast_vote(client, "P-LC-1", ua["id"], "YES")
    assert va["weight"] == pytest.approx(600_000)
    await cast_vote(client, "P-LC-1", ub["id"], "NO")

    result = (await client.post("/proposals/P-LC-1/close")).json()
    assert result["quorum_met"]  is True
    assert result["passed"]      is True
    assert result["yes_weight"]  == pytest.approx(600_000)
    assert result["no_weight"]   == pytest.approx(400_000)


# ── Quorum ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_quorum_not_met_blocks_pass(client):
    """Only 10% participates; quorum=51% → FAILS even with 100% YES."""
    u     = await make_user(client, "QU_Voter")
    other = await make_user(client, "QU_Silent")
    e     = await make_entity(client, "QuorumCorp")
    await set_ownership(client, u["id"],     e["id"], 100_000)
    await set_ownership(client, other["id"], e["id"], 900_000)

    await make_proposal(client, e["id"], u["id"], "P-QU-1", quorum=0.51)
    await cast_vote(client, "P-QU-1", u["id"], "YES")

    result = (await client.post("/proposals/P-QU-1/close")).json()
    assert result["quorum_met"] is False
    assert result["passed"]     is False


# ── Thresholds ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_supermajority_passes(client):
    """750k YES / 1000k total = 75% ≥ 66.67% → PASSES."""
    u1, u2, u3 = [await make_user(client, f"SM{i}") for i in range(1, 4)]
    e = await make_entity(client, "SuperCorp")
    await set_ownership(client, u1["id"], e["id"], 400_000)
    await set_ownership(client, u2["id"], e["id"], 350_000)
    await set_ownership(client, u3["id"], e["id"], 250_000)

    await make_proposal(client, e["id"], u1["id"], "P-SM-1",
                        threshold="supermajority", quorum=0.80)
    await cast_vote(client, "P-SM-1", u1["id"], "YES")
    await cast_vote(client, "P-SM-1", u2["id"], "YES")
    await cast_vote(client, "P-SM-1", u3["id"], "NO")

    result = (await client.post("/proposals/P-SM-1/close")).json()
    assert result["quorum_met"] is True
    assert result["passed"]     is True


@pytest.mark.asyncio
async def test_supermajority_fails_below_threshold(client):
    """50% YES / 50% NO < 66.67% → FAILS."""
    u1 = await make_user(client, "SMF1")
    u2 = await make_user(client, "SMF2")
    e  = await make_entity(client, "SMFailCorp")
    await set_ownership(client, u1["id"], e["id"], 500_000)
    await set_ownership(client, u2["id"], e["id"], 500_000)

    await make_proposal(client, e["id"], u1["id"], "P-SMF-1",
                        threshold="supermajority", quorum=0.50)
    await cast_vote(client, "P-SMF-1", u1["id"], "YES")
    await cast_vote(client, "P-SMF-1", u2["id"], "NO")

    result = (await client.post("/proposals/P-SMF-1/close")).json()
    assert result["passed"] is False


@pytest.mark.asyncio
async def test_abstain_excluded_from_threshold(client):
    """YES=400k NO=100k ABSTAIN=500k → YES/(YES+NO)=80% → PASSES."""
    u1, u2, u3 = [await make_user(client, f"AB{i}") for i in range(1, 4)]
    e = await make_entity(client, "AbstainCorp")
    await set_ownership(client, u1["id"], e["id"], 400_000)
    await set_ownership(client, u2["id"], e["id"], 100_000)
    await set_ownership(client, u3["id"], e["id"], 500_000)

    await make_proposal(client, e["id"], u1["id"], "P-AB-1", quorum=0.90)
    await cast_vote(client, "P-AB-1", u1["id"], "YES")
    await cast_vote(client, "P-AB-1", u2["id"], "NO")
    await cast_vote(client, "P-AB-1", u3["id"], "ABSTAIN")

    result = (await client.post("/proposals/P-AB-1/close")).json()
    assert result["abstain_weight"] == pytest.approx(500_000)
    assert result["quorum_met"]     is True
    assert result["passed"]         is True


@pytest.mark.asyncio
async def test_unanimous_passes(client):
    u1 = await make_user(client, "UN1")
    u2 = await make_user(client, "UN2")
    e  = await make_entity(client, "UniCorp")
    await set_ownership(client, u1["id"], e["id"], 500_000)
    await set_ownership(client, u2["id"], e["id"], 500_000)

    await make_proposal(client, e["id"], u1["id"], "P-UN-1",
                        threshold="unanimous", quorum=0.50)
    await cast_vote(client, "P-UN-1", u1["id"], "YES")
    await cast_vote(client, "P-UN-1", u2["id"], "YES")

    result = (await client.post("/proposals/P-UN-1/close")).json()
    assert result["passed"] is True


@pytest.mark.asyncio
async def test_unanimous_fails_with_one_no(client):
    u1 = await make_user(client, "UNF1")
    u2 = await make_user(client, "UNF2")
    e  = await make_entity(client, "UniFail")
    await set_ownership(client, u1["id"], e["id"], 500_000)
    await set_ownership(client, u2["id"], e["id"], 500_000)

    await make_proposal(client, e["id"], u1["id"], "P-UNF-1",
                        threshold="unanimous", quorum=0.50)
    await cast_vote(client, "P-UNF-1", u1["id"], "YES")
    await cast_vote(client, "P-UNF-1", u2["id"], "NO")

    result = (await client.post("/proposals/P-UNF-1/close")).json()
    assert result["passed"] is False


# ── Error cases ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_duplicate_vote_rejected(client):
    u = await make_user(client, "DV1")
    e = await make_entity(client, "DVCorp")
    await set_ownership(client, u["id"], e["id"], 100_000)
    await make_proposal(client, e["id"], u["id"], "P-DV-1")
    await cast_vote(client, "P-DV-1", u["id"], "YES")

    r = await client.post("/votes",
                          json={"proposal_id": "P-DV-1", "voter_id": u["id"],
                                "action_type": "vote", "choice": "NO"})
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_vote_on_closed_proposal_rejected(client):
    u = await make_user(client, "LV1")
    e = await make_entity(client, "ClosedCorp")
    await set_ownership(client, u["id"], e["id"], 100_000)
    await make_proposal(client, e["id"], u["id"], "P-CLO-1")
    await client.post("/proposals/P-CLO-1/close")

    r = await client.post("/votes",
                          json={"proposal_id": "P-CLO-1", "voter_id": u["id"],
                                "action_type": "vote", "choice": "YES"})
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_vote_without_ownership_rejected(client):
    u = await make_user(client, "NS1")
    e = await make_entity(client, "NoSharesCorp")
    await make_proposal(client, e["id"], u["id"], "P-NS-1")

    r = await client.post("/votes",
                          json={"proposal_id": "P-NS-1", "voter_id": u["id"],
                                "action_type": "vote", "choice": "YES"})
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_double_close_rejected(client):
    u = await make_user(client, "DC1")
    e = await make_entity(client, "DblCloseCorp")
    await set_ownership(client, u["id"], e["id"], 100_000)
    await make_proposal(client, e["id"], u["id"], "P-DC-1")
    await client.post("/proposals/P-DC-1/close")
    r = await client.post("/proposals/P-DC-1/close")
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_result_unavailable_on_open_proposal(client):
    u = await make_user(client, "RO1")
    e = await make_entity(client, "ResultOpenCorp")
    await set_ownership(client, u["id"], e["id"], 100_000)
    await make_proposal(client, e["id"], u["id"], "P-RO-1")
    r = await client.get("/proposals/P-RO-1/result")
    assert r.status_code == 400


# ── Weighting ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_board_role_weight_multiplier(client):
    """100k shares × 1.5× multiplier = 150k effective weight."""
    board = await make_user(client, "BoardM", role="board")
    e     = await make_entity(client, "BoardCorp")
    await set_ownership(client, board["id"], e["id"], 100_000, mult=1.5)
    await make_proposal(client, e["id"], board["id"], "P-BM-1")

    v = await cast_vote(client, "P-BM-1", board["id"], "YES")
    assert v["weight"] == pytest.approx(150_000)


# ── Delegation ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_vote_delegation_boosts_weight(client):
    """Fund_C (50k) delegates to Fund_A (400k) → Fund_A casts 450k."""
    fa = await make_user(client, "DA_FundA")
    fc = await make_user(client, "DA_FundC")
    e  = await make_entity(client, "DelegCorp")
    await set_ownership(client, fa["id"], e["id"], 400_000)
    await set_ownership(client, fc["id"], e["id"],  50_000)

    await make_proposal(client, e["id"], fa["id"], "P-DA-1")

    deleg = await client.post("/votes",
                              json={"proposal_id": "P-DA-1",
                                    "voter_id": fc["id"],
                                    "action_type": "delegate",
                                    "delegate_to_id": fa["id"]})
    assert deleg.status_code == 201
    assert deleg.json()["action_type"] == "delegate"

    v = await cast_vote(client, "P-DA-1", fa["id"], "YES")
    assert v["weight"] == pytest.approx(450_000)


@pytest.mark.asyncio
async def test_delegation_to_self_rejected(client):
    u = await make_user(client, "SelfD")
    e = await make_entity(client, "SelfDCorp")
    await set_ownership(client, u["id"], e["id"], 100_000)
    await make_proposal(client, e["id"], u["id"], "P-SD-1")

    r = await client.post("/votes",
                          json={"proposal_id": "P-SD-1",
                                "voter_id": u["id"],
                                "action_type": "delegate",
                                "delegate_to_id": u["id"]})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_delegation_boosts_existing_vote(client):
    """Fund_A votes first; Fund_C delegates after → Fund_A's weight updated."""
    fa = await make_user(client, "DAE_FundA")
    fc = await make_user(client, "DAE_FundC")
    e  = await make_entity(client, "DelegExistCorp")
    await set_ownership(client, fa["id"], e["id"], 300_000)
    await set_ownership(client, fc["id"], e["id"],  50_000)

    await make_proposal(client, e["id"], fa["id"], "P-DAE-1")

    # Fund_A votes first
    v = await cast_vote(client, "P-DAE-1", fa["id"], "YES")
    assert v["weight"] == pytest.approx(300_000)

    # Fund_C delegates after
    d = await client.post("/votes",
                          json={"proposal_id": "P-DAE-1",
                                "voter_id": fc["id"],
                                "action_type": "delegate",
                                "delegate_to_id": fa["id"]})
    assert d.status_code == 201

    # Fund_A's vote weight should have been updated
    votes = (await client.get("/votes/P-DAE-1")).json()
    fa_vote = next(v for v in votes if v["voter_id"] == fa["id"]
                   and v["action_type"] == "vote")
    assert fa_vote["weight"] == pytest.approx(350_000)


# ── approve / reject sugar ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_approve_maps_to_yes(client):
    u = await make_user(client, "ApprU")
    e = await make_entity(client, "ApprCorp")
    await set_ownership(client, u["id"], e["id"], 200_000)
    await make_proposal(client, e["id"], u["id"], "P-APPR-1")

    r = await client.post("/votes",
                          json={"proposal_id": "P-APPR-1",
                                "voter_id": u["id"],
                                "action_type": "approve",
                                "choice": "YES"})
    assert r.status_code == 201
    assert r.json()["choice"] == "YES"


@pytest.mark.asyncio
async def test_reject_maps_to_no(client):
    u = await make_user(client, "RejU")
    e = await make_entity(client, "RejCorp")
    await set_ownership(client, u["id"], e["id"], 200_000)
    await make_proposal(client, e["id"], u["id"], "P-REJ-1")

    r = await client.post("/votes",
                          json={"proposal_id": "P-REJ-1",
                                "voter_id": u["id"],
                                "action_type": "reject",
                                "choice": "NO"})
    assert r.status_code == 201
    assert r.json()["choice"] == "NO"


# ── Lists & audit ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_votes_for_proposal(client):
    u1 = await make_user(client, "LV_1")
    u2 = await make_user(client, "LV_2")
    e  = await make_entity(client, "LVCorp")
    await set_ownership(client, u1["id"], e["id"], 300_000)
    await set_ownership(client, u2["id"], e["id"], 300_000)
    await make_proposal(client, e["id"], u1["id"], "P-LV-1")
    await cast_vote(client, "P-LV-1", u1["id"], "YES")
    await cast_vote(client, "P-LV-1", u2["id"], "NO")

    r = await client.get("/votes/P-LV-1")
    assert r.status_code == 200
    assert len(r.json()) == 2


@pytest.mark.asyncio
async def test_audit_trail_populated(client):
    u = await make_user(client, "AuditU")
    e = await make_entity(client, "AuditCorp")
    await set_ownership(client, u["id"], e["id"], 200_000)
    await make_proposal(client, e["id"], u["id"], "P-AUDIT-1")
    await cast_vote(client, "P-AUDIT-1", u["id"], "YES")
    await client.post("/proposals/P-AUDIT-1/close")

    audit = (await client.get("/votes/P-AUDIT-1/audit")).json()
    actions = {e["action"] for e in audit}
    assert "vote.cast"       in actions
    assert "result.computed" in actions


# ── Events ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_recent_events_endpoint(client):
    r = await client.get("/events/recent", params={"limit": 20})
    assert r.status_code == 200
    data = r.json()
    assert "events" in data
    assert isinstance(data["events"], list)


# ── Webhooks ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_register_webhook(client):
    owner = await make_user(client, "WHOwner")
    r = await client.post("/webhooks",
                          json={"owner_id": owner["id"],
                                "url": "https://example.com/hook",
                                "event_filter": "governance.vote.*"})
    assert r.status_code == 201
    assert r.json()["is_active"] is True
    assert r.json()["event_filter"] == "governance.vote.*"


@pytest.mark.asyncio
async def test_list_webhooks(client):
    owner = await make_user(client, "WHList")
    await client.post("/webhooks",
                      json={"owner_id": owner["id"],
                            "url": "https://example.com/wh1"})
    r = await client.get("/webhooks", params={"owner_id": owner["id"]})
    assert r.status_code == 200
    assert len(r.json()) >= 1


@pytest.mark.asyncio
async def test_deactivate_webhook(client):
    owner = await make_user(client, "WHDeact")
    wh = (await client.post("/webhooks",
                            json={"owner_id": owner["id"],
                                  "url": "https://example.com/deact"})).json()
    assert (await client.delete(f"/webhooks/{wh['id']}")).status_code == 200
    assert (await client.get(f"/webhooks/{wh['id']}")).json()["is_active"] is False
