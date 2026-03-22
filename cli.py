#!/usr/bin/env python3
"""
Electa Systems — CLI Management Tool
======================================
Command-line tool for database initialisation, demo data seeding,
and common operational tasks.

Usage:
    python cli.py seed                   Seed demo entities, users, proposals
    python cli.py init                   Initialise database tables
    python cli.py reset                  Drop and recreate all tables (destructive)
    python cli.py stats                  Print system statistics
    python cli.py proposals              List all proposals
    python cli.py create-user NAME ROLE  Create a user
    python cli.py close PROPOSAL_ID      Force-close a proposal
"""

import argparse
import asyncio
import json
import sys
import time
import os

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://electa:electa@localhost:5432/electa",
)


async def _init_tables():
    from database import engine, Base
    import models.db_models  # noqa: F401
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("✓ Tables created.")


async def _reset_tables():
    from database import engine, Base
    import models.db_models  # noqa: F401
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    print("✓ Tables dropped and recreated.")


async def _seed():
    from database import AsyncSessionLocal
    from models.db_models import (
        Entity, Ownership, Proposal, ProposalStatus,
        ThresholdType, User, UserRole,
    )

    await _init_tables()

    async with AsyncSessionLocal() as db:
        print("\n[1/4] Creating users...")
        users = [
            User(name="Vanguard_Fund",   role=UserRole.SHAREHOLDER, external_id="BBG-VANG"),
            User(name="BlackRock_Fund",  role=UserRole.SHAREHOLDER, external_id="BBG-BLK"),
            User(name="StateStreet",     role=UserRole.SHAREHOLDER, external_id="BBG-SST"),
            User(name="CEO_Director",    role=UserRole.BOARD),
            User(name="CFO_Director",    role=UserRole.BOARD),
            User(name="Electa_Admin",    role=UserRole.ADMIN),
        ]
        for u in users:
            db.add(u)
        await db.flush()
        print(f"   Created {len(users)} users.")

        print("[2/4] Creating entity...")
        entity = Entity(
            name="Meridian Technologies plc", ticker="MRDN",
            lei="2138003B1P3EWRDE4780", jurisdiction="UK",
            total_shares=10_000_000,
        )
        db.add(entity)
        await db.flush()
        print(f"   Created: {entity.ticker} — {entity.name}")

        print("[3/4] Assigning ownership...")
        ownerships = [
            (users[0], 3_500_000, 1.0),   # Vanguard    35%
            (users[1], 2_800_000, 1.0),   # BlackRock   28%
            (users[2], 1_700_000, 1.0),   # StateStreet 17%
            (users[3],   400_000, 1.5),   # CEO          4% × 1.5×
            (users[4],   200_000, 1.5),   # CFO          2% × 1.5×
        ]
        for user, shares, mult in ownerships:
            db.add(Ownership(user_id=user.id, entity_id=entity.id,
                             shares=shares, role_weight_multiplier=mult))
        await db.flush()
        print(f"   Assigned {len(ownerships)} ownership records.")

        print("[4/4] Creating proposals...")
        now = int(time.time())
        proposals_data = [
            ("P-SEED-001", "Acquisition of AlphaTech Ltd — Special Resolution",
             ThresholdType.SUPERMAJORITY, 0.60, now + 3600),
            ("P-SEED-002", "FY2025 Dividend Distribution — Ordinary Resolution",
             ThresholdType.SIMPLE_MAJORITY, 0.51, now + 7200),
            ("P-SEED-003", "Re-election of Non-Executive Directors",
             ThresholdType.SIMPLE_MAJORITY, 0.40, now + 86400),
        ]
        for pid, title, thresh, quorum, closes in proposals_data:
            db.add(Proposal(id=pid, entity_id=entity.id, title=title,
                            threshold_type=thresh, quorum_pct=quorum,
                            opens_at=now, closes_at=closes,
                            created_by=users[5].id,
                            status=ProposalStatus.OPEN))
        await db.commit()
        print(f"   Created {len(proposals_data)} proposals.")

    print("\n✅  Seed complete.")
    print(f"\n   Entity:    {entity.ticker} — {entity.name}")
    print(f"   Proposals: P-SEED-001, P-SEED-002, P-SEED-003")
    print(f"   Users:     {', '.join(u.name for u in users)}")
    print("\n   Start:  uvicorn main:app --reload")
    print("   Docs:   http://localhost:8000/docs\n")


async def _create_user(name: str, role: str):
    from database import AsyncSessionLocal
    from models.db_models import User, UserRole
    try:
        role_enum = UserRole(role)
    except ValueError:
        print(f"Invalid role '{role}'. Valid: {[r.value for r in UserRole]}")
        sys.exit(1)
    await _init_tables()
    async with AsyncSessionLocal() as db:
        user = User(name=name, role=role_enum)
        db.add(user)
        await db.commit()
        print(f"✓ Created user: {user.id}  name={name}  role={role}")


async def _list_proposals():
    from database import AsyncSessionLocal
    from models.db_models import Entity, Proposal
    from sqlalchemy import select
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Proposal, Entity).join(Entity, Proposal.entity_id == Entity.id)
            .order_by(Proposal.created_at.desc())
        )
        rows = result.all()
    if not rows:
        print("No proposals found.")
        return
    print(f"\n{'ID':<20} {'Status':<12} {'Title':<46} {'Entity'}")
    print("─" * 100)
    for p, e in rows:
        title = (p.title[:44] + "…") if len(p.title) > 45 else p.title
        passed = (" ✅" if p.result_passed else " ❌") if p.status.value == "closed" else ""
        print(f"{p.id:<20} {p.status.value:<12} {title:<46} {e.ticker or e.name[:14]}{passed}")


async def _close_proposal(pid: str):
    from database import AsyncSessionLocal
    from models.db_models import Proposal, ProposalStatus
    from services.decision_engine import close_and_compute
    async with AsyncSessionLocal() as db:
        p = await db.get(Proposal, pid)
        if not p:
            print(f"Proposal '{pid}' not found.")
            sys.exit(1)
        if p.status != ProposalStatus.OPEN:
            print(f"Proposal is already {p.status.value}.")
            sys.exit(1)
        tally, _ = await close_and_compute(db, p)
        await db.commit()
    print(f"\n✅  {pid} closed.")
    print(f"   Passed:        {tally.passed}")
    print(f"   Quorum met:    {tally.quorum_met}")
    print(f"   YES:           {tally.yes_weight:,.0f}")
    print(f"   NO:            {tally.no_weight:,.0f}")
    print(f"   ABSTAIN:       {tally.abstain_weight:,.0f}")
    print(f"   Participation: {tally.participation_pct * 100:.1f}%")


async def _stats():
    from database import AsyncSessionLocal
    from models.db_models import AuditLog, Entity, Proposal, ProposalStatus, User, Vote
    from sqlalchemy import func, select
    async with AsyncSessionLocal() as db:
        async def count(model):
            r = await db.execute(select(func.count()).select_from(model))
            return r.scalar_one()
        open_p = (await db.execute(
            select(func.count()).select_from(Proposal)
            .where(Proposal.status == ProposalStatus.OPEN)
        )).scalar_one()
        closed_p = (await db.execute(
            select(func.count()).select_from(Proposal)
            .where(Proposal.status == ProposalStatus.CLOSED)
        )).scalar_one()
    print(json.dumps({
        "entities":   await count(Entity),
        "users":      await count(User),
        "proposals":  {"open": open_p, "closed": closed_p},
        "votes":      await count(Vote),
        "audit_logs": await count(AuditLog),
    }, indent=2))


def main():
    parser = argparse.ArgumentParser(
        prog="electa", description="Electa Systems GEA — CLI management tool"
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("seed",      help="Seed demo data")
    sub.add_parser("init",      help="Initialise database tables")
    sub.add_parser("reset",     help="Drop and recreate all tables")
    sub.add_parser("stats",     help="Print system statistics")
    sub.add_parser("proposals", help="List all proposals")

    cu = sub.add_parser("create-user", help="Create a user")
    cu.add_argument("name")
    cu.add_argument("role", nargs="?", default="shareholder")

    cl = sub.add_parser("close", help="Force-close a proposal")
    cl.add_argument("proposal_id")

    args = parser.parse_args()

    if args.command == "seed":
        asyncio.run(_seed())
    elif args.command == "init":
        asyncio.run(_init_tables())
    elif args.command == "reset":
        confirm = input("⚠  This will DELETE all data. Type 'yes' to confirm: ")
        if confirm.strip().lower() == "yes":
            asyncio.run(_reset_tables())
        else:
            print("Aborted.")
    elif args.command == "stats":
        asyncio.run(_stats())
    elif args.command == "proposals":
        asyncio.run(_list_proposals())
    elif args.command == "create-user":
        asyncio.run(_create_user(args.name, args.role))
    elif args.command == "close":
        asyncio.run(_close_proposal(args.proposal_id))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
