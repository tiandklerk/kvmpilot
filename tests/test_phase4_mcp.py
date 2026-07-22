"""Phase 4: MCP tools call through the same core against the mock device."""

import asyncio
import re

from mcp.types import ImageContent, TextContent

from pikvm_agent import guard, mcp_server


def _call(name, **kwargs):
    return asyncio.run(mcp_server.mcp.call_tool(name, kwargs))


def test_tools_listed():
    tools = asyncio.run(mcp_server.mcp.list_tools())
    names = {t.name for t in tools}
    assert {"screenshot", "type_text", "atx_action", "confirm_action",
            "system_status", "mouse_move"} <= names


def test_run_actions_screenshot_uncropped_gets_size_tip(mock_device):
    # an uncropped verification frame is legitimately larger; nudge the agent
    # toward crop for small-widget checks (found live: ~6x smaller cropped)
    result = _call("run_actions", actions=[{"action": "move", "x": 100, "y": 100}],
                   screenshot=True)
    contents = result[0] if isinstance(result, tuple) else result
    texts = [c.text for c in contents if isinstance(c, TextContent)]
    assert any("crop=[x,y,w,h]" in t for t in texts)


def test_run_actions_screenshot_cropped_no_tip(mock_device):
    result = _call("run_actions", actions=[{"action": "move", "x": 100, "y": 100}],
                   screenshot=True, crop=[0, 0, 100, 100])
    contents = result[0] if isinstance(result, tuple) else result
    texts = [c.text for c in contents if isinstance(c, TextContent)]
    assert not any("crop=[x,y,w,h]" in t for t in texts)


def test_screenshot_returns_inline_image(mock_device):
    result = _call("screenshot")
    contents = result[0] if isinstance(result, tuple) else result
    imgs = [c for c in contents if isinstance(c, ImageContent)]
    assert imgs and imgs[0].mimeType == "image/jpeg"
    assert imgs[0].data  # base64 payload present


def test_system_status_text(mock_device):
    result = _call("system_status")
    contents = result[0] if isinstance(result, tuple) else result
    text = " ".join(c.text for c in contents if isinstance(c, TextContent))
    assert "mock-pikvm" in text
    assert "mouse:" in text


def test_type_text_records_on_mock(mock_device):
    _call("type_text", text="hello mcp")
    assert any(e["kind"] == "hid_print" and e["text"] == "hello mcp"
               for e in mock_device.events())


def test_dangerous_type_blocked_then_confirmed(mock_device):
    result = _call("type_text", text="sudo reboot")
    contents = result[0] if isinstance(result, tuple) else result
    text = " ".join(c.text for c in contents if isinstance(c, TextContent))
    assert "BLOCKED" in text
    # the token is shown only in this response, never persisted in recoverable
    # form — extract it the same way the calling agent must.
    token = re.search(r'token "([0-9a-f]{6})"', text).group(1)
    _call("confirm_action", token=token)
    assert any(e["kind"] == "hid_print" and e["text"] == "sudo reboot"
               for e in mock_device.events())
