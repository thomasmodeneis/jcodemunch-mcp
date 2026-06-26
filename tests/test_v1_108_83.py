"""v1.108.83 — watch-all CPU under WSL (issue #356).

watchfiles auto-enables polling under WSL; its default 300ms poll re-stats every
watched tree several times a second, pegging the CPU on a many-repo host. We
raise the poll floor (tunable) and emit a one-time WSL hint. These tests pin the
poll-delay resolution + WSL detection helpers (the awatch wiring itself needs the
optional `watchfiles` extra, so it's exercised by the live watcher suite).
"""
from __future__ import annotations

import builtins

import pytest

from jcodemunch_mcp import watcher


# --- poll delay resolution ---------------------------------------------------

def _clear_delay_env(monkeypatch):
    monkeypatch.delenv("JCODEMUNCH_WATCH_POLL_DELAY_MS", raising=False)
    monkeypatch.delenv("WATCHFILES_POLL_DELAY_MS", raising=False)


def test_default_poll_delay_is_raised_floor(monkeypatch):
    _clear_delay_env(monkeypatch)
    assert watcher._watch_poll_delay_ms() == watcher.DEFAULT_WATCH_POLL_DELAY_MS == 1000


def test_jcm_env_overrides_poll_delay(monkeypatch):
    _clear_delay_env(monkeypatch)
    monkeypatch.setenv("JCODEMUNCH_WATCH_POLL_DELAY_MS", "2500")
    assert watcher._watch_poll_delay_ms() == 2500


@pytest.mark.parametrize("bad", ["0", "-5", "junk", ""])
def test_non_positive_or_garbage_falls_back_to_default(monkeypatch, bad):
    _clear_delay_env(monkeypatch)
    monkeypatch.setenv("JCODEMUNCH_WATCH_POLL_DELAY_MS", bad)
    assert watcher._watch_poll_delay_ms() == watcher.DEFAULT_WATCH_POLL_DELAY_MS


def test_watchfiles_env_is_fallback(monkeypatch):
    _clear_delay_env(monkeypatch)
    monkeypatch.setenv("WATCHFILES_POLL_DELAY_MS", "1800")
    assert watcher._watch_poll_delay_ms() == 1800


def test_jcm_env_takes_precedence_over_watchfiles_env(monkeypatch):
    _clear_delay_env(monkeypatch)
    monkeypatch.setenv("WATCHFILES_POLL_DELAY_MS", "1800")
    monkeypatch.setenv("JCODEMUNCH_WATCH_POLL_DELAY_MS", "4000")
    assert watcher._watch_poll_delay_ms() == 4000


# --- WSL detection -----------------------------------------------------------

def test_is_wsl_false_on_non_linux(monkeypatch):
    monkeypatch.setattr(watcher.sys, "platform", "win32")
    assert watcher._is_wsl() is False


def test_is_wsl_true_when_proc_version_names_microsoft(monkeypatch):
    monkeypatch.setattr(watcher.sys, "platform", "linux")
    real_open = builtins.open

    def fake_open(path, *a, **k):
        if str(path) == "/proc/version":
            from io import StringIO
            return StringIO("Linux version 5.15.0-microsoft-standard-WSL2 ...")
        return real_open(path, *a, **k)

    monkeypatch.setattr(builtins, "open", fake_open)
    assert watcher._is_wsl() is True


def test_is_wsl_false_on_bare_metal_linux(monkeypatch):
    monkeypatch.setattr(watcher.sys, "platform", "linux")
    real_open = builtins.open

    def fake_open(path, *a, **k):
        if str(path) == "/proc/version":
            from io import StringIO
            return StringIO("Linux version 6.8.0-generic (gcc ...) ")
        return real_open(path, *a, **k)

    monkeypatch.setattr(builtins, "open", fake_open)
    assert watcher._is_wsl() is False
