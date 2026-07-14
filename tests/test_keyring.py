"""Keyring credential backend: storage round-trip, resolution precedence,
CLI login/logout, and graceful fallback when no secret store exists."""

import keyring
import pytest
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError

from pikvm_agent import config as config_mod
from pikvm_agent.cli import main


class MemoryKeyring(KeyringBackend):
    """An in-process keyring backend standing in for the OS secret store."""
    priority = 1

    def __init__(self):
        super().__init__()
        self._store: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self._store.get((service, username))

    def set_password(self, service, username, password):
        self._store[(service, username)] = password

    def delete_password(self, service, username):
        if (service, username) not in self._store:
            raise PasswordDeleteError("not found")
        del self._store[(service, username)]


@pytest.fixture
def mem_keyring(monkeypatch, tmp_path):
    prev = keyring.get_keyring()
    mem = MemoryKeyring()
    keyring.set_keyring(mem)
    monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "config.toml")
    for var in ("PIKVM_HOST", "PIKVM_USER", "PIKVM_PASSWORD", "PIKVM_SSL",
                "PIKVM_PORT", "PIKVM_MOCK"):
        monkeypatch.delenv(var, raising=False)
    yield mem
    keyring.set_keyring(prev)


def _write_toml(monkeypatch, tmp_path, body: str):
    path = tmp_path / "config.toml"
    path.write_text(body)
    monkeypatch.setattr(config_mod, "CONFIG_PATH", path)
    return path


# -- storage round-trip --------------------------------------------------------

def test_store_get_delete(mem_keyring):
    assert config_mod.get_stored_password("kvm.local", "admin") is None
    config_mod.set_stored_password("kvm.local", "admin", "s3cret")
    assert config_mod.get_stored_password("kvm.local", "admin") == "s3cret"
    assert config_mod.delete_stored_password("kvm.local", "admin") is True
    assert config_mod.get_stored_password("kvm.local", "admin") is None
    # deleting a missing entry is a no-op, not an error
    assert config_mod.delete_stored_password("kvm.local", "admin") is False


def test_keyed_by_user_and_host(mem_keyring):
    config_mod.set_stored_password("h1", "admin", "a")
    config_mod.set_stored_password("h1", "bob", "b")
    config_mod.set_stored_password("h2", "admin", "c")
    assert config_mod.get_stored_password("h1", "admin") == "a"
    assert config_mod.get_stored_password("h1", "bob") == "b"
    assert config_mod.get_stored_password("h2", "admin") == "c"


# -- resolution precedence -----------------------------------------------------

def test_load_config_uses_keyring_when_toml_omits_password(mem_keyring, monkeypatch, tmp_path):
    _write_toml(monkeypatch, tmp_path, 'host = "kvm.local"\nuser = "admin"\n')
    config_mod.set_stored_password("kvm.local", "admin", "from-keyring")
    cfg = config_mod.load_config()
    assert cfg.password == "from-keyring"
    assert cfg.auth_headers["X-KVMD-Passwd"] == "from-keyring"


def test_toml_password_beats_keyring(mem_keyring, monkeypatch, tmp_path):
    _write_toml(monkeypatch, tmp_path,
                'host = "kvm.local"\nuser = "admin"\npassword = "from-file"\n')
    config_mod.set_stored_password("kvm.local", "admin", "from-keyring")
    assert config_mod.load_config().password == "from-file"


def test_env_password_beats_everything(mem_keyring, monkeypatch, tmp_path):
    _write_toml(monkeypatch, tmp_path,
                'host = "kvm.local"\nuser = "admin"\npassword = "from-file"\n')
    config_mod.set_stored_password("kvm.local", "admin", "from-keyring")
    monkeypatch.setenv("PIKVM_PASSWORD", "from-env")
    assert config_mod.load_config().password == "from-env"


def test_env_host_uses_keyring(mem_keyring, monkeypatch):
    monkeypatch.setenv("PIKVM_HOST", "10.0.0.9")
    monkeypatch.setenv("PIKVM_USER", "operator")
    config_mod.set_stored_password("10.0.0.9", "operator", "kr-pass")
    cfg = config_mod.load_config()
    assert cfg.host == "10.0.0.9" and cfg.user == "operator"
    assert cfg.password == "kr-pass"


def test_missing_password_everywhere_errors(mem_keyring, monkeypatch, tmp_path):
    _write_toml(monkeypatch, tmp_path, 'host = "kvm.local"\nuser = "admin"\n')
    with pytest.raises(config_mod.ConfigError, match="No password"):
        config_mod.load_config()


# -- graceful fallback when no secret store exists -----------------------------

def test_fallback_when_keyring_module_missing(monkeypatch, tmp_path):
    _write_toml(monkeypatch, tmp_path,
                'host = "kvm.local"\npassword = "file-pw"\n')
    monkeypatch.setattr(config_mod, "keyring", None)
    monkeypatch.delenv("PIKVM_PASSWORD", raising=False)
    # get returns None, set raises a clear error, but the TOML password still works
    assert config_mod.get_stored_password("kvm.local", "admin") is None
    with pytest.raises(config_mod.ConfigError, match="not installed"):
        config_mod.set_stored_password("kvm.local", "admin", "x")
    assert config_mod.load_config().password == "file-pw"


# -- CLI login / logout --------------------------------------------------------

def test_cli_login_stores_and_logout_removes(mem_keyring, monkeypatch, tmp_path):
    _write_toml(monkeypatch, tmp_path, 'host = "kvm.local"\nuser = "admin"\n')
    rc = main(["login", "--password", "cli-pw", "--no-verify"])
    assert rc == 0
    assert config_mod.get_stored_password("kvm.local", "admin") == "cli-pw"
    # the stored password is now what load_config picks up
    assert config_mod.load_config().password == "cli-pw"
    rc = main(["logout"])
    assert rc == 0
    assert config_mod.get_stored_password("kvm.local", "admin") is None


def test_cli_login_explicit_host_user(mem_keyring):
    rc = main(["login", "--host", "1.2.3.4", "--user", "svc",
               "--password", "p", "--no-verify"])
    assert rc == 0
    assert config_mod.get_stored_password("1.2.3.4", "svc") == "p"
