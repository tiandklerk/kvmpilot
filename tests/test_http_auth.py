"""HTTP MCP transport: bearer-token auth. The streamable-http transport has no
access control of its own — anyone reaching the port can drive the KVM — so a
configured token must be required on every request."""

from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from pikvm_agent import mcp_server


def _downstream_app():
    async def ok(request):
        return PlainTextResponse("ok")
    return Starlette(routes=[Route("/mcp", ok)])


def test_rejects_missing_authorization():
    app = mcp_server.BearerAuthMiddleware(_downstream_app(), token="secret123")
    client = TestClient(app)
    r = client.get("/mcp")
    assert r.status_code == 401
    assert "unauthorized" in r.text


def test_rejects_wrong_token():
    app = mcp_server.BearerAuthMiddleware(_downstream_app(), token="secret123")
    client = TestClient(app)
    r = client.get("/mcp", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_rejects_malformed_header():
    app = mcp_server.BearerAuthMiddleware(_downstream_app(), token="secret123")
    client = TestClient(app)
    r = client.get("/mcp", headers={"Authorization": "secret123"})  # missing "Bearer "
    assert r.status_code == 401


def test_accepts_correct_token():
    app = mcp_server.BearerAuthMiddleware(_downstream_app(), token="secret123")
    client = TestClient(app)
    r = client.get("/mcp", headers={"Authorization": "Bearer secret123"})
    assert r.status_code == 200 and r.text == "ok"


def test_wired_against_the_real_mcp_app_without_token():
    # exercises the actual integration point main() uses, not just the
    # middleware in isolation
    app = mcp_server.BearerAuthMiddleware(mcp_server.mcp.streamable_http_app(), token="secret123")
    client = TestClient(app)
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r.status_code == 401


def test_wired_against_the_real_mcp_app_with_token_reaches_mcp():
    # correct token clears the gate; the request reaches FastMCP's own handler
    # (whatever it decides to do with a bare ping — the point is it's no
    # longer a 401 from our middleware). FastMCP's session manager needs the
    # ASGI lifespan running, hence TestClient as a context manager here.
    app = mcp_server.BearerAuthMiddleware(mcp_server.mcp.streamable_http_app(), token="secret123")
    with TestClient(app) as client:
        r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                        headers={"Authorization": "Bearer secret123",
                                 "Accept": "application/json, text/event-stream"})
    assert r.status_code != 401
