"""Shared API dependencies."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from app.services import Services


def get_services(request: Request) -> Services:
    services: Services | None = getattr(request.app.state, "services", None)
    if services is None:  # pragma: no cover - only when the app failed to start
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "SERVICES_UNAVAILABLE", "message": "Dobot is still starting up", "recoverable": True},
        )
    return services


ServicesDep = Annotated[Services, Depends(get_services)]
