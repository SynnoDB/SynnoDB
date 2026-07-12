"""W&B is opt-in with no separate on/off flag: it turns on iff an entity or
project is supplied - either explicitly on ``SynnoConfig``/via the ``SynnoDB``
constructor, or through the ``WANDB_ENTITY``/``WANDB_PROJECT`` env vars (or
``.env``). This is the single gate the whole pipeline reads (``log_to_wandb``);
the CLI and the Python API must agree so ``.env`` behaves the same on both.
"""

from __future__ import annotations

from synnodb.api import SynnoConfig
from synnodb.settings import wandb_logging_enabled


def _clear_wandb_env(monkeypatch):
    # Isolate from the developer's real .env: the resolver calls load_dotenv(),
    # which would otherwise repopulate WANDB_* from the repo .env right after we
    # clear it. Stub it to a no-op so only the process env drives the assertions.
    import synnodb.settings as settings_mod

    monkeypatch.setattr(settings_mod, "load_dotenv", lambda *a, **k: False)
    monkeypatch.delenv("WANDB_ENTITY", raising=False)
    monkeypatch.delenv("WANDB_PROJECT", raising=False)


# ── the single source of truth: settings.wandb_logging_enabled ─────────────


def test_disabled_when_nothing_set(monkeypatch):
    _clear_wandb_env(monkeypatch)
    # The project *default* ("SynnoDB") must NOT count as opting in - otherwise
    # every run would log.
    assert wandb_logging_enabled(None, None) is False


def test_explicit_entity_or_project_enables(monkeypatch):
    _clear_wandb_env(monkeypatch)
    assert wandb_logging_enabled("some-team", None) is True
    assert wandb_logging_enabled(None, "some-project") is True


def test_env_entity_or_project_enables(monkeypatch):
    _clear_wandb_env(monkeypatch)
    monkeypatch.setenv("WANDB_ENTITY", "learneddb")
    assert wandb_logging_enabled(None, None) is True

    monkeypatch.delenv("WANDB_ENTITY", raising=False)
    monkeypatch.setenv("WANDB_PROJECT", "SynnoDB")
    assert wandb_logging_enabled(None, None) is True


# ── the API path reads that gate (regression: .env used to be ignored) ─────


def test_config_wandb_enabled_honors_env(monkeypatch):
    # Setting keys in .env / the environment must enable W&B on the Python API
    # path too, matching the CLI. Previously SynnoConfig.wandb_enabled checked
    # only its own fields and silently ignored the env, so a demo that relied on
    # .env never logged.
    _clear_wandb_env(monkeypatch)
    assert SynnoConfig().wandb_enabled is False

    monkeypatch.setenv("WANDB_PROJECT", "SynnoDB")
    assert SynnoConfig().wandb_enabled is True


def test_config_wandb_enabled_honors_explicit_args(monkeypatch):
    _clear_wandb_env(monkeypatch)
    assert SynnoConfig(wandb_entity="some-team").wandb_enabled is True
    assert SynnoConfig(wandb_project="some-project").wandb_enabled is True
