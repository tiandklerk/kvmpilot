"""Guardrails for dangerous actions.

A dangerous action (ATX power, a power key-chord, or typed text that
matches a destructive-command pattern) is refused until it is confirmed
with a one-time token. The token is printed for the *human* — the agent
must relay it and get the user's approval before re-invoking with it.

Every input action (allowed, blocked, or confirmed) is appended to an
audit log. A rolling buffer of recently typed characters lets us catch
the "type a dangerous command, then press Enter separately" evasion.

State lives under ~/.local/state/pikvm-agent/ (overridable via the
STATE_DIR the module reads at call time, so tests can isolate it).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from . import config

TOKEN_TTL = 600  # seconds a confirm token stays valid
TYPE_BUFFER_MAX = 200  # chars of typing history kept for Enter-evasion checks
TYPE_BUFFER_TTL = 120  # seconds before the typing buffer is considered stale


# Destructive command fragments, matched case-insensitively against typed
# text (and against the rolling buffer when Enter is pressed). Cross-platform.
DANGEROUS_PATTERNS: list[tuple[str, str]] = [
    (r"\bshutdown\b", "shutdown command"),
    (r"\breboot\b", "reboot command"),
    (r"\bpoweroff\b", "poweroff command"),
    (r"\bhalt\b", "halt command"),
    (r"\binit\s+[06]\b", "init 0/6 (halt/reboot)"),
    (r"systemctl\s+(poweroff|reboot|halt|suspend)", "systemctl power command"),
    (r"\brm\s+-[a-z]*r[a-z]*f|\brm\s+-[a-z]*f[a-z]*r", "rm -rf (recursive force delete)"),
    (r"\bsudo\s+rm\b", "sudo rm"),
    (r"\bmkfs\b", "mkfs (format filesystem)"),
    (r"\bdd\s+if=", "dd (raw disk write)"),
    (r"\bshred\b", "shred (secure delete)"),
    (r":\s*>\s*/", "truncate to a root path"),
    (r"\bfdisk\b|\bdiskpart\b|\bparted\b", "disk partitioning tool"),
    (r"\bdel\s+/[sq]", "Windows del /s or /q"),
    (r"\br(?:d|mdir)\s+/s", "Windows rmdir /s"),
    (r"\bformat\s+[a-z]:", "Windows format drive"),
    (r"Remove-Item.*-Recurse", "PowerShell Remove-Item -Recurse"),
    (r"\bStop-Computer\b", "PowerShell Stop-Computer"),
    (r"\bRestart-Computer\b", "PowerShell Restart-Computer"),
    (r"\bkillall\b", "killall"),
    (r"\btaskkill\s+/f", "taskkill /f"),
]

# Key combos that are power/interrupt chords regardless of context.
DANGEROUS_COMBOS = [
    frozenset({"ControlLeft", "AltLeft", "Delete"}),
    frozenset({"ControlRight", "AltRight", "Delete"}),
]


class GuardError(Exception):
    """Raised when an action is blocked pending confirmation. `token` is None
    when a confirmation is already pending for this exact action — see
    require_confirmation() for why it can't be redisplayed in that case."""

    def __init__(self, message: str, token: str | None, reason: str):
        super().__init__(message)
        self.token = token
        self.reason = reason


@dataclass
class Danger:
    reason: str  # human-readable description


def _state_dir() -> Path:
    d = config.STATE_DIR
    d.mkdir(parents=True, exist_ok=True)
    try:
        d.chmod(0o700)  # owner-only: pending.json/audit.log live here
    except OSError:
        pass
    return d


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _pending_path() -> Path:
    return _state_dir() / "pending.json"


def _buffer_path() -> Path:
    return _state_dir() / "type_buffer.json"


def _audit_path() -> Path:
    return _state_dir() / "audit.log"


# -- detection ----------------------------------------------------------------

def scan_text(text: str) -> Danger | None:
    for pattern, reason in DANGEROUS_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return Danger(reason)
    return None


def scan_combo(keys: list[str]) -> Danger | None:
    key_set = frozenset(keys)
    for combo in DANGEROUS_COMBOS:
        if combo <= key_set:
            return Danger("Ctrl+Alt+Del power chord")
    return None


# -- rolling typed-character buffer (Enter-evasion) ---------------------------

def record_typed(text: str) -> None:
    """Append typed characters to the rolling buffer used to catch a
    dangerous command that is typed first and executed by a later Enter."""
    buf = _read_buffer()
    combined = (buf + text)[-TYPE_BUFFER_MAX:]
    _buffer_path().write_text(json.dumps({"ts": time.time(), "buf": combined}))


def _read_buffer() -> str:
    path = _buffer_path()
    if not path.exists():
        return ""
    try:
        data = json.loads(path.read_text())
    except (ValueError, OSError):
        return ""
    if time.time() - data.get("ts", 0) > TYPE_BUFFER_TTL:
        return ""
    return data.get("buf", "")


def clear_typed_buffer() -> None:
    _buffer_path().unlink(missing_ok=True)


def enter_would_execute_danger() -> Danger | None:
    """Called before an Enter/Return keypress: is the recently typed text a
    dangerous command that Enter is about to execute?"""
    buf = _read_buffer()
    if not buf:
        return None
    # only the current line matters for a command prompt
    last_line = buf.rsplit("\n", 1)[-1]
    danger = scan_text(last_line)
    if danger:
        return Danger(f"Enter would execute: {danger.reason}")
    return None


# -- confirm-token lifecycle --------------------------------------------------

def require_confirmation(action: str, reason: str, supplied_token: str | None) -> None:
    """Gate a dangerous action. If `supplied_token` matches a live pending
    token for this action, consume it and return (allowed). Otherwise issue
    a new token, persist only its hash, audit the block, and raise GuardError.

    The raw token is returned to the caller here and NEVER written to disk —
    only its sha256 is persisted (mirrors the enrollment pairing token), so a
    plain read of the state file (another local user, a backup, a monitoring
    agent scraping state dirs) cannot recover it. This means a genuine retry of
    the SAME action while a token is already pending cannot redisplay that
    token (we no longer have the plaintext to show) — it instead reports that
    one is already pending, without inventing a new one that would silently
    invalidate what the human already has.
    """
    if supplied_token and _consume_token(action, supplied_token):
        audit(action, "confirmed", reason)
        return

    existing = peek_pending()
    if existing and existing.get("action") == action:
        audit(action, "blocked", reason)
        remaining = max(0, int(existing["expires"] - time.time()))
        raise GuardError(
            f"BLOCKED: {reason}. A confirmation token was already issued for "
            f"this exact action and is still valid for {remaining}s — relay "
            "the token from that earlier response; it isn't re-shown here.",
            token=None,
            reason=reason,
        )

    token = secrets.token_hex(3)  # 6 hex chars — shown here once, never persisted
    _write_pending(action, reason, token)
    audit(action, "blocked", reason)
    raise GuardError(
        f"BLOCKED: {reason}. This action is dangerous and needs confirmation.",
        token=token,
        reason=reason,
    )


def _write_pending(action: str, reason: str, token: str) -> None:
    path = _pending_path()
    path.write_text(json.dumps({
        "action": action, "reason": reason, "token_hash": _token_hash(token),
        "expires": time.time() + TOKEN_TTL,
    }))
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _consume_token(action: str, token: str) -> bool:
    path = _pending_path()
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text())
    except (ValueError, OSError):
        return False
    if data.get("action") != action:
        return False
    if not hmac.compare_digest(data.get("token_hash", ""), _token_hash(token)):
        return False
    if time.time() > data.get("expires", 0):
        return False
    path.unlink(missing_ok=True)
    return True


def peek_pending() -> dict | None:
    """The pending action's action/reason/expiry — never the token, which is
    only ever held in-memory at the moment it's minted (see
    require_confirmation), not persisted in recoverable form."""
    path = _pending_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (ValueError, OSError):
        return None
    if time.time() > data.get("expires", 0):
        return None
    return data


# -- audit --------------------------------------------------------------------

def audit(action: str, outcome: str, detail: str = "") -> None:
    line = json.dumps({
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "action": action, "outcome": outcome, "detail": detail,
    })
    with _audit_path().open("a") as f:
        f.write(line + "\n")
