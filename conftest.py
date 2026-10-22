"""Shared test setup."""
import pytest

import config


@pytest.fixture(autouse=True)
def _no_bcc_copy_from_the_real_env(monkeypatch):
    # BCC_EMAIL comes from the machine's .env (the laptop sets it). Tests that
    # assert on who a send goes to must not depend on that; test_bcc_copy.py
    # sets it explicitly where it matters.
    monkeypatch.setattr(config, "BCC_EMAIL", "", raising=False)
