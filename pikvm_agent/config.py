"""Connection configuration.

Connection endpoint (host/user/ssl/port) comes from, in precedence order,
`--mock` > PIKVM_* environment variables > ~/.config/pikvm-agent/config.toml.

The password is resolved separately, so it need not sit in plaintext:
    PIKVM_PASSWORD env var  >  `password` in the TOML file  >  OS keyring
The keyring path (GNOME Keyring / KWallet / macOS Keychain / Windows
Credential Manager, via the `keyring` package) is the recommended store —
run `pikvm login` to save a password there and omit it from the TOML.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

try:
    import keyring
    import keyring.errors
except ImportError:  # keyring is an optional convenience; env/TOML still work
    keyring = None

CONFIG_PATH = Path(os.environ.get("PIKVM_CONFIG", "~/.config/pikvm-agent/config.toml")).expanduser()
STATE_DIR = Path(os.environ.get("PIKVM_STATE_DIR", "~/.local/state/pikvm-agent")).expanduser()

MOCK_HOST = "127.0.0.1"
MOCK_PORT = 8686

KEYRING_SERVICE = "pikvm-agent"


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


@dataclass
class Endpoint:
    """Connection target without the secret — used to look up / store a password."""
    host: str
    user: str
    use_ssl: bool = True
    port: int | None = None
    file_password: str | None = None  # a plaintext password found in the TOML, if any


# -- keyring helpers ----------------------------------------------------------

def keyring_available() -> bool:
    return keyring is not None


def _keyring_id(host: str, user: str) -> str:
    return f"{user}@{host}"


def get_stored_password(host: str, user: str) -> str | None:
    """Fetch the password from the OS keyring, or None if unavailable/unset."""
    if keyring is None:
        return None
    try:
        return keyring.get_password(KEYRING_SERVICE, _keyring_id(host, user))
    except keyring.errors.KeyringError:
        return None


def set_stored_password(host: str, user: str, password: str) -> None:
    if keyring is None:
        raise ConfigError(
            "The 'keyring' package is not installed, so no OS secret store is available. "
            "Install it (pip install keyring) or use PIKVM_PASSWORD / the TOML file."
        )
    try:
        keyring.set_password(KEYRING_SERVICE, _keyring_id(host, user), password)
    except keyring.errors.KeyringError as e:
        raise ConfigError(
            f"could not store the password in the OS keyring ({e}). No working secret "
            "store is reachable — ensure a keyring daemon (gnome-keyring / KWallet, "
            "providing org.freedesktop.secrets) is running and unlocked, or fall back "
            "to PIKVM_PASSWORD or a password in the TOML file."
        ) from None


def delete_stored_password(host: str, user: str) -> bool:
    """Remove a keyring entry. Returns True if one was deleted."""
    if keyring is None:
        return False
    try:
        keyring.delete_password(KEYRING_SERVICE, _keyring_id(host, user))
        return True
    except keyring.errors.PasswordDeleteError:
        return False
    except keyring.errors.KeyringError:
        return False


# -- endpoint + config resolution ---------------------------------------------

def resolve_endpoint() -> Endpoint:
    """Connection target (no password required) from env vars or the TOML file."""
    env_host = os.environ.get("PIKVM_HOST")
    if env_host:
        return Endpoint(
            host=env_host,
            user=os.environ.get("PIKVM_USER", "admin"),
            use_ssl=os.environ.get("PIKVM_SSL", "1") != "0",
            port=int(os.environ["PIKVM_PORT"]) if os.environ.get("PIKVM_PORT") else None,
            file_password=None,
        )
    if CONFIG_PATH.exists():
        data = tomllib.loads(CONFIG_PATH.read_text())
        if "host" not in data:
            raise ConfigError(f"{CONFIG_PATH}: missing required key 'host'")
        return Endpoint(
            host=data["host"],
            user=data.get("user", "admin"),
            use_ssl=data.get("use_ssl", True),
            port=data.get("port"),
            file_password=data.get("password"),
        )
    raise ConfigError(
        "No PiKVM configured. Set PIKVM_HOST (with PIKVM_USER), "
        f"create {CONFIG_PATH} (keys: host, user, use_ssl, port), "
        "or use --mock against the local mock device."
    )


def load_config(mock: bool = False) -> Config:
    if mock or os.environ.get("PIKVM_MOCK") == "1":
        return Config(
            host=os.environ.get("PIKVM_MOCK_HOST", MOCK_HOST),
            port=int(os.environ.get("PIKVM_MOCK_PORT", MOCK_PORT)),
            user="admin",
            password="admin",
            use_ssl=False,
        )

    ep = resolve_endpoint()
    # password precedence: env override > plaintext TOML > OS keyring
    password = (
        os.environ.get("PIKVM_PASSWORD")
        or ep.file_password
        or get_stored_password(ep.host, ep.user)
    )
    if not password:
        raise ConfigError(
            f"No password for {ep.user}@{ep.host}. Store one securely in the OS keyring "
            "with `pikvm login`, set PIKVM_PASSWORD, or add password=... to "
            f"{CONFIG_PATH}."
        )
    return Config(host=ep.host, user=ep.user, password=password, use_ssl=ep.use_ssl, port=ep.port)
