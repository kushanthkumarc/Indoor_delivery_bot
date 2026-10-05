"""
Tests for database URL normalization (PlanetScale / Aiven / Render compatibility).

Some managed MySQL providers embed SSL params as query-string keys like
`?ssl-mode=REQUIRED`. SQLAlchemy's MySQL/aiomysql dialect forwards every
query-string key as a `**kwarg` to `aiomysql.connect()`, which does NOT
accept `ssl-mode`. `app.db.session._normalize_database_url` strips those
keys and returns a clean URL plus a `connect_args` dict.

The SSL arg inside `connect_args` must be a `bool` or `ssl.SSLContext`
object — never a dict. Passing a dict breaks the asyncio SSL transport
(`AttributeError: 'dict' object has no attribute 'wrap_bio'`).

For cloud MySQL providers (PlanetScale, Aiven, Render) the standard
pattern is `ssl-mode=REQUIRED` (no cert verify) because the providers
use self-signed certs that aren't in the system trust store. The
connection is still encrypted; we just don't validate the server's
identity.
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


def test_build_ssl_arg_required_returns_sslcontext_no_verify():
    """ssl-mode=REQUIRED → SSLContext with cert verification OFF.

    This is the standard pattern for cloud MySQL providers whose certs
    are self-signed (PlanetScale, Aiven, Render). The connection is
    still encrypted; we just don't validate the server's identity.
    """
    ctx = _build_ssl_arg({"ssl_mode": "REQUIRED"})
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.check_hostname is False
    assert ctx.verify_mode == ssl.CERT_NONE


def test_build_ssl_arg_underscore_form_required():
    ctx = _build_ssl_arg({"ssl_mode": "required"})
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.check_hostname is False
    assert ctx.verify_mode == ssl.CERT_NONE


def test_build_ssl_arg_default_when_no_mode():
    """No ssl_mode key, has ssl-ca → SSLContext (verify-CA path)."""
    ctx = _build_ssl_arg({"ssl_ca": _CA_FILE})
    assert isinstance(ctx, ssl.SSLContext)
    # When ssl-ca is present, cert verification is enforced.
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


def test_build_ssl_arg_with_custom_ca_returns_sslcontext():
    """ssl-ca=/path → SSLContext that loads the CA file and verifies."""
    ctx = _build_ssl_arg({"ssl_ca": _CA_FILE})
    assert isinstance(ctx, ssl.SSLContext)
    # load_verify_locations stores the cafile internally; we just verify
    # the context was built without raising.
    assert ctx.verify_mode == ssl.CERT_REQUIRED


def test_build_ssl_arg_with_ca_and_verify_ca():
    """Both ssl-ca and ssl-mode=VERIFY_CA → SSLContext with both flags."""
    ctx = _build_ssl_arg({"ssl_ca": _CA_FILE, "ssl_mode": "VERIFY_CA"})
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.check_hostname is True
    assert ctx.verify_mode == ssl.CERT_REQUIRED


def test_build_ssl_arg_verify_identity():
    """ssl-mode=VERIFY_IDENTITY → SSLContext with cert verification ON."""
    ctx = _build_ssl_arg({"ssl_mode": "VERIFY_IDENTITY"})
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.check_hostname is True
    assert ctx.verify_mode == ssl.CERT_REQUIRED


def test_build_ssl_arg_never_returns_true():
    """Critical regression: never return bare True (causes Aiven failure)."""
    test_cases = [
        {"ssl_mode": "REQUIRED"},
        {"ssl_mode": "PREFERRED"},
        {"ssl_ca": _CA_FILE},
        {"ssl_mode": "VERIFY_CA"},
        {"ssl_mode": "VERIFY_IDENTITY"},
    ]
    for case in test_cases:
        result = _build_ssl_arg(case)
        assert result is not True, f"_build_ssl_arg({case!r}) returned True; must return SSLContext"
        assert isinstance(result, ssl.SSLContext), (
            f"_build_ssl_arg({case!r}) returned {type(result).__name__}; must return SSLContext"
        )


# ---------------------------------------------------------------------------
# _normalize_database_url
# ---------------------------------------------------------------------------


def test_strips_ssl_mode_planetscale():
    raw = "mysql+aiomysql://user:pass@host.example.com:3306/db?ssl-mode=REQUIRED"
    clean, connect_args = _normalize_database_url(raw)
    assert "ssl-mode" not in clean
    assert "ssl_mode" not in clean
    # SSL arg must be SSLContext, never a dict or bool.
    assert "ssl" in connect_args
    assert isinstance(connect_args["ssl"], ssl.SSLContext)
    assert connect_args["ssl"].verify_mode == ssl.CERT_NONE


def test_aiven_self_signed_no_verification():
    """Aiven uses self-signed certs; SSL must be on, cert verify off."""
    raw = (
        "mysql+aiomysql://avnadmin:pass@mysql-237033e1-kushanth2005-1c5a"
        ".i.aivencloud.com:12345/defaultdb?ssl-mode=REQUIRED"
    )
    clean, connect_args = _normalize_database_url(raw)
    assert "ssl-mode" not in clean
    ctx = connect_args["ssl"]
    assert isinstance(ctx, ssl.SSLContext)
    # Critical: NOT CERT_REQUIRED (would fail on self-signed certs).
    assert ctx.verify_mode == ssl.CERT_NONE
    assert ctx.check_hostname is False


def test_strips_ssl_ca_aiven():
    raw = f"mysql+aiomysql://user:pass@host:3306/db?ssl-ca={_CA_FILE}"
    clean, connect_args = _normalize_database_url(raw)
    assert "ssl-ca" not in clean
    assert isinstance(connect_args["ssl"], ssl.SSLContext)
    # ssl-ca present → cert verify on
    assert connect_args["ssl"].verify_mode == ssl.CERT_REQUIRED


def test_strips_multiple_ssl_params():
    raw = (
        f"mysql+aiomysql://user:pass@host:3306/db"
        f"?ssl-mode=REQUIRED&ssl-ca={_CA_FILE}"
    )
    clean, connect_args = _normalize_database_url(raw)
    assert "ssl" not in clean
    # Both keys present → verify-CA path → SSLContext with cert verify.
    assert isinstance(connect_args["ssl"], ssl.SSLContext)
    assert connect_args["ssl"].verify_mode == ssl.CERT_REQUIRED


def test_underscore_form_ssl_mode():
    raw = "mysql+aiomysql://user:pass@host:3306/db?ssl_mode=REQUIRED"
    clean, connect_args = _normalize_database_url(raw)
    assert "ssl_mode" not in clean
    assert isinstance(connect_args["ssl"], ssl.SSLContext)
    assert connect_args["ssl"].verify_mode == ssl.CERT_NONE


def test_no_ssl_params_returns_clean_url_unchanged():
    raw = "mysql+aiomysql://root:@127.0.0.1:3306/robot_db"
    clean, connect_args = _normalize_database_url(raw)
    assert clean == raw
    assert "ssl" not in connect_args  # no SSL config, no key


def test_localhost_no_ssl_returns_empty():
    """Plain local connection — no SSL at all (no key in connect_args)."""
    raw = "mysql+aiomysql://root:@127.0.0.1:3306/robot_db"
    _, connect_args = _normalize_database_url(raw)
    assert "ssl" not in connect_args


def test_preserves_non_ssl_query_params():
    raw = "mysql+aiomysql://u:p@h:3306/db?charset=utf8mb4&ssl-mode=REQUIRED"
    clean, connect_args = _normalize_database_url(raw)
    assert "charset=utf8mb4" in clean
    assert "ssl-mode" not in clean
    assert isinstance(connect_args["ssl"], ssl.SSLContext)


def test_preserves_password_with_special_chars():
    raw = "mysql+aiomysql://u:p%40ss%21@h:3306/db?ssl-mode=REQUIRED"
    clean, connect_args = _normalize_database_url(raw)
    assert "p%40ss%21" in clean  # %40=@, %21=!
    assert "ssl-mode" not in clean
    assert isinstance(connect_args["ssl"], ssl.SSLContext)


def test_ssl_arg_never_dict_under_any_url():
    """
    Critical regression: no matter what ssl-* keys are present, the
    returned `connect_args["ssl"]` must be an `ssl.SSLContext`, never a
    dict or a bare `True`. This is what caused both the
    `'dict' has no attribute 'wrap_bio'` error AND the
    `SSLCertVerificationError: self-signed certificate` error.
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
            assert connect_args["ssl"] is not True, (
                f"connect_args['ssl'] is True for {suffix!r}; "
                f"this fails on cloud MySQL providers with self-signed certs. "
                f"Got: {connect_args['ssl']!r}"
            )
            assert isinstance(connect_args["ssl"], ssl.SSLContext)
