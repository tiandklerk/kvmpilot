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

import hashlib
import hmac
import json
import os
import re
import secrets
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
ENROLL_TOKEN_PATH = STATE_DIR / "enroll_token.json"

_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
MAX_ENROLL_PROFILES = 50


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


# -- profiles (multiple PiKVMs) -----------------------------------------------
#
# The TOML file holds one or more named profiles plus an optional default:
#
#   default = "my_desktop"
#   [profiles.my_desktop]
#   host = "192.168.1.50"
#   user = "admin"
#   [profiles.lab]
#   host = "10.0.0.5"
#
# A legacy flat file (top-level `host = ...`) is read as a single profile
# named "default". Passwords live in the keyring (keyed per user@host), so one
# keyring works for every profile.

def _read_toml() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    return tomllib.loads(CONFIG_PATH.read_text())


def _endpoint_from_table(table: dict) -> Endpoint:
    if "host" not in table:
        raise ConfigError(f"{CONFIG_PATH}: profile missing required key 'host'")
    return Endpoint(
        host=table["host"],
        user=table.get("user", "admin"),
        use_ssl=table.get("use_ssl", True),
        port=table.get("port"),
        file_password=table.get("password"),
    )


def list_profiles() -> dict[str, Endpoint]:
    """All configured profiles (name -> Endpoint), from the TOML file."""
    data = _read_toml()
    profiles: dict[str, Endpoint] = {}
    if isinstance(data.get("profiles"), dict):
        for name, table in data["profiles"].items():
            profiles[name] = _endpoint_from_table(table)
    elif "host" in data:  # legacy flat form
        profiles["default"] = _endpoint_from_table(data)
    return profiles


def default_profile_name() -> str | None:
    data = _read_toml()
    if data.get("default"):
        return data["default"]
    profiles = list_profiles()
    return next(iter(profiles)) if len(profiles) == 1 else None


def resolve_endpoint(profile: str | None = None) -> Endpoint:
    """Connection target (no password) — env vars, or a named TOML profile.

    Selection: PIKVM_HOST env short-circuits to a single env device; otherwise
    `profile` arg > PIKVM_PROFILE env > the file's `default` > the sole profile.
    """
    env_host = os.environ.get("PIKVM_HOST")
    if env_host:
        return Endpoint(
            host=env_host,
            user=os.environ.get("PIKVM_USER", "admin"),
            use_ssl=os.environ.get("PIKVM_SSL", "1") != "0",
            port=int(os.environ["PIKVM_PORT"]) if os.environ.get("PIKVM_PORT") else None,
        )

    profiles = list_profiles()
    if not profiles:
        raise ConfigError(
            "No PiKVM configured. Run `pikvm setup`, set PIKVM_HOST, or use --mock."
        )
    name = profile or os.environ.get("PIKVM_PROFILE") or default_profile_name()
    if name is None:
        raise ConfigError(
            f"Multiple profiles configured ({', '.join(profiles)}); pick one with "
            "--profile NAME or set a `default` in the config."
        )
    if name not in profiles:
        raise ConfigError(f"no profile {name!r} (have: {', '.join(profiles)})")
    return profiles[name]


def load_config(mock: bool = False, profile: str | None = None) -> Config:
    if mock or os.environ.get("PIKVM_MOCK") == "1":
        return Config(
            host=os.environ.get("PIKVM_MOCK_HOST", MOCK_HOST),
            port=int(os.environ.get("PIKVM_MOCK_PORT", MOCK_PORT)),
            user="admin",
            password="admin",
            use_ssl=False,
        )

    ep = resolve_endpoint(profile)
    # password precedence: env override > plaintext TOML > OS keyring
    password = (
        os.environ.get("PIKVM_PASSWORD")
        or ep.file_password
        or get_stored_password(ep.host, ep.user)
    )
    if not password:
        raise ConfigError(
            f"No password for {ep.user}@{ep.host}. Store one securely with "
            "`pikvm login`, set PIKVM_PASSWORD, or add password=... to the profile."
        )
    return Config(host=ep.host, user=ep.user, password=password, use_ssl=ep.use_ssl, port=ep.port)


# -- writing config (setup wizard, import) ------------------------------------

def save_profile(name: str, ep: Endpoint, make_default: bool = False) -> None:
    """Add or update a named profile in the TOML file (secrets go to keyring)."""
    import tomli_w

    data = _read_toml()
    # migrate a legacy flat file into the profiles table
    if "host" in data and "profiles" not in data:
        legacy = {k: data.pop(k) for k in ("host", "user", "use_ssl", "port", "password")
                  if k in data}
        data.setdefault("profiles", {})["default"] = legacy
    data.setdefault("profiles", {})
    table = {"host": ep.host, "user": ep.user, "use_ssl": ep.use_ssl}
    if ep.port:
        table["port"] = ep.port
    data["profiles"][name] = table
    if make_default or "default" not in data:
        data["default"] = name
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        "# pikvm-agent config — profiles for one or more PiKVMs.\n"
        "# Passwords are NOT stored here; use `pikvm login`.\n"
        + tomli_w.dumps(data)
    )
    CONFIG_PATH.chmod(0o600)


def import_registry_doc(doc: dict) -> list[str]:
    """Ingest a pikonnect-style registry (already parsed). Schema:
        {"version":1, "default":"name",
         "profiles":[{"name","host","user"?,"use_ssl"?,"port"?,"password"?}]}
    Validates untrusted input (this also backs the network enrollment endpoint):
    endpoints are written to the TOML; included passwords go to the keyring.
    Returns the names imported.
    """
    if not isinstance(doc, dict):
        raise ConfigError("registry must be a JSON object")
    entries = doc.get("profiles")
    if not isinstance(entries, list) or not entries:
        raise ConfigError("registry needs a non-empty 'profiles' array")
    if len(entries) > MAX_ENROLL_PROFILES:
        raise ConfigError(f"too many profiles (limit {MAX_ENROLL_PROFILES})")
    default = doc.get("default")
    imported: list[str] = []
    for e in entries:
        if not isinstance(e, dict) or "name" not in e or "host" not in e:
            raise ConfigError("each profile needs at least 'name' and 'host'")
        name = str(e["name"])
        if not _PROFILE_NAME_RE.match(name):
            raise ConfigError(f"invalid profile name {name!r} (use letters, digits, . _ -)")
        try:
            port = int(e["port"]) if e.get("port") else None
        except (TypeError, ValueError):
            raise ConfigError(f"profile {name!r}: 'port' must be a number") from None
        ep = Endpoint(host=str(e["host"]), user=str(e.get("user", "admin")),
                      use_ssl=bool(e.get("use_ssl", True)), port=port)
        save_profile(name, ep, make_default=(name == default))
        if e.get("password") and keyring is not None:
            try:
                set_stored_password(ep.host, ep.user, str(e["password"]))
            except ConfigError:
                pass  # no keyring here; endpoint still imported, user runs `pikvm login`
        imported.append(name)
    return imported


def import_registry(path: Path) -> list[str]:
    """Ingest a registry export from a file (see import_registry_doc)."""
    return import_registry_doc(json.loads(Path(path).read_text()))


# -- enrollment pairing token -------------------------------------------------
#
# The network enrollment endpoint (enroll.py) is credential-bearing, so it
# requires a Bearer token. Only the token's hash is stored on disk.

def create_pairing_token() -> str:
    token = secrets.token_urlsafe(24)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    ENROLL_TOKEN_PATH.write_text(
        json.dumps({"sha256": hashlib.sha256(token.encode()).hexdigest()}))
    ENROLL_TOKEN_PATH.chmod(0o600)
    return token


def pairing_token_exists() -> bool:
    return ENROLL_TOKEN_PATH.exists()


def verify_pairing_token(presented: str | None) -> bool:
    if not presented or not ENROLL_TOKEN_PATH.exists():
        return False
    try:
        want = json.loads(ENROLL_TOKEN_PATH.read_text())["sha256"]
    except (ValueError, OSError, KeyError):
        return False
    got = hashlib.sha256(presented.encode()).hexdigest()
    return hmac.compare_digest(want, got)


def clear_pairing_token() -> bool:
    if ENROLL_TOKEN_PATH.exists():
        ENROLL_TOKEN_PATH.unlink()
        return True
    return False
