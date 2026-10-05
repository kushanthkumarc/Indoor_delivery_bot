"""
Tests for database URL normalization (PlanetScale / Aiven compatibility).

Some managed MySQL providers embed SSL params as query-string keys like
`?ssl-mode=REQUIRED`. SQLAlchemy's MySQL/aiomysql dialect forwards every
query-string key as a `**kwarg` to `aiomysql.connect()`, which does NOT
accept `ssl-mode`. `app.db.session._normalize_database_url` strips those
keys and returns a clean URL plus a `connect_args` dict that aiomysql
understands.
"""
from app.db.session import _normalize_database_url


def test_strips_ssl_mode_planetscale():
    raw = "mysql+aiomysql://user:pass@host.example.com:3306/db?ssl-mode=REQUIRED"
    clean, connect_args = _normalize_database_url(raw)
    assert "ssl-mode" not in clean
    assert "ssl_mode" not in clean
    assert connect_args == {"ssl": {"ssl_mode": "REQUIRED"}}


def test_strips_ssl_ca_aiven():
    raw = "mysql+aiomysql://user:pass@host:3306/db?ssl-ca=/etc/ssl/ca.pem"
    clean, connect_args = _normalize_database_url(raw)
    assert "ssl-ca" not in clean
    assert connect_args == {"ssl": {"ssl_ca": "/etc/ssl/ca.pem"}}


def test_strips_multiple_ssl_params():
    raw = (
        "mysql+aiomysql://user:pass@host:3306/db"
        "?ssl-mode=REQUIRED&ssl-ca=/etc/ssl/ca.pem"
    )
    clean, connect_args = _normalize_database_url(raw)
    assert "ssl" not in clean
    assert connect_args["ssl"]["ssl_mode"] == "REQUIRED"
    assert connect_args["ssl"]["ssl_ca"] == "/etc/ssl/ca.pem"


def test_underscore_form_ssl_mode():
    raw = "mysql+aiomysql://user:pass@host:3306/db?ssl_mode=REQUIRED"
    clean, connect_args = _normalize_database_url(raw)
    assert "ssl_mode" not in clean
    assert connect_args == {"ssl": {"ssl_mode": "REQUIRED"}}


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
    assert connect_args == {"ssl": {"ssl_mode": "REQUIRED"}}


def test_preserves_password_with_special_chars():
    raw = "mysql+aiomysql://u:p%40ss%21@h:3306/db?ssl-mode=REQUIRED"
    clean, connect_args = _normalize_database_url(raw)
    assert "p%40ss%21" in clean  # %40=@, %21=!
    assert "ssl-mode" not in clean
    assert connect_args["ssl"]["ssl_mode"] == "REQUIRED"
