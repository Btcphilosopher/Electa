# Electa Systems — Governance Execution API

> Institutional-grade infrastructure for programmatic corporate governance.
> Designed for integration with Bloomberg Terminal, Refinitiv Eikon, and prime-brokerage platforms.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                       Electa Systems GEA                            │
│                                                                     │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────────┐  │
│  │  /votes  │  │/proposals│  │/entities │  │   /events        │  │
│  └────┬─────┘  └────┬─────┘  └──────────┘  └──────┬───────────┘  │
│       │              │                              │               │
│  ┌────▼──────────────▼──────────────────────────────▼───────────┐  │
│  │                   Decision Engine                            │  │
│  │  share-weighted tallying · quorum enforcement                │  │
│  │  role multipliers · threshold logic (simple/super/unanimous) │  │
│  └────────────────────────┬──────────────────────────────────────┘  │
│                           │                                         │
│  ┌────────────────────────▼──────────────────────────────────────┐  │
│  │                     Event Bus                                │  │
│  │  async pub/sub · SSE stream · WebSocket · 200-event replay   │  │
│  └───────┬───────────────────────────────────────────────────────┘  │
│          │                                                          │
│  ┌───────▼──────────────┐    ┌──────────────────────────────────┐  │
│  │   Webhook Service    │    │         PostgreSQL               │  │
│  │  HMAC-SHA256 signing │    │  users · entities · proposals    │  │
│  │  retry + backoff     │    │  votes · ownership · audit_logs  │  │
│  └──────────────────────┘    │  webhook_endpoints · deliveries  │  │
│                              └──────────────────────────────────┘  │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │  Proposal Scheduler  (auto-close on closes_at deadline)     │   │
│  └─────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Quick Start

### Option A — Docker Compose (recommended)

```bash
git clone <repo> && cd electa
docker compose up --build
```

API:  **http://localhost:8000**
Docs: **http://localhost:8000/docs**

### Option B — Local Python

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # edit DATABASE_URL
uvicorn main:app --reload
```

### Seed demo data

```bash
python cli.py seed
```

---

## API Reference

### Users
| Method | Path | Description |
|---|---|---|
| `POST` | `/users` | Create user (returns one-time API key) |
| `GET` | `/users` | List users |
| `GET` | `/users/{id}` | Get user |
| `DELETE` | `/users/{id}` | Deactivate user |

### Entities
| Method | Path | Description |
|---|---|---|
| `POST` | `/entities` | Register entity |
| `GET` | `/entities` | List entities |
| `GET` | `/entities/{id}` | Get entity |
| `PUT` | `/entities/ownership` | Upsert share ownership |
| `GET` | `/entities/{id}/ownership` | Ownership table |

### Proposals
| Method | Path | Description |
|---|---|---|
| `POST` | `/proposals` | Create proposal |
| `GET` | `/proposals` | List (filter by entity, status) |
| `GET` | `/proposals/{id}` | Get proposal |
| `PATCH` | `/proposals/{id}/status` | Update status |
| `POST` | `/proposals/{id}/close` | Force-close + compute result |
| `GET` | `/proposals/{id}/result` | Get computed result |

### Votes
| Method | Path | Description |
|---|---|---|
| `POST` | `/votes` | Cast vote / approve / reject / delegate |
| `GET` | `/votes/{proposal_id}` | List votes for proposal |
| `GET` | `/votes/{proposal_id}/audit` | Append-only audit trail |

### Events
| Method | Path | Description |
|---|---|---|
| `GET` | `/events/stream` | SSE real-time stream |
| `WS` | `/events/ws` | WebSocket stream |
| `GET` | `/events/recent` | Replay buffer snapshot |

### Webhooks
| Method | Path | Description |
|---|---|---|
| `POST` | `/webhooks` | Register endpoint |
| `GET` | `/webhooks` | List |
| `DELETE` | `/webhooks/{id}` | Deactivate |
| `GET` | `/webhooks/{id}/deliveries` | Delivery history |
| `POST` | `/webhooks/test` | Send test ping |

### Admin
| Method | Path | Description |
|---|---|---|
| `GET` | `/admin/stats` | System statistics |
| `GET` | `/admin/audit` | Searchable audit trail |
| `GET` | `/admin/entities/{id}/ownership/validate` | Share cap check |
| `GET` | `/admin/proposals/overdue` | Proposals past deadline |
| `POST` | `/admin/scheduler/run` | Manual scheduler tick |
| `GET` | `/admin/events/recent` | Recent events |
| `GET` | `/admin/proposals/{id}/participation` | Voter participation |

---

## Governance Events

```json
{
  "event": "governance.vote.cast",
  "entity": "Meridian Technologies plc",
  "proposal_id": "P-001",
  "actor": "Vanguard_Fund",
  "vote": "YES",
  "weight": 4200000.0,
  "details": {"action_type": "vote", "proposal_title": "Acquisition of AlphaTech Ltd"},
  "timestamp": 1710000000
}
```

| Event | Trigger |
|---|---|
| `governance.proposal.created` | New proposal |
| `governance.vote.cast` | Vote, approve, or reject |
| `governance.vote.delegated` | Vote weight delegated |
| `governance.result.computed` | Proposal closed |
| `governance.test.ping` | Webhook test delivery |

---

## Weighted Decision Logic

```
effective_weight = shares × role_weight_multiplier
participation    = Σ(cast weights) / Σ(all eligible weights)
quorum_met       = participation ≥ quorum_pct
passed           = yes / (yes + no) > threshold   AND quorum_met
```

Abstentions count toward quorum but are excluded from the pass/fail ratio.

### Threshold types

| Type | Rule |
|---|---|
| `simple_majority` | YES > 50% of YES+NO |
| `supermajority` | YES ≥ 66.67% of YES+NO |
| `unanimous` | no_weight == 0 |
| `custom` | YES ≥ custom_threshold_pct |

---

## Webhook Security

All deliveries include `X-Electa-Signature: sha256=<HMAC-SHA256(secret, body)>`.

Verify in your consumer:

```python
import hmac, hashlib

def verify(secret: str, body: bytes, header: str) -> bool:
    expected = "sha256=" + hmac.new(
        secret.encode(), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, header)
```

---

## CLI

```bash
python cli.py seed                        # demo dataset
python cli.py init                        # initialise tables
python cli.py proposals                   # list all proposals
python cli.py create-user "Fund_X" shareholder
python cli.py close P-001
python cli.py stats
```

---

## Tests

```bash
pip install pytest pytest-asyncio aiosqlite
pytest -v
```

39 tests. No Postgres required — uses SQLite in-memory.

---

## Project Structure

```
electa/
├── main.py                    FastAPI app, lifespan, middleware
├── config.py                  Pydantic settings
├── database.py                Async SQLAlchemy engine + session DI
├── models/
│   ├── db_models.py           ORM: 8 tables
│   └── schemas.py             Pydantic schemas + GovernanceEvent
├── routers/
│   ├── users.py
│   ├── entities.py
│   ├── proposals.py
│   ├── votes.py
│   ├── events.py              SSE + WebSocket
│   ├── webhooks.py
│   └── admin.py               Stats, audit, participation, scheduler
├── services/
│   ├── event_bus.py           Async pub/sub with replay buffer
│   ├── decision_engine.py     Weighted tallying + threshold logic
│   ├── webhook_service.py     HMAC delivery + retry backoff
│   ├── scheduler.py           Auto-close background task
│   └── startup_hooks.py       Wires dispatcher to bus
├── middleware/
│   └── rate_limiter.py        Sliding-window per-IP rate limiting
├── utils/
│   ├── audit.py               Append-only audit helpers
│   └── crypto.py              Ed25519 signature verification
├── tests/
│   ├── conftest.py            Shared in-memory SQLite + client fixture
│   ├── test_governance.py     Core governance flows (25 tests)
│   └── test_extended.py       Admin, scheduler, rate limiter (21 tests)
├── cli.py                     CLI management tool
├── example_usage.py           End-to-end demo script
├── alembic/env.py             Async migration runner
├── alembic.ini
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
└── .env.example
```

---

## Integration Targets

| System | Integration Method |
|---|---|
| Bloomberg Terminal | Webhook → B-PIPE / SSE stream |
| Refinitiv Eikon | SSE → RIC-tagged event enrichment |
| Risk Engine | Webhook on `governance.result.computed` |
| Compliance Platform | Audit log polling + `governance.*` webhook filter |
| Accounting System | Webhook on proposal close for book-of-record update |
| DTC / Euroclear | Post-close settlement trigger |
