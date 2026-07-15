"""Multi-profile config, profile selection, and registry import."""

import json

import keyring
import pytest

from pikvm_agent import config as config_mod
from pikvm_agent.cli import main
from tests.test_keyring import MemoryKeyring


@pytest.fixture
def cfg(monkeypatch, tmp_path):
    prev = keyring.get_keyring()
    keyring.set_keyring(MemoryKeyring())
    monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "config.toml")
    for var in ("PIKVM_HOST", "PIKVM_USER", "PIKVM_PASSWORD", "PIKVM_PROFILE", "PIKVM_MOCK"):
        monkeypatch.delenv(var, raising=False)
    yield tmp_path / "config.toml"
    keyring.set_keyring(prev)


def _write(path, body):
    path.write_text(body)


# -- reading profiles ----------------------------------------------------------

def test_legacy_flat_config_reads_as_default_profile(cfg):
    _write(cfg, 'host = "10.0.0.1"\nuser = "admin"\n')
    profiles = config_mod.list_profiles()
    assert set(profiles) == {"default"}
    assert profiles["default"].host == "10.0.0.1"


def test_named_profiles(cfg):
    _write(cfg, '''
default = "my_desktop"
[profiles.my_desktop]
host = "192.168.1.50"
user = "mr_robot"
[profiles.lab]
host = "10.0.0.5"
''')
    profiles = config_mod.list_profiles()
    assert set(profiles) == {"my_desktop", "lab"}
    assert config_mod.default_profile_name() == "my_desktop"
    assert config_mod.resolve_endpoint().host == "192.168.1.50"          # default
    assert config_mod.resolve_endpoint("lab").host == "10.0.0.5"         # explicit


def test_profile_selection_precedence(cfg, monkeypatch):
    _write(cfg, '''
default = "my_desktop"
[profiles.my_desktop]
host = "1.1.1.1"
[profiles.lab]
host = "2.2.2.2"
''')
    monkeypatch.setenv("PIKVM_PROFILE", "lab")
    assert config_mod.resolve_endpoint().host == "2.2.2.2"               # env beats default
    assert config_mod.resolve_endpoint("my_desktop").host == "1.1.1.1"         # arg beats env


def test_ambiguous_without_default_errors(cfg):
    _write(cfg, '[profiles.a]\nhost="1.1.1.1"\n[profiles.b]\nhost="2.2.2.2"\n')
    with pytest.raises(config_mod.ConfigError, match="Multiple profiles"):
        config_mod.resolve_endpoint()


def test_unknown_profile_errors(cfg):
    _write(cfg, '[profiles.a]\nhost="1.1.1.1"\n')
    with pytest.raises(config_mod.ConfigError, match="no profile"):
        config_mod.resolve_endpoint("nope")


# -- writing profiles ----------------------------------------------------------

def test_save_profile_roundtrip(cfg):
    ep = config_mod.Endpoint(host="5.5.5.5", user="op", use_ssl=False, port=8080)
    config_mod.save_profile("edge", ep, make_default=True)
    profiles = config_mod.list_profiles()
    assert profiles["edge"].host == "5.5.5.5"
    assert profiles["edge"].use_ssl is False and profiles["edge"].port == 8080
    assert config_mod.default_profile_name() == "edge"
    # file is chmod 600 and carries no password
    assert (cfg.stat().st_mode & 0o777) == 0o600
    assert "password" not in cfg.read_text()


def test_save_profile_migrates_legacy_flat(cfg):
    _write(cfg, 'host = "old.host"\nuser = "admin"\n')
    config_mod.save_profile("new", config_mod.Endpoint(host="new.host", user="admin"))
    profiles = config_mod.list_profiles()
    assert set(profiles) == {"default", "new"}   # legacy became "default"
    assert profiles["default"].host == "old.host"


# -- registry import -----------------------------------------------------------

def test_import_registry(cfg, tmp_path):
    reg = tmp_path / "reg.json"
    reg.write_text(json.dumps({
        "version": 1, "default": "my_desktop",
        "profiles": [
            {"name": "my_desktop", "host": "192.168.1.50", "user": "mr_robot",
             "use_ssl": True, "password": "sekret"},
            {"name": "lab", "host": "10.0.0.5", "user": "admin"},
        ],
    }))
    names = config_mod.import_registry(reg)
    assert set(names) == {"my_desktop", "lab"}
    assert config_mod.default_profile_name() == "my_desktop"
    # bundled password landed in the keyring and resolves via load_config
    cfg_obj = config_mod.load_config(profile="my_desktop")
    assert cfg_obj.password == "sekret" and cfg_obj.host == "192.168.1.50"


def test_cli_profiles_and_import(cfg, tmp_path, capsys):
    reg = tmp_path / "reg.json"
    reg.write_text(json.dumps({"profiles": [{"name": "a", "host": "1.2.3.4"}]}))
    assert main(["import", str(reg)]) == 0
    assert main(["profiles"]) == 0
    out = capsys.readouterr().out
    assert "a" in out and "1.2.3.4" in out
