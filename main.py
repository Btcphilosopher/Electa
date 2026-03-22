"""
Electa Systems — Governance Execution API
Institutional-grade infrastructure for programmatic corporate governance.
Designed for integration with Bloomberg, Refinitiv, and prime-brokerage platforms.
"""

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from config import settings
from database import Base, engine
from middleware.rate_limiter import RateLimitMiddleware
from routers import admin, entities, events, proposals, users, votes, webhooks
from services.event_bus import event_bus
from services.scheduler import proposal_scheduler
from services.startup_hooks import register_event_hooks

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("electa")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Electa Systems GEA starting up...")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database schema initialised.")

    await event_bus.start()
    register_event_hooks()
    await proposal_scheduler.start()
    logger.info("Services online.")

    yield

    await proposal_scheduler.stop()
    await event_bus.stop()
    await engine.dispose()
    logger.info("Electa Systems GEA shut down cleanly.")


app = FastAPI(
    title="Electa Systems — Governance Execution API",
    description=(
        "Real-time, institutional-grade infrastructure for casting, processing, "
        "and distributing corporate governance decisions. Designed for integration "
        "with Bloomberg, Refinitiv, and prime-brokerage systems."
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# ── Middleware ────────────────────────────────────────────────────────────────

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RateLimitMiddleware)


@app.middleware("http")
async def add_timing_header(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    elapsed = (time.perf_counter() - start) * 1000
    response.headers["X-Process-Time-Ms"] = f"{elapsed:.2f}"
    response.headers["X-Powered-By"] = "Electa Systems GEA v1.0"
    return response


# ── Global exception handler ──────────────────────────────────────────────────

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception on %s %s", request.method, request.url)
    return JSONResponse(status_code=500, content={
        "error": "internal_server_error",
        "message": "An unexpected error occurred. This incident has been logged.",
    })


# ── Routers ───────────────────────────────────────────────────────────────────

app.include_router(users.router,     prefix="/users",     tags=["Users"])
app.include_router(entities.router,  prefix="/entities",  tags=["Entities"])
app.include_router(proposals.router, prefix="/proposals", tags=["Proposals"])
app.include_router(votes.router,     prefix="/votes",     tags=["Votes"])
app.include_router(events.router,    prefix="/events",    tags=["Events"])
app.include_router(webhooks.router,  prefix="/webhooks",  tags=["Webhooks"])
app.include_router(admin.router,     prefix="/admin",     tags=["Admin"])


# ── Health / root ─────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
async def root():
    return {
        "system": "Electa Systems Governance Execution API",
        "version": "1.0.0",
        "status": "operational",
        "docs": "/docs",
    }


@app.get("/health", tags=["System"])
async def health():
    return {
        "status": "healthy",
        "event_bus_subscribers": event_bus.subscriber_count(),
        "timestamp": int(time.time()),
    }
