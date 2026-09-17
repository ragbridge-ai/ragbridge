"""Health check endpoints, used by monitoring and container orchestrators."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from ragbridge.db.session import get_session

router = APIRouter(tags=["health"])


@router.get("/health")
def get_health() -> dict[str, str]:
    """Report that the process is running (liveness). No database check."""
    return {"status": "ok"}


@router.get("/health/ready")
async def get_readiness(session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, str]:
    """Report whether the service can serve traffic (readiness).

    Runs a trivial query against the database. An orchestrator uses this
    to stop routing traffic here while the database is unreachable.
    """
    try:
        await session.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="database unavailable") from exc
    return {"status": "ok"}
