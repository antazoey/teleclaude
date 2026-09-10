"""teleclaude settings: a TOML file, with environment variables taking precedence."""

import os
import tomllib
from pathlib import Path

DEFAULT_CONFIG_PATH = Path.home() / ".config/teleclaude/config.toml"
TRUE_WORDS = ("1", "true", "yes", "on")


def resolve_config_path():
    return Path(os.environ.get("TELECLAUDE_CONFIG") or DEFAULT_CONFIG_PATH).expanduser()


class Config:
    """Settings looked up by dotted key, e.g. `telegram.bot_token`."""

    def __init__(self, table=None):
        self.table = table or {}

    @classmethod
    def load(cls, path=None):
        config_path = Path(path) if path else resolve_config_path()
        if not config_path.exists():
            return cls()

        return cls(tomllib.loads(config_path.read_text()))

    def get(self, key, *env_names, default=None):
        """The first set environment variable in `env_names`, else the file's value, else `default`."""
        for name in env_names:
            if os.environ.get(name):
                return os.environ[name]

        node = self.table
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default

            node = node[part]

        return node

    def get_bool(self, key, *env_names, default=False):
        value = self.get(key, *env_names, default=default)
        return value if isinstance(value, bool) else str(value).strip().lower() in TRUE_WORDS

    def get_path(self, key, *env_names, default=None):
        value = self.get(key, *env_names, default=default)
        return Path(value).expanduser() if value else None
