"""Shared fixtures. Tests never touch the real data root: TRADEQ_DATA_DIR points at tmp_path."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest

from mcp_gateway.harness import Harness
from mcp_gateway.settings import Settings, load_settings

ADMIN_KEY = "test-admin-key"


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in list(os.environ):
        if name.startswith("GATEWAY_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("TRADEQ_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    yield


@pytest.fixture
def settings() -> Settings:
    return load_settings()


def with_updates(settings: Settings, section: str, **updates: Any) -> Settings:
    """Copy of settings with fields of one nested section replaced."""
    sub = getattr(settings, section).model_copy(update=updates)
    return settings.model_copy(update={section: sub})


@pytest.fixture
async def harness(settings: Settings) -> AsyncIterator[Harness]:
    h = Harness.create(settings, admin_key=ADMIN_KEY)
    yield h
    await h.gateway.downstream.aclose()
