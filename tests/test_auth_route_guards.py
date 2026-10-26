"""Regression tests for authentication dependencies on sensitive delivery routes."""

from app.auth.dependencies import get_current_user
from app.delivery.router import router


def test_delivery_routes_use_authentication_and_admin_guards():
    expected_guards = {
        ("POST", "/deliveries"): get_current_user,
        ("GET", "/deliveries"): "admin",
        ("GET", "/deliveries/mine"): get_current_user,
        ("GET", "/deliveries/{delivery_id}"): get_current_user,
        ("POST", "/deliveries/{delivery_id}/dispatch"): "admin",
        ("POST", "/deliveries/{delivery_id}/confirm-pickup"): get_current_user,
        ("POST", "/deliveries/{delivery_id}/cancel"): "admin",
    }

    seen = set()
    for route in router.routes:
        key = (next(iter(route.methods)), route.path)
        if key not in expected_guards:
            continue
        seen.add(key)
        dependencies = route.dependant.dependencies
        assert len(dependencies) == 1
        dependency = dependencies[0].call
        expected = expected_guards[key]
        if expected == "admin":
            assert dependency.__name__ == "_check"
            assert dependency.__module__ == "app.auth.dependencies"
        else:
            assert dependency is expected

    assert seen == set(expected_guards)
