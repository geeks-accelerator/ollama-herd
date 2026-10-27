"""Every route a module declares must be one the app actually serves.

``text_embedding_compat`` once decorated a handler with ``@router.post("/api/embed-text")``
on a router ``app.py`` never included, so the endpoint 404'd while reading as API
surface — and a test docstring that said so did not get it removed.  This pins the
class rather than the instance: any module-level ``APIRouter`` under
``server/routes/`` must have all of its paths mounted on the real app.
"""

from __future__ import annotations

import importlib
import pkgutil

from fastapi import APIRouter

import fleet_manager.server.routes as routes_pkg
from fleet_manager.server.app import create_app


def _declared_routers():
    for info in pkgutil.iter_modules(routes_pkg.__path__):
        mod = importlib.import_module(f"{routes_pkg.__name__}.{info.name}")
        for name, obj in vars(mod).items():
            if isinstance(obj, APIRouter):
                yield f"{info.name}.{name}", obj


def test_every_declared_router_is_mounted():
    app = create_app()
    # FastAPI 0.15x includes routers lazily (an ``_IncludedRouter`` holding the
    # original); older versions copy each route onto the app.  Accept either.
    mounted = {id(r.original_router) for r in app.routes if hasattr(r, "original_router")}
    served = {r.path for r in app.routes if hasattr(r, "path")}
    unmounted = [
        f"{owner}: {[route.path for route in router.routes]}"
        for owner, router in _declared_routers()
        if id(router) not in mounted and not {route.path for route in router.routes} <= served
    ]
    assert not unmounted, f"declared but never served: {unmounted}"


def test_there_are_routers_to_check():
    # Guard against the scan silently finding nothing and passing vacuously.
    assert len(list(_declared_routers())) >= 8
