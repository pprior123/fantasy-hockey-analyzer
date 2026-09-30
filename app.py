"""The Vercel entry point: the lazy ASGI app from ``fha.web.main``.

Vercel's Python runtime loads ``app`` from a root-level ``app.py`` (one of its
zero-config entrypoints). ``[tool.vercel] entrypoint`` would look for
``fha/web/main.py`` at the root, which the ``src/`` layout doesn't have, so
this file re-exports it. Importing it builds nothing and reads no environment.
"""

from fha.web.main import app

__all__ = ["app"]
