"""Character and combo mapping to kvmd web key codes (US layout).

Ported from pikonnect's KeyMap.kt; key names match kvmd's web keymap
(KeyA, Digit1, Minus, BracketLeft, ControlLeft, ...).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MappedKey:
    key: str
    shift: bool


_SHIFTED_SYMBOLS = {
    "!": "Digit1", "@": "Digit2", "#": "Digit3", "$": "Digit4", "%": "Digit5",
    "^": "Digit6", "&": "Digit7", "*": "Digit8", "(": "Digit9", ")": "Digit0",
    "_": "Minus", "+": "Equal", "{": "BracketLeft", "}": "BracketRight",
    "|": "Backslash", ":": "Semicolon", '"': "Quote", "~": "Backquote",
    "<": "Comma", ">": "Period", "?": "Slash",
}

_PLAIN_SYMBOLS = {
    " ": "Space", "\n": "Enter", "\t": "Tab",
    "-": "Minus", "=": "Equal", "[": "BracketLeft", "]": "BracketRight",
    "\\": "Backslash", ";": "Semicolon", "'": "Quote", "`": "Backquote",
    ",": "Comma", ".": "Period", "/": "Slash",
}

# Names accepted in combos ("ctrl+alt+del") -> kvmd key codes.
NAMED_KEYS = {
    "ctrl": "ControlLeft", "control": "ControlLeft", "rctrl": "ControlRight",
    "shift": "ShiftLeft", "rshift": "ShiftRight",
    "alt": "AltLeft", "ralt": "AltRight", "altgr": "AltRight",
    "meta": "MetaLeft", "win": "MetaLeft", "super": "MetaLeft", "cmd": "MetaLeft",
    "enter": "Enter", "return": "Enter", "esc": "Escape", "escape": "Escape",
    "tab": "Tab", "space": "Space", "backspace": "Backspace",
    "del": "Delete", "delete": "Delete", "insert": "Insert",
    "home": "Home", "end": "End", "pageup": "PageUp", "pagedown": "PageDown",
    "up": "ArrowUp", "down": "ArrowDown", "left": "ArrowLeft", "right": "ArrowRight",
    "capslock": "CapsLock", "printscreen": "PrintScreen", "scrolllock": "ScrollLock",
    "pause": "Pause", "menu": "ContextMenu",
    **{f"f{i}": f"F{i}" for i in range(1, 13)},
}


def from_char(c: str) -> MappedKey | None:
    """Map one typed character to (key, needs-shift); None if untypeable."""
    if "a" <= c <= "z":
        return MappedKey(f"Key{c.upper()}", False)
    if "A" <= c <= "Z":
        return MappedKey(f"Key{c}", True)
    if "0" <= c <= "9":
        return MappedKey(f"Digit{c}", False)
    if c in _PLAIN_SYMBOLS:
        return MappedKey(_PLAIN_SYMBOLS[c], False)
    if c in _SHIFTED_SYMBOLS:
        return MappedKey(_SHIFTED_SYMBOLS[c], True)
    return None


def parse_combo(combo: str) -> list[str]:
    """Parse "ctrl+alt+del" / "meta" / "ctrl+shift+t" into kvmd key codes,
    in press order (released in reverse)."""
    keys: list[str] = []
    for part in combo.split("+"):
        name = part.strip()
        if not name:
            raise ValueError(f"empty key in combo {combo!r}")
        lower = name.lower()
        if lower in NAMED_KEYS:
            keys.append(NAMED_KEYS[lower])
        elif len(name) == 1:
            mapped = from_char(name.lower())
            if mapped is None:
                raise ValueError(f"cannot map key {name!r} in combo {combo!r}")
            keys.append(mapped.key)
        else:
            # allow raw kvmd names like "KeyA", "F5", "NumpadEnter"
            keys.append(name)
    if not keys:
        raise ValueError(f"empty combo {combo!r}")
    return keys
