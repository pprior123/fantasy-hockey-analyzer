"""The ASGI entry point: ``uv run uvicorn fha.web.main:app --reload`` (and Vercel).

The app is built from the environment on the first event (lazily, so
importing this module reads no environment and starts nothing). A missing
configuration gives an app that shows the error, naming the variables.
"""

from __future__ import annotations

import os
from typing import Any

from fha.web.app import create_app, create_error_app
from fha.web.context import AppContext, ConfigError

ASGIApp = Any


def build(environ: dict[str, str] | None = None) -> ASGIApp:
    try:
        context = AppContext.from_env(os.environ if environ is None else environ)
    except ConfigError as e:
        return create_error_app(str(e))
    return create_app(context)


class LazyApp:
    def __init__(self) -> None:
        self._app: ASGIApp | None = None

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if self._app is None:
            self._app = build()
        await self._app(scope, receive, send)


app = LazyApp()
