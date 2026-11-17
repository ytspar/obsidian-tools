"""Configuration loading for the Instapaper -> Obsidian sync."""

from __future__ import annotations

import json
import os

DEFAULTS = {
    "vault_path": "",
    "notes_dir": "Instapaper",
    "layout": "folders",
    "to_read_dir": "To Read",
    "archive_dir": "Archived",
    "sections": ["home", "archive", "folders"],
    "fetch_article_text": True,
    "fetch_highlights": False,
    "include_images": True,
    "page_size": 500,
    "request_delay": 0.3,
    "workers": 4,
    "chunk_size": 250,
    "timeout": 120,
    "parse_timeout": 20,
    "filename": {"template": "{title}", "max_length": 120},
    "frontmatter": {
        "extra": {},
        "include_description": True,
        "include_instapaper_url": True,
    },
    "categorize": {
        "mode": "rules",
        "categories_file": "categories.json",
        "fallback": "Unsorted",
        # No base_url/model here on purpose: those come from the provider
        # preset in categorize.PROVIDERS, so switching provider means changing
        # one word ("provider": "deepseek") rather than remembering to clear an
        # ollama default that would otherwise silently win.
        "llm": {
            "batch_size": 12,
            "timeout": 300,
        },
    },
    "state_dir": "~/.local/state/instapaper-sync",
    "cache_dir": "~/.cache/instapaper-sync",
    "token_file": "~/.config/instapaper/token",
    "token_env": "INSTAPAPER_TOKEN",
    "delete_mode": "trash",
    "index_note": "Instapaper Index",
}


def expand(path):
    return os.path.abspath(os.path.expanduser(os.path.expandvars(path)))


def _merge(base, override):
    if not isinstance(override, dict):
        return override
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            result[key] = _merge(base[key], value)
        else:
            result[key] = value
    return result


class Config:
    def __init__(self, data, path=None):
        self.data = data
        self.path = path

    def __getitem__(self, key):
        return self.data[key]

    def get(self, key, default=None):
        return self.data.get(key, default)

    # ------------------------------------------------------------------ helpers

    @property
    def vault_path(self):
        return expand(self.data["vault_path"])

    @property
    def notes_root(self):
        return os.path.join(self.vault_path, self.data["notes_dir"].strip("/"))

    @property
    def state_dir(self):
        return expand(self.data["state_dir"])

    @property
    def cache_dir(self):
        return expand(self.data["cache_dir"])

    def resolve(self, path, base=None):
        """Resolve a config-relative path against the config file's directory."""
        path = os.path.expanduser(os.path.expandvars(path))
        if os.path.isabs(path):
            return os.path.abspath(path)
        base = base or (os.path.dirname(self.path) if self.path else os.getcwd())
        return os.path.abspath(os.path.join(base, path))

    def validate(self):
        missing = []
        if not self.data["vault_path"]:
            missing.append("vault_path")
        if missing:
            raise ValueError("config is missing required keys: " + ", ".join(missing))
        if not os.path.isdir(self.vault_path):
            raise ValueError(f"vault_path is not a directory: {self.vault_path}")
        if self.data["layout"] not in ("folders", "flat"):
            raise ValueError("layout must be 'folders' or 'flat'")


def load(path="config.json"):
    path = expand(path)
    if not os.path.isfile(path):
        raise FileNotFoundError(
            f"no config at {path}; copy config.example.json to {path} and edit it"
        )
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    config = Config(_merge(DEFAULTS, data), path)
    return config


def load_token(config, explicit=None, allow_plugin=True):
    """Find an API token: explicit value, env var, token file, then the Obsidian plugin."""
    if explicit:
        return explicit.strip()

    env_name = config.get("token_env")
    if env_name and os.environ.get(env_name):
        return os.environ[env_name].strip()

    token_file = config.get("token_file")
    if token_file:
        candidate = expand(token_file)
        if os.path.isfile(candidate):
            with open(candidate, "r", encoding="utf-8") as handle:
                text = handle.read().strip()
            if text:
                return text

    if allow_plugin:
        plugin = os.path.join(
            config.vault_path, ".obsidian", "plugins", "instapaper", "data.json"
        )
        if os.path.isfile(plugin):
            try:
                with open(plugin, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
            except (OSError, ValueError):
                data = {}
            token = data.get("token")
            if isinstance(token, dict):
                token = token.get("key")
            if isinstance(token, str) and token.strip():
                return token.strip()

    raise RuntimeError(
        "no Instapaper token found. Create one at "
        "https://www.instapaper.com/developers/applications (New App -> Generate access token), "
        f"then save it to {expand(config.get('token_file') or '~/.config/instapaper/token')} "
        f"or export {config.get('token_env') or 'INSTAPAPER_TOKEN'}."
    )
