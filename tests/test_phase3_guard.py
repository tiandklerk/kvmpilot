"""Phase 3: dangerous-pattern detection, confirm-token lifecycle, Enter
evasion, ATX gating, audit log."""

import json
import re
import time

import pytest

from pikvm_agent import config, guard, hid
from pikvm_agent.cli import main
from pikvm_agent.client import PiKvmClient
from pikvm_agent.config import load_config


# -- pure detection ------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "sudo reboot", "shutdown -h now", "rm -rf /", "sudo rm -rf ~",
    "systemctl poweroff", "dd if=/dev/zero of=/dev/sda", "mkfs.ext4 /dev/sdb1",
    "Stop-Computer", "Remove-Item -Recurse -Force C:\\", "format c:", "taskkill /f /im x",
])
def test_dangerous_text_detected(text):
    assert guard.scan_text(text) is not None


@pytest.mark.parametrize("text", [
    "ls -la", "cd /home", "cat notes.txt", "git commit -m fix",
    "python app.py", "docker ps -a", "systemctl status my_desktop",
])
def test_benign_text_allowed(text):
    assert guard.scan_text(text) is None


def test_guard_errs_toward_blocking_trigger_words():
    # Deliberately conservative: any occurrence of a trigger word blocks,
    # even in a benign sentence. Better a false prompt than a missed shutdown.
    assert guard.scan_text("echo reboot the plan tomorrow") is not None


def test_dangerous_combo_detected():
    assert guard.scan_combo(["ControlLeft", "AltLeft", "Delete"]) is not None
    assert guard.scan_combo(["ControlLeft", "KeyS"]) is None


# -- token lifecycle -----------------------------------------------------------

def test_require_confirmation_blocks_then_confirms(mock_device):
    with pytest.raises(guard.GuardError) as ei:
        guard.require_confirmation("type:sudo reboot", "reboot command", None)
    token = ei.value.token
    # wrong token still blocks
    with pytest.raises(guard.GuardError):
        guard.require_confirmation("type:sudo reboot", "reboot command", "000000")
    # correct token passes (no raise)
    guard.require_confirmation("type:sudo reboot", "reboot command", token)
    # token is single-use: gone now
    with pytest.raises(guard.GuardError):
        guard.require_confirmation("type:sudo reboot", "reboot command", token)


def test_pending_state_never_holds_the_plaintext_token(mock_device):
    with pytest.raises(guard.GuardError) as ei:
        guard.require_confirmation("type:sudo reboot", "reboot command", None)
    token = ei.value.token
    raw = (config.STATE_DIR / "pending.json").read_text()
    assert token not in raw            # plaintext never hits disk
    assert "token_hash" in raw         # only the hash is persisted
    pending = guard.peek_pending()
    assert "token" not in pending and "token_hash" in pending


def test_pending_file_and_state_dir_are_owner_only(mock_device):
    guard.audit("noop", "executed")  # ensures the state dir exists
    assert (config.STATE_DIR.stat().st_mode & 0o777) == 0o700
    with pytest.raises(guard.GuardError):
        guard.require_confirmation("atx:reset", "reset", None)
    pending_path = config.STATE_DIR / "pending.json"
    assert (pending_path.stat().st_mode & 0o777) == 0o600


def test_retry_of_same_action_reports_pending_without_a_token(mock_device):
    with pytest.raises(guard.GuardError) as first:
        guard.require_confirmation("type:sudo reboot", "reboot command", None)
    original_token = first.value.token
    assert original_token is not None
    # retrying the SAME action while it's still pending must not mint (or try
    # to redisplay) a token — only its hash exists, so it can't be recovered
    with pytest.raises(guard.GuardError) as second:
        guard.require_confirmation("type:sudo reboot", "reboot command", None)
    assert second.value.token is None
    assert "already" in str(second.value).lower()
    # the ORIGINAL token, from the first response, still works
    guard.require_confirmation("type:sudo reboot", "reboot command", original_token)


def test_token_expiry(mock_device, monkeypatch):
    monkeypatch.setattr(guard, "TOKEN_TTL", -1)  # already expired when written
    with pytest.raises(guard.GuardError) as ei:
        guard.require_confirmation("atx:reset", "reset", None)
    with pytest.raises(guard.GuardError):
        guard.require_confirmation("atx:reset", "reset", ei.value.token)


# -- Enter evasion -------------------------------------------------------------

def test_enter_evasion_caught(mock_device):
    with PiKvmClient(load_config()) as client:
        # type a dangerous command WITHOUT a newline (no danger word split, so
        # scan passes char-safe here since "sudo reboot" IS flagged) -> use a
        # benign-looking approach: type it, which itself blocks. Force via ws
        # after confirming to seed the buffer, then Enter should re-block.
        with pytest.raises(guard.GuardError):
            hid.type_text(client, "sudo reboot")
    # Seed the rolling buffer directly as if it had been typed piecemeal.
    guard.record_typed("sudo reboot")
    danger = guard.enter_would_execute_danger()
    assert danger is not None
    with PiKvmClient(load_config()) as client:
        with pytest.raises(guard.GuardError):
            hid.press_combo(client, "enter")


def test_enter_benign_allowed(mock_device):
    guard.record_typed("ls -la")
    assert guard.enter_would_execute_danger() is None
    with PiKvmClient(load_config()) as client:
        hid.press_combo(client, "enter")  # should not raise


# -- CLI end to end ------------------------------------------------------------

def test_cli_type_blocks_and_confirm_executes(mock_device, capsys):
    rc = main(["--mock", "type", "sudo reboot"])
    assert rc == 3
    err = capsys.readouterr().err
    assert "BLOCKED" in err
    # the token is shown only in the blocked response, never persisted in
    # recoverable form (guarded against a plain read of state) — extract it
    # from the actual output, the same way a human/agent would.
    token = re.search(r"confirm ([0-9a-f]{6})", err).group(1)
    pending = guard.peek_pending()
    assert pending is not None and "token" not in pending  # only a hash is stored
    assert main(["--mock", "confirm", token]) == 0
    # the reboot text reached the mock via hid_print after confirmation
    kinds = [e.get("text") for e in mock_device.events() if e["kind"] == "hid_print"]
    assert "sudo reboot" in kinds


def test_cli_atx_guarded(mock_device, capsys):
    assert main(["--mock", "atx", "reset"]) == 3
    err = capsys.readouterr().err
    assert "BLOCKED" in err
    token = re.search(r"confirm ([0-9a-f]{6})", err).group(1)
    assert main(["--mock", "atx", "reset", "--confirm", token]) == 0
    assert any(e["kind"] == "atx_click" and e["button"] == "reset" for e in mock_device.events())


def test_audit_log_written(mock_device):
    with PiKvmClient(load_config()) as client:
        hid.type_text(client, "hello world")
        with pytest.raises(guard.GuardError):
            hid.type_text(client, "sudo reboot")
    audit = (config.STATE_DIR / "audit.log").read_text().splitlines()
    outcomes = [json.loads(line) for line in audit]
    assert any(o["outcome"] == "executed" for o in outcomes)
    assert any(o["outcome"] == "blocked" for o in outcomes)
