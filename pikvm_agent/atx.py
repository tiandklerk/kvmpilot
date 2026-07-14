"""ATX power control — always guarded (see guard.py)."""

from __future__ import annotations

from . import guard
from .client import PiKvmClient

BUTTONS = {
    "power": "short power press (power on, or ask the OS to shut down)",
    "power_long": "5-second power hold (hard power off)",
    "reset": "hardware reset",
}


def state(client: PiKvmClient) -> dict:
    return client.atx_state()


def click(client: PiKvmClient, button: str, confirm_token: str | None = None) -> None:
    if button not in BUTTONS:
        raise ValueError(f"unknown ATX button {button!r}; choose from {', '.join(BUTTONS)}")
    action = f"atx:{button}"
    guard.require_confirmation(action, f"ATX {button} — {BUTTONS[button]}", confirm_token)
    client.atx_click(button)
    guard.audit(action, "executed", BUTTONS[button])
