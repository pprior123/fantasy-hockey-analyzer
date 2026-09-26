"""The League screen (SPEC §7)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from fha.web.app import render, services

router = APIRouter()


@router.get("/league", response_class=HTMLResponse)
async def league(request: Request) -> HTMLResponse:
    services(request)  # a missing configuration shows the error page
    return render(request, "page.html", active="league", title="League")
