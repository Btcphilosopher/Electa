#!/usr/bin/env python3
"""
Electa Systems — Example Usage
================================
Demonstrates the full governance lifecycle against a running instance:

  1. Register institutional voters and a board member
  2. Register a company entity (Meridian Technologies plc)
  3. Assign share ownership
  4. Create a merger resolution
  5. Cast votes (YES / NO / ABSTAIN)
  6. Delegate a vote
  7. Close the proposal and retrieve results
  8. Query participation report and audit trail
  9. Register a webhook endpoint

Requirements:  pip install httpx
Run:           python example_usage.py   (needs Electa running on localhost:8000)
"""

import asyncio
import json
import time

import httpx

BASE = "http://localhost:8000"
H    = {"Content-Type": "application/json"}


def pp(label: str, data: dict):
    print(f"\n{'─' * 62}")
    print(f"  {label}")
    print(f"{'─' * 62}")
    print(json.dumps(data, indent=2))


async def main():
    async with httpx.AsyncClient(base_url=BASE, headers=H, timeout=30) as c:

        # ── 1. Create users ───────────────────────────────────────────────────
        print("\n[1/9] Creating institutional participants...")
        fund_a = (await c.post("/users", json={
            "name": "Vanguard_Fund", "role": "shareholder",
            "external_id": "BBG-VANG",
        })).json()
        fund_b = (await c.post("/users", json={
            "name": "BlackRock_Fund", "role": "shareholder",
            "external_id": "BBG-BLK",
        })).json()
        board = (await c.post("/users", json={
            "name": "CEO_Director", "role": "board",
        })).json()
        print(f"   ✓ {fund_a['name']}  id={fund_a['id'][:8]}…")
        print(f"   ✓ {fund_b['name']} id={fund_b['id'][:8]}…")
        print(f"   ✓ {board['name']}   id={board['id'][:8]}…")

        # ── 2. Register entity ────────────────────────────────────────────────
        print("\n[2/9] Registering Meridian Technologies plc...")
        entity = (await c.post("/entities", json={
            "name": "Meridian Technologies plc",
            "ticker": "MRDN", "lei": "2138003B1P3EWRDE4780",
            "jurisdiction": "UK", "total_shares": 10_000_000,
        })).json()
        print(f"   ✓ {entity['ticker']} — {entity['name']}")
        eid = entity["id"]

        # ── 3. Assign ownership ───────────────────────────────────────────────
        print("\n[3/9] Assigning share ownership...")
        for uid, shares, mult, label in [
            (fund_a["id"], 4_200_000, 1.0, "Vanguard  42%"),
            (fund_b["id"], 3_500_000, 1.0, "BlackRock 35%"),
            (board["id"],    800_000, 1.5, "CEO        8% × 1.5×"),
        ]:
            await c.put("/entities/ownership",
                        json={"user_id": uid, "entity_id": eid,
                              "shares": shares, "role_weight_multiplier": mult})
            print(f"   ✓ {label}  →  eff. weight {shares * mult:>12,.0f}")

        # ── 4. Create merger proposal ─────────────────────────────────────────
        print("\n[4/9] Creating P-001: Acquisition of AlphaTech Ltd...")
        proposal = (await c.post(
            "/proposals",
            params={"creator_id": board["id"]},
            json={
                "id": "P-001",
                "entity_id": eid,
                "title": "Acquisition of AlphaTech Ltd — Special Resolution",
                "description": "All-share acquisition at 22% premium, subject to UK Takeover Code.",
                "proposal_type": "merger",
                "threshold_type": "supermajority",
                "quorum_pct": 0.60,
                "closes_at": int(time.time()) + 3600,
            },
        )).json()
        print(f"   ✓ {proposal['id']} — status: {proposal['status']}")

        # ── 5. Cast votes ─────────────────────────────────────────────────────
        print("\n[5/9] Casting votes...")
        va = (await c.post("/votes", json={
            "proposal_id": "P-001", "voter_id": fund_a["id"],
            "action_type": "vote", "choice": "YES",
        })).json()
        print(f"   ✓ {fund_a['name']} → YES   weight={va['weight']:>12,.0f}")

        vboard = (await c.post("/votes", json={
            "proposal_id": "P-001", "voter_id": board["id"],
            "action_type": "approve", "choice": "YES",
        })).json()
        print(f"   ✓ {board['name']}   → APPROVE weight={vboard['weight']:>12,.0f}")

        vb = (await c.post("/votes", json={
            "proposal_id": "P-001", "voter_id": fund_b["id"],
            "action_type": "vote", "choice": "ABSTAIN",
        })).json()
        print(f"   ✓ {fund_b['name']} → ABSTAIN weight={vb['weight']:>12,.0f}")

        # ── 6. Close and compute result ───────────────────────────────────────
        print("\n[6/9] Closing proposal and computing result...")
        result = (await c.post("/proposals/P-001/close")).json()

        verdict = "✅  RESOLUTION PASSED" if result["passed"] else "❌  RESOLUTION FAILED"
        print(f"\n   {verdict}")
        print(f"\n   Quorum met:        {result['quorum_met']}")
        print(f"   YES weight:        {result['yes_weight']:>14,.2f}")
        print(f"   NO weight:         {result['no_weight']:>14,.2f}")
        print(f"   ABSTAIN weight:    {result['abstain_weight']:>14,.2f}")
        print(f"   Participation:     {(result.get('participation_pct') or 0) * 100:.1f}%")

        # ── 7. Participation report ───────────────────────────────────────────
        print("\n[7/9] Participation breakdown...")
        part = (await c.get("/admin/proposals/P-001/participation")).json()
        print(f"   Eligible voters:   {part['eligible_count']}")
        print(f"   Voted:             {part['voter_count']}")
        print(f"   Participation:     {part['participation_pct']:.1f}%")
        for p in part["participants"]:
            voted_str = f"{p['choice']:>7}" if p["voted"] and p["choice"] else "  —    "
            print(f"   · {p['user_name']:<20} {p['effective_weight']:>12,.0f}  {voted_str}")

        # ── 8. Audit trail ────────────────────────────────────────────────────
        print("\n[8/9] Audit trail for P-001...")
        audit = (await c.get("/votes/P-001/audit")).json()
        for entry in audit:
            print(f"   [{entry['timestamp']}]  {entry['action']}")

        # ── 9. Delegation on a second proposal ────────────────────────────────
        print("\n[9/9] Demonstrating delegation on P-002...")
        fund_c = (await c.post("/users", json={
            "name": "StateStreet", "role": "shareholder",
        })).json()
        await c.put("/entities/ownership", json={
            "user_id": fund_c["id"], "entity_id": eid,
            "shares": 500_000, "role_weight_multiplier": 1.0,
        })
        await c.post("/proposals", params={"creator_id": board["id"]}, json={
            "id": "P-002", "entity_id": eid,
            "title": "Director Re-election: Sarah Chen",
            "proposal_type": "election",
            "threshold_type": "simple_majority",
            "quorum_pct": 0.30,
        })

        # StateStreet delegates to Vanguard
        d = (await c.post("/votes", json={
            "proposal_id": "P-002",
            "voter_id": fund_c["id"],
            "action_type": "delegate",
            "delegate_to_id": fund_a["id"],
        })).json()
        print(f"   ✓ StateStreet delegates {d['weight']:,.0f} → Vanguard")

        # Vanguard votes with boosted weight
        va2 = (await c.post("/votes", json={
            "proposal_id": "P-002", "voter_id": fund_a["id"],
            "action_type": "vote", "choice": "YES",
        })).json()
        print(f"   ✓ Vanguard votes YES on P-002 with weight={va2['weight']:,.0f}")

        result2 = (await c.post("/proposals/P-002/close")).json()
        print(f"   ✓ P-002 result: passed={result2['passed']} quorum={result2['quorum_met']}")

        # ── Health check ──────────────────────────────────────────────────────
        health = (await c.get("/health")).json()
        stats  = (await c.get("/admin/stats")).json()
        print(f"\n{'─' * 62}")
        print(f"  System Health: {health['status']}")
        print(f"  Entities: {stats['entities']}  Users: {stats['users']}  "
              f"Votes: {stats['votes_cast']}  Audit entries: {stats['audit_log_entries']}")
        print(f"{'─' * 62}")
        print("\n✅  Electa Systems example run complete.\n")


if __name__ == "__main__":
    asyncio.run(main())
