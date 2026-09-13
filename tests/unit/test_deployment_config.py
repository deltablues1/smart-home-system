import importlib

import config.deployment_config as deployment_config


def _reload():
    deployment_config.reset_deployment_config_cache()
    importlib.reload(deployment_config)
    deployment_config.reset_deployment_config_cache()
    return deployment_config


def test_rpi_home_defaults(monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_PROFILE", "rpi-home")
    monkeypatch.delenv("API_TOKEN_REQUIRED", raising=False)

    module = _reload()
    cfg = module.get_deployment_config()

    assert cfg.profile == "rpi-home"
    assert cfg.telegram_enabled is True
    assert cfg.wake_word_enabled is True
    assert cfg.api_token_required is True


def test_env_overrides_profile_defaults(monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_PROFILE", "rpi-home")
    monkeypatch.setenv("API_TOKEN_REQUIRED", "false")
    monkeypatch.setenv("VOICE_MODE_DEFAULT", "live")

    module = _reload()
    cfg = module.get_deployment_config()

    assert cfg.api_token_required is False
    assert cfg.voice_mode_default == "live"
