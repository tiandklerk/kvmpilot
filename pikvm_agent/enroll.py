"""Enrollment gateway: an authenticated HTTP endpoint that lets a companion
app (e.g. pikonnect) register PiKVM profiles over the network — no file
shuffling.

Because the payload carries device credentials that the agent can then drive,
every mutating request needs a Bearer pairing token (`pikvm pair`), only the
token's hash is stored, and the server binds to localhost unless told
otherwise. Put it behind Tailscale/WireGuard, never the public internet.

    POST /enroll   Bearer <token>   body = registry JSON  -> imports profiles
    GET  /profiles Bearer <token>                          -> names (no secrets)
    GET  /health                                           -> liveness
"""

from __future__ import annotations

from aiohttp import web

from . import config as config_mod
from . import guard


def _authorized(request: web.Request) -> bool:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return False
    return config_mod.verify_pairing_token(auth[len("Bearer "):].strip())


async def health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "service": "pikvm-enroll"})


async def enroll(request: web.Request) -> web.Response:
    if not _authorized(request):
        guard.audit("enroll", "blocked", f"unauthorized from {request.remote}")
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    try:
        doc = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid JSON body"}, status=400)
    try:
        names = config_mod.import_registry_doc(doc)
    except config_mod.ConfigError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)
    guard.audit("enroll", "executed", f"{','.join(names)} from {request.remote}")
    return web.json_response({"ok": True, "imported": names})


async def profiles(request: web.Request) -> web.Response:
    if not _authorized(request):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    out = [
        {"name": name, "host": ep.host, "user": ep.user,
         "use_ssl": ep.use_ssl, "port": ep.port}
        for name, ep in config_mod.list_profiles().items()
    ]
    return web.json_response({"ok": True, "profiles": out,
                              "default": config_mod.default_profile_name()})


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_post("/enroll", enroll)
    app.router.add_get("/profiles", profiles)
    return app


def serve(bind: str = "127.0.0.1", port: int = 8099) -> None:
    web.run_app(build_app(), host=bind, port=port, print=None)
