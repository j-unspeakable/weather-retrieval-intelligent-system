"""Health-check routes."""

from fastapi import APIRouter
from fastapi.responses import RedirectResponse


router = APIRouter(tags=["health"])


@router.get("/", include_in_schema=False)
def home() -> RedirectResponse:
    return RedirectResponse(url="/weather", status_code=307)


@router.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
