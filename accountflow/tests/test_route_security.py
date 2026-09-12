"""Every API route is either on the explicit public allowlist or requires the
tenant JWT / admin key, and no route accepts tenant_id from the client.

This is the automated form of the route audit in
docs/AccountFlow_Review_Report.md: a new unauthenticated route, or a route
that takes tenant_id as a parameter, fails CI instead of shipping.
"""
import pytest
from fastapi.routing import APIRoute

from app.api.deps import get_current_tenant, require_admin_key
from app.main import app

# (path, method) pairs that are unauthenticated BY DESIGN. Anything else must
# depend on get_current_tenant or require_admin_key. Adding to this set is a
# security decision — do it deliberately and say why in the commit message.
PUBLIC_ROUTES = {
    ("/", "GET"),                            # service banner
    ("/api/health", "GET"),                  # liveness probe
    ("/api/auth/login", "POST"),             # issues the JWT
    ("/api/auth/refresh", "POST"),           # the refresh cookie is the credential
    ("/api/auth/logout", "POST"),
    ("/api/auth/google/callback", "GET"),    # the signed state token is the credential
    ("/api/auth/microsoft/callback", "GET"),
    ("/api/review/{token}", "GET"),          # the Fernet review token is the credential
    ("/api/review/{token}", "POST"),
}

AUTH_DEPENDENCIES = {get_current_tenant, require_admin_key}


def _dependency_calls(dependant):
    for dep in dependant.dependencies:
        yield dep.call
        yield from _dependency_calls(dep)


def _api_routes():
    return [r for r in app.routes if isinstance(r, APIRoute)]


def _route_keys(route):
    return {(route.path, method) for method in route.methods}


def _route_id(route):
    return f"{','.join(sorted(route.methods))} {route.path}"


@pytest.mark.parametrize("route", _api_routes(), ids=_route_id)
def test_route_is_public_by_design_or_authenticated(route):
    if _route_keys(route) <= PUBLIC_ROUTES:
        return
    calls = set(_dependency_calls(route.dependant))
    assert calls & AUTH_DEPENDENCIES, (
        f"{_route_id(route)} has no tenant/admin auth dependency and is not in PUBLIC_ROUTES"
    )


def test_public_allowlist_matches_real_routes():
    """A renamed or removed public route must leave the allowlist too, or the
    stale entry silently grants a future route with that path a free pass."""
    real = set()
    for route in _api_routes():
        real |= _route_keys(route)
    stale = PUBLIC_ROUTES - real
    assert not stale, f"PUBLIC_ROUTES lists routes that no longer exist: {sorted(stale)}"


@pytest.mark.parametrize("route", _api_routes(), ids=_route_id)
def test_no_route_accepts_tenant_id_from_client(route):
    """tenant_id comes from the verified token or the worker — never the request."""
    dependant = route.dependant
    client_params = [
        param.name
        for group in (
            dependant.path_params,
            dependant.query_params,
            dependant.header_params,
            dependant.cookie_params,
            dependant.body_params,
        )
        for param in group
    ]
    assert "tenant_id" not in client_params, f"{_route_id(route)} accepts tenant_id from the client"

    for param in dependant.body_params:
        model = getattr(param, "type_", None)
        fields = getattr(model, "model_fields", None) or {}
        assert "tenant_id" not in fields, (
            f"{_route_id(route)} body model {getattr(model, '__name__', model)} exposes tenant_id"
        )
