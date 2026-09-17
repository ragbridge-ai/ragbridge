"""Health check endpoint, used by monitoring and container orchestrators."""

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health")
def get_health() -> dict[str, str]:
    """Report that the service is up."""
    return {"status": "ok"}
