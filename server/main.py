import asyncio
import logging
import os
import sys
from pathlib import Path

import uvicorn
from fastapi import Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from starlette.applications import Starlette
from starlette.routing import Route

from server.app import create_app
from server.config import Config, load_config

logger = logging.getLogger("seccam")


def create_http_redirect_app(cfg: Config) -> Starlette:
    async def ca_cert(request: Request):
        if cfg.ca_cert and os.path.exists(cfg.ca_cert):
            return FileResponse(cfg.ca_cert, media_type="application/x-x509-ca-cert")
        return JSONResponse({"detail": "Not found", "code": "not_found"}, status_code=404)

    async def setup_page(request: Request):
        return FileResponse(os.path.join(cfg.static_dir, "setup.html"))

    async def redirect_to_https(request: Request):
        host = request.url.hostname or cfg.host
        port_suffix = "" if cfg.https_port == 443 else f":{cfg.https_port}"
        url = f"https://{host}{port_suffix}{request.url.path}"
        if request.url.query:
            url += "?" + request.url.query
        return RedirectResponse(url, status_code=301)

    routes = [
        Route("/ca.crt", ca_cert, methods=["GET"]),
        Route("/setup", setup_page, methods=["GET"]),
        Route("/{path:path}", redirect_to_https,
              methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"]),
    ]
    return Starlette(routes=routes)


def build_server_configs(cfg: Config, app) -> list[uvicorn.Config]:
    tls = bool(cfg.cert and cfg.key)
    configs = [uvicorn.Config(
        app,
        host=cfg.host,
        port=cfg.https_port,
        ssl_certfile=cfg.cert if tls else None,
        ssl_keyfile=cfg.key if tls else None,
        proxy_headers=False,
        server_header=False,
        ws_max_size=2 * 1024 * 1024,
        ws_ping_interval=20,
        ws_ping_timeout=20,
        log_level="warning",
    )]
    if tls:
        configs.append(uvicorn.Config(
            create_http_redirect_app(cfg),
            host=cfg.host,
            port=cfg.http_port,
            proxy_headers=False,
            server_header=False,
            log_level="warning",
        ))
    return configs


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    app = create_app(cfg)
    servers = [uvicorn.Server(c) for c in build_server_configs(cfg, app)]
    await asyncio.gather(*(s.serve() for s in servers))


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent.parent))
    asyncio.run(main())
