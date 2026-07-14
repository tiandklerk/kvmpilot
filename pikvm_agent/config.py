"""Connection configuration.

Precedence: explicit CLI overrides > PIKVM_* environment variables >
~/.config/pikvm-agent/config.toml. `--mock` / PIKVM_MOCK=1 targets a local
mock kvmd (see mock/mock_kvmd.py) with its well-known defaults.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

CONFIG_PATH = Path(os.environ.get("PIKVM_CONFIG", "~/.config/pikvm-agent/config.toml")).expanduser()
STATE_DIR = Path(os.environ.get("PIKVM_STATE_DIR", "~/.local/state/pikvm-agent")).expanduser()

MOCK_HOST = "127.0.0.1"
MOCK_PORT = 8686


class ConfigError(Exception):
    pass


@dataclass
class Config:
    host: str
    user: str
    password: str
    use_ssl: bool = True
    port: int | None = None  # None -> scheme default

    @property
    def base_url(self) -> str:
        scheme = "https" if self.use_ssl else "http"
        default_port = 443 if self.use_ssl else 80
        port = self.port or default_port
        port_part = "" if port == default_port else f":{port}"
        return f"{scheme}://{self.host}{port_part}"

    @property
    def ws_url(self) -> str:
        scheme = "wss" if self.use_ssl else "ws"
        return self.base_url.replace("https://", "wss://").replace("http://", "ws://") + "/api/ws"

    @property
    def auth_headers(self) -> dict[str, str]:
        return {"X-KVMD-User": self.user, "X-KVMD-Passwd": self.password}


def load_config(mock: bool = False) -> Config:
    if mock or os.environ.get("PIKVM_MOCK") == "1":
        return Config(
            host=os.environ.get("PIKVM_MOCK_HOST", MOCK_HOST),
            port=int(os.environ.get("PIKVM_MOCK_PORT", MOCK_PORT)),
            user="admin",
            password="admin",
            use_ssl=False,
        )

    env_host = os.environ.get("PIKVM_HOST")
    if env_host:
        return Config(
            host=env_host,
            user=os.environ.get("PIKVM_USER", "admin"),
            password=os.environ.get("PIKVM_PASSWORD", ""),
            use_ssl=os.environ.get("PIKVM_SSL", "1") != "0",
            port=int(os.environ["PIKVM_PORT"]) if os.environ.get("PIKVM_PORT") else None,
        )

    if CONFIG_PATH.exists():
        data = tomllib.loads(CONFIG_PATH.read_text())
        try:
            return Config(
                host=data["host"],
                user=data.get("user", "admin"),
                password=data["password"],
                use_ssl=data.get("use_ssl", True),
                port=data.get("port"),
            )
        except KeyError as e:
            raise ConfigError(f"{CONFIG_PATH}: missing required key {e}") from None

    raise ConfigError(
        "No PiKVM configured. Set PIKVM_HOST/PIKVM_USER/PIKVM_PASSWORD, "
        f"create {CONFIG_PATH} (keys: host, user, password, use_ssl, port), "
        "or use --mock against the local mock device."
    )
