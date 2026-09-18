"""config 模块单测，对应 docs/modules/config.md 第 7 节。"""

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_settings_loads_from_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/db")
    s = Settings()  # ENV 默认 dev
    assert s.database_url == "postgresql://u:p@localhost/db"


def test_prod_requires_llm_key(monkeypatch):
    monkeypatch.setenv("ENV", "prod")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    with pytest.raises(ValidationError):
        Settings()


def test_secret_not_in_repr(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-secret-123")
    s = Settings()
    assert "sk-secret-123" not in repr(s)


def test_prod_ok_when_required_present(monkeypatch):
    monkeypatch.setenv("ENV", "prod")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/db")
    monkeypatch.setenv("LLM_API_KEY", "sk-secret-123")
    s = Settings()
    assert s.env == "prod"
    assert s.database_url == "postgresql://u:p@localhost/db"


def test_get_settings_is_singleton(monkeypatch):
    from app.config import get_settings

    a = get_settings()
    b = get_settings()
    assert a is b
