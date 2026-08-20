"""Synthesized mouse and keyboard input, for controls with no usable accessibility action."""

from __future__ import annotations

import time

import Quartz

# The pacing an operating system needs for a synthesized gesture to register lives in the central tuning policy.
from langmesh.base.primitives.limits import current_limits

# Virtual key codes for the named non-printing keys, which map to a fixed physical key on any layout.
_NAMED_KEY_CODES = {
    "return": 0x24,
    "enter": 0x4C,
    "tab": 0x30,
    "space": 0x31,
    "delete": 0x33,
    "backspace": 0x33,
    "forwarddelete": 0x75,
    "escape": 0x35,
    "help": 0x72,
    "clear": 0x47,
    "home": 0x73,
    "end": 0x77,
    "pageup": 0x74,
    "pagedown": 0x79,
    "left": 0x7B,
    "right": 0x7C,
    "down": 0x7D,
    "up": 0x7E,
    "f1": 0x7A,
    "f2": 0x78,
    "f3": 0x63,
    "f4": 0x76,
    "f5": 0x60,
    "f6": 0x61,
    "f7": 0x62,
    "f8": 0x64,
    "f9": 0x65,
    "f10": 0x6D,
    "f11": 0x67,
    "f12": 0x6F,
}

# US-layout key codes for letters and digits, the fallback when the active layout cannot produce a character.
_US_CHAR_KEY_CODES = {
    "a": 0x00,
    "b": 0x0B,
    "c": 0x08,
    "d": 0x02,
    "e": 0x0E,
    "f": 0x03,
    "g": 0x05,
    "h": 0x04,
    "i": 0x22,
    "j": 0x26,
    "k": 0x28,
    "l": 0x25,
    "m": 0x2E,
    "n": 0x2D,
    "o": 0x1F,
    "p": 0x23,
    "q": 0x0C,
    "r": 0x0F,
    "s": 0x01,
    "t": 0x11,
    "u": 0x20,
    "v": 0x09,
    "w": 0x0D,
    "x": 0x07,
    "y": 0x10,
    "z": 0x06,
    "0": 0x1D,
    "1": 0x12,
    "2": 0x13,
    "3": 0x14,
    "4": 0x15,
    "5": 0x17,
    "6": 0x16,
    "7": 0x1A,
    "8": 0x1C,
    "9": 0x19,
}

_MODIFIER_FLAGS = {
    "command": Quartz.kCGEventFlagMaskCommand,  # type: ignore[attr-defined]
    "cmd": Quartz.kCGEventFlagMaskCommand,  # type: ignore[attr-defined]
    "option": Quartz.kCGEventFlagMaskAlternate,  # type: ignore[attr-defined]
    "opt": Quartz.kCGEventFlagMaskAlternate,  # type: ignore[attr-defined]
    "alt": Quartz.kCGEventFlagMaskAlternate,  # type: ignore[attr-defined]
    "control": Quartz.kCGEventFlagMaskControl,  # type: ignore[attr-defined]
    "ctrl": Quartz.kCGEventFlagMaskControl,  # type: ignore[attr-defined]
    "shift": Quartz.kCGEventFlagMaskShift,  # type: ignore[attr-defined]
    "function": Quartz.kCGEventFlagMaskSecondaryFn,  # type: ignore[attr-defined]
    "fn": Quartz.kCGEventFlagMaskSecondaryFn,  # type: ignore[attr-defined]
}

NAMED_KEYS = tuple(sorted(_NAMED_KEY_CODES))


def _layout_key_code(char: str) -> int | None:
    """The key code that types `char` under the active layout, or `None`."""
    for key_code in range(128):
        event = Quartz.CGEventCreateKeyboardEvent(None, key_code, True)  # type: ignore[attr-defined]
        if event is None:
            continue
        _, produced = Quartz.CGEventKeyboardGetUnicodeString(  # type: ignore[attr-defined]
            event, 4, None, None
        )
        if produced and produced.lower() == char:
            return key_code
    return None


def click(
    pid: int, point_x: float, point_y: float, *, clicks: int = 1, button: str = "left"
) -> None:
    """Post a click to one app's event queue, leaving the user's real cursor where it is."""
    button_code = {
        "left": Quartz.kCGMouseButtonLeft,  # type: ignore[attr-defined]
        "right": Quartz.kCGMouseButtonRight,  # type: ignore[attr-defined]
        "center": Quartz.kCGMouseButtonCenter,  # type: ignore[attr-defined]
    }.get(button, Quartz.kCGMouseButtonLeft)  # type: ignore[attr-defined]
    down_type = Quartz.kCGEventLeftMouseDown if button != "right" else Quartz.kCGEventRightMouseDown  # type: ignore[attr-defined]
    up_type = Quartz.kCGEventLeftMouseUp if button != "right" else Quartz.kCGEventRightMouseUp  # type: ignore[attr-defined]
    for click_index in range(max(1, clicks)):
        down = Quartz.CGEventCreateMouseEvent(None, down_type, (point_x, point_y), button_code)  # type: ignore[attr-defined]
        up = Quartz.CGEventCreateMouseEvent(None, up_type, (point_x, point_y), button_code)  # type: ignore[attr-defined]
        # Click-state lets the target recognize a double/triple click as one gesture.
        Quartz.CGEventSetIntegerValueField(down, Quartz.kCGMouseEventClickState, click_index + 1)  # type: ignore[attr-defined]
        Quartz.CGEventSetIntegerValueField(up, Quartz.kCGMouseEventClickState, click_index + 1)  # type: ignore[attr-defined]
        Quartz.CGEventPostToPid(pid, down)  # type: ignore[attr-defined]
        Quartz.CGEventPostToPid(pid, up)  # type: ignore[attr-defined]
        time.sleep(current_limits().click_interval)


def move(pid: int, point_x: float, point_y: float) -> None:
    """Move the pointer over one app's window to reveal hover states, without pressing anything."""
    event = Quartz.CGEventCreateMouseEvent(  # type: ignore[attr-defined]
        None,
        Quartz.kCGEventMouseMoved,  # type: ignore[attr-defined]
        (point_x, point_y),
        Quartz.kCGMouseButtonLeft,  # type: ignore[attr-defined]
    )
    Quartz.CGEventPostToPid(pid, event)  # type: ignore[attr-defined]


def drag(
    pid: int, start_x: float, start_y: float, end_x: float, end_y: float, *, button: str = "left"
) -> None:
    """Press, drag and release, interpolated into several moves so the target sees a real drag."""
    button_code = {
        "left": Quartz.kCGMouseButtonLeft,  # type: ignore[attr-defined]
        "right": Quartz.kCGMouseButtonRight,  # type: ignore[attr-defined]
    }.get(button, Quartz.kCGMouseButtonLeft)  # type: ignore[attr-defined]
    down_type = Quartz.kCGEventLeftMouseDown if button != "right" else Quartz.kCGEventRightMouseDown  # type: ignore[attr-defined]
    drag_type = (
        Quartz.kCGEventLeftMouseDragged  # type: ignore[attr-defined]
        if button != "right"
        else Quartz.kCGEventRightMouseDragged  # type: ignore[attr-defined]
    )  # type: ignore[attr-defined]
    up_type = Quartz.kCGEventLeftMouseUp if button != "right" else Quartz.kCGEventRightMouseUp  # type: ignore[attr-defined]

    def post(event_type: int, point_x: float, point_y: float) -> None:
        Quartz.CGEventPostToPid(  # type: ignore[attr-defined]
            pid,
            Quartz.CGEventCreateMouseEvent(None, event_type, (point_x, point_y), button_code),  # type: ignore[attr-defined]
        )

    step_interval = current_limits().drag_step_interval
    post(down_type, start_x, start_y)
    time.sleep(step_interval)
    steps = current_limits().drag_steps
    for step in range(1, steps + 1):
        fraction = step / steps
        post(
            drag_type,
            start_x + (end_x - start_x) * fraction,
            start_y + (end_y - start_y) * fraction,
        )
        time.sleep(step_interval)
    post(up_type, end_x, end_y)


def type_text(pid: int, text: str) -> None:
    """Type an arbitrary Unicode string into the target app, script-independent and without stealing focus."""
    chunk_size = current_limits().type_chunk_size
    chunk_interval = current_limits().type_chunk_interval
    for chunk_start in range(0, len(text), chunk_size):
        chunk = text[chunk_start : chunk_start + chunk_size]
        utf16_length = len(chunk.encode("utf-16-le")) // 2
        down = Quartz.CGEventCreateKeyboardEvent(None, 0, True)  # type: ignore[attr-defined]
        Quartz.CGEventKeyboardSetUnicodeString(  # type: ignore[attr-defined]
            down, utf16_length, chunk
        )
        Quartz.CGEventPostToPid(pid, down)  # type: ignore[attr-defined]
        up = Quartz.CGEventCreateKeyboardEvent(None, 0, False)  # type: ignore[attr-defined]
        Quartz.CGEventKeyboardSetUnicodeString(  # type: ignore[attr-defined]
            up, utf16_length, chunk
        )
        Quartz.CGEventPostToPid(pid, up)  # type: ignore[attr-defined]
        time.sleep(chunk_interval)


_MODIFIER_ALIASES = {
    "cmd": "command",
    "meta": "command",
    "super": "command",
    "win": "command",
    "⌘": "command",
    "opt": "option",
    "alt": "option",
    "⌥": "option",
    "ctrl": "control",
    "⌃": "control",
    "⇧": "shift",
}


def press_key(pid: int, key: str, modifiers: list[str]) -> bool:
    """Press a key or chord in the target app, named non-printing or a single character for a shortcut."""
    name = key.strip().lower()
    if "+" in name and len(name) > 1:
        *chord_modifiers, name = [part.strip() for part in name.split("+") if part.strip()]
        modifiers = [*modifiers, *chord_modifiers]
    code = _NAMED_KEY_CODES.get(name)
    if code is None and len(name) == 1:
        # Ask the active layout which key types this character, falling back to the US position as macOS does.
        code = _layout_key_code(name)
        if code is None:
            code = _US_CHAR_KEY_CODES.get(name)
    if code is None:
        return False
    flags = 0
    for modifier in modifiers:
        spelled = modifier.strip().lower()
        flag = _MODIFIER_FLAGS.get(_MODIFIER_ALIASES.get(spelled, spelled))
        if flag is None:
            return False
        flags |= flag
    down = Quartz.CGEventCreateKeyboardEvent(None, code, True)  # type: ignore[attr-defined]
    up = Quartz.CGEventCreateKeyboardEvent(None, code, False)  # type: ignore[attr-defined]
    if flags:
        Quartz.CGEventSetFlags(down, flags)  # type: ignore[attr-defined]
        Quartz.CGEventSetFlags(up, flags)  # type: ignore[attr-defined]
    Quartz.CGEventPostToPid(pid, down)  # type: ignore[attr-defined]
    Quartz.CGEventPostToPid(pid, up)  # type: ignore[attr-defined]
    return True


def scroll(pid: int, delta_x: int, delta_y: int) -> None:
    """Post a scroll-wheel event to the target app. Positive delta_y scrolls up."""
    event = Quartz.CGEventCreateScrollWheelEvent(  # type: ignore[attr-defined]
        None,
        Quartz.kCGScrollEventUnitPixel,  # type: ignore[attr-defined]
        2,
        delta_y,
        delta_x,  # type: ignore[attr-defined]
    )
    Quartz.CGEventPostToPid(pid, event)  # type: ignore[attr-defined]
