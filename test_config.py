from config import Config


def test_get_prefers_the_environment_over_the_file(monkeypatch, tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[telegram]\nbot_username = "from_file"\n\n[daemon]\nskip_permissions = false\n')
    monkeypatch.delenv("TELEGRAM_BOT_USERNAME", raising=False)
    monkeypatch.setenv("SKIP_PERMISSIONS", "1")
    config = Config.load(path)

    assert config.get("telegram.bot_username", "TELEGRAM_BOT_USERNAME") == "from_file"
    assert config.get_bool("daemon.skip_permissions", "SKIP_PERMISSIONS") is True
    assert config.get_bool("daemon.skip_permissions") is False
    assert config.get("telegram.missing.deeper", default="fallback") == "fallback"

    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "from_env")
    assert config.get("telegram.bot_username", "TELEGRAM_BOT_USERNAME") == "from_env"
