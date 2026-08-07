"""
Unit tests for warmup_send.py's recipients-file resolution, added 2026-08-07
alongside wiring warm-up sending into scheduler.py. No network, no API keys.
"""

import warmup_send


def test_prefers_the_persistent_volume_path_over_repo_root(monkeypatch, tmp_path):
    volume_path = tmp_path / "data_recipients.txt"
    repo_path = tmp_path / "repo_recipients.txt"
    volume_path.write_text("volume@example.com\n")
    repo_path.write_text("repo@example.com\n")

    monkeypatch.setattr(warmup_send, "_RECIPIENTS_CANDIDATES", [str(volume_path), str(repo_path)])
    assert warmup_send._load_recipients() == ["volume@example.com"]


def test_falls_back_to_repo_root_when_volume_path_absent(monkeypatch, tmp_path):
    missing_volume_path = tmp_path / "does_not_exist.txt"
    repo_path = tmp_path / "repo_recipients.txt"
    repo_path.write_text("repo@example.com\n")

    monkeypatch.setattr(warmup_send, "_RECIPIENTS_CANDIDATES", [str(missing_volume_path), str(repo_path)])
    assert warmup_send._load_recipients() == ["repo@example.com"]


def test_raises_a_clear_error_when_neither_path_exists(monkeypatch, tmp_path):
    monkeypatch.setattr(
        warmup_send, "_RECIPIENTS_CANDIDATES",
        [str(tmp_path / "a.txt"), str(tmp_path / "b.txt")],
    )
    try:
        warmup_send._load_recipients()
        assert False, "expected FileNotFoundError"
    except FileNotFoundError as e:
        assert "a.txt" in str(e) and "b.txt" in str(e)


def test_blank_lines_are_skipped(monkeypatch, tmp_path):
    path = tmp_path / "recipients.txt"
    path.write_text("first@example.com\n\n   \nsecond@example.com\n")
    monkeypatch.setattr(warmup_send, "_RECIPIENTS_CANDIDATES", [str(path)])
    assert warmup_send._load_recipients() == ["first@example.com", "second@example.com"]


def test_warmup_enabled_defaults_to_false(monkeypatch):
    monkeypatch.delenv("WARMUP_ENABLED", raising=False)
    import importlib
    import config
    importlib.reload(config)
    assert config.WARMUP_ENABLED is False
    monkeypatch.setenv("WARMUP_ENABLED", "true")
    importlib.reload(config)
    assert config.WARMUP_ENABLED is True
    monkeypatch.delenv("WARMUP_ENABLED", raising=False)
    importlib.reload(config)
