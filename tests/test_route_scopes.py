"""Every /api/v1 route declares exactly one scope dependency, except the public probes."""
from fastapi.routing import APIRoute

PUBLIC = {"/api/v1/health/live", "/api/v1/health/ready"}


def api_routes(app):
    """Flatten the app's routes. FastAPI >= 0.163 nests included routers lazily
    (``_IncludedRouter``), so ``app.routes`` alone hides every /api/v1 route."""
    for route in app.routes:
        if isinstance(route, APIRoute):
            yield route
        elif hasattr(route, "effective_route_contexts"):
            yield from route.effective_route_contexts()


def route_scopes(route):
    return [dep.call.required_scope for dep in route.dependant.dependencies if hasattr(dep.call, "required_scope")]


def test_every_route_declares_one_scope():
    from app.main import app

    routes = [route for route in api_routes(app) if route.path.startswith("/api/v1")]
    assert len(routes) >= 50, "route discovery broke; the scope check would pass vacuously"
    unscoped, multiple = [], []
    for route in routes:
        if route.path in PUBLIC:
            continue
        scopes = route_scopes(route)
        if not scopes:
            unscoped.append(f"{sorted(route.methods)} {route.path}")
        elif len(scopes) > 1:
            multiple.append(f"{sorted(route.methods)} {route.path}: {scopes}")
    assert not unscoped, unscoped
    assert not multiple, multiple
