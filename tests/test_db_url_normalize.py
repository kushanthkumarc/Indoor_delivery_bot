"""
Tests for database URL normalization (PlanetScale / Aiven compatibility).

Some managed MySQL providers embed SSL params as query-string keys like
`?ssl-mode=REQUIRED`. SQLAlchemy's MySQL/aiomysql dialect forwards every
query-string key as a `**kwarg` to `aiomysql.connect()`, which does NOT
accept `ssl-mode`. `app.db.session._normalize_database_url` strips those
keys and returns a clean URL plus a `connect_args` dict.

The SSL arg inside `connect_args` must be a `bool` or `ssl.SSLContext`
object — never a dict. Passing a dict breaks the asyncio SSL transport
(`AttributeError: 'dict' object has no attribute 'wrap_bio'`).
"""
import ssl

import certifi

from app.db.session import _build_ssl_arg, _normalize_database_url

# Real CA file we can use in tests that exercise `load_verify_locations`.
_CA_FILE = certifi.where()


# ---------------------------------------------------------------------------
# _build_ssl_arg
# ---------------------------------------------------------------------------


def test_build_ssl_arg_empty_returns_false():
    assert _build_ssl_arg({}) is False


def test_build_ssl_arg_required_returns_true():
    """ssl-mode=REQUIRED → True (SSL on, no cert verification)."""
    assert _build_ssl_arg({"ssl_mode": "REQUIRED"}) is True


def test_build_ssl_arg_underscore_form_required():
    assert _build_ssl_arg({"ssl_mode": "required"}) is True


def test_build_ssl_arg_verify_ca_returns_sslcontext():
    """ssl-mode=VERIFY_CA → real SSLContext with cert verification."""
    ctx = _build_ssl_arg({"ssl_mode": "VERIFY_CA"})
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.check_hostname is True
    assert ctx.verify_mode == ssl.CERT_REQUIRED


def test_build_ssl_arg_with_custom_ca_returns_sslcontext():
    """ssl-ca=/path → SSLContext that loads the CA file."""
    ctx = _build_ssl_arg({"ssl_ca": _CA_FILE})
    assert isinstance(ctx, ssl.SSLContext)
    # load_verify_locations stores the cafile internally; we just verify
    # the context was built without raising.


def test_build_ssl_arg_with_ca_and_verify_ca():
    """Both ssl-ca and ssl-mode=VERIFY_CA → SSLContext with both flags."""
    ctx = _build_ssl_arg({"ssl_ca": _CA_FILE, "ssl_mode": "VERIFY_CA"})
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.check_hostname is True
    assert ctx.verify_mode == ssl.CERT_REQUIRED


def test_build_ssl_arg_default_when_no_mode():
    """No ssl_mode key, has ssl-ca → SSLContext (verify-CA path)."""
    assert _build_ssl_arg({"ssl_ca": _CA_FILE}) is not False
    assert isinstance(_build_ssl_arg({"ssl_ca": _CA_FILE}), ssl.SSLContext)


# ---------------------------------------------------------------------------
# _normalize_database_url
# ---------------------------------------------------------------------------


def test_strips_ssl_mode_planetscale():
    raw = "mysql+aiomysql://user:pass@host.example.com:3306/db?ssl-mode=REQUIRED"
    clean, connect_args = _normalize_database_url(raw)
    assert "ssl-mode" not in clean
    assert "ssl_mode" not in clean
    # SSL arg must be bool or SSLContext, never a dict.
    assert "ssl" in connect_args
    assert not isinstance(connect_args["ssl"], dict)
    assert connect_args["ssl"] is True


def test_strips_ssl_ca_aiven():
    raw = f"mysql+aiomysql://user:pass@host:3306/db?ssl-ca={_CA_FILE}"
    clean, connect_args = _normalize_database_url(raw)
    assert "ssl-ca" not in clean
    assert isinstance(connect_args["ssl"], ssl.SSLContext)


def test_strips_multiple_ssl_params():
    raw = (
        f"mysql+aiomysql://user:pass@host:3306/db"
        f"?ssl-mode=REQUIRED&ssl-ca={_CA_FILE}"
    )
    clean, connect_args = _normalize_database_url(raw)
    assert "ssl" not in clean
    # Both keys present → verify-CA path → SSLContext.
    assert isinstance(connect_args["ssl"], ssl.SSLContext)


def test_underscore_form_ssl_mode():
    raw = "mysql+aiomysql://user:pass@host:3306/db?ssl_mode=REQUIRED"
    clean, connect_args = _normalize_database_url(raw)
    assert "ssl_mode" not in clean
    assert connect_args == {"ssl": True}


def test_no_ssl_params_returns_clean_url_unchanged():
    raw = "mysql+aiomysql://root:@127.0.0.1:3306/robot_db"
    clean, connect_args = _normalize_database_url(raw)
    assert clean == raw
    assert connect_args == {}


def test_preserves_non_ssl_query_params():
    raw = "mysql+aiomysql://u:p@h:3306/db?charset=utf8mb4&ssl-mode=REQUIRED"
    clean, connect_args = _normalize_database_url(raw)
    assert "charset=utf8mb4" in clean
    assert "ssl-mode" not in clean
    assert connect_args == {"ssl": True}


def test_preserves_password_with_special_chars():
    raw = "mysql+aiomysql://u:p%40ss%21@h:3306/db?ssl-mode=REQUIRED"
    clean, connect_args = _normalize_database_url(raw)
    assert "p%40ss%21" in clean  # %40=@, %21=!
    assert "ssl-mode" not in clean
    assert connect_args["ssl"] is True


def test_ssl_arg_never_dict_under_any_url():
    """
    Critical regression: no matter what ssl-* keys are present, the
    returned `connect_args["ssl"]` must be bool or SSLContext, never a
    dict. This is what caused `'dict' object has no attribute 'wrap_bio'`
    in the Render deploy.
    """
    cases = [
        "?ssl-mode=REQUIRED",
        "?ssl_mode=REQUIRED",
        f"?ssl-ca={_CA_FILE}",
        f"?ssl-mode=VERIFY_CA&ssl-ca={_CA_FILE}",
        f"?ssl-cert={_CA_FILE}&ssl-key={_CA_FILE}&ssl-mode=VERIFY_IDENTITY",
    ]
    for suffix in cases:
        raw = f"mysql+aiomysql://u:p@h:3306/db{suffix}"
        _, connect_args = _normalize_database_url(raw)
        if "ssl" in connect_args:
            assert not isinstance(connect_args["ssl"], dict), (
                f"connect_args['ssl'] is a dict for {suffix!r}; "
                f"this breaks the asyncio SSL transport. "
                f"Got: {connect_args['ssl']!r}"
            )
            assert isinstance(connect_args["ssl"], (bool, ssl.SSLContext))
