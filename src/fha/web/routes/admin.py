"""The Admin screen (SPEC §7)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from fha.web.app import render, services

router = APIRouter()


@router.get("/admin", response_class=HTMLResponse)
async def admin(request: Request) -> HTMLResponse:
    services(request)  # a missing configuration shows the error page
    return render(request, "page.html", active="admin", title="Admin")
