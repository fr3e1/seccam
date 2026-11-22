import asyncio
import logging
import os
import sys
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from starlette.applications import Starlette
from starlette.routing import Route

from server.app import create_app
from server.config import load_config

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("seccam")


def create_http_redirect_app(cfg) -> Starlette:
    async def ca_cert(request: Request):
        if cfg.ca_cert and os.path.exists(cfg.ca_cert):
            return FileResponse(cfg.ca_cert, media_type="application/x-x509-ca-cert")
        return Response(status_code=404)

    async def setup_page(request: Request):
        return FileResponse(os.path.join(cfg.static_dir, "setup.html"))

    async def redirect_to_https(request: Request):
        host = request.url.hostname
        if not host:
            host = cfg.host
        https_port = cfg.https_port
        port_suffix = "" if https_port == 443 else f":{https_port}"
        url = f"https://{host}{port_suffix}{request.url.path}"
        if request.url.query:
            url += "?" + request.url.query
        return RedirectResponse(url, status_code=301)

    routes = [
        Route("/ca.crt", ca_cert, methods=["GET"]),
        Route("/setup", setup_page, methods=["GET"]),
        Route("/{path:path}", redirect_to_https, methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"]),
    ]
    return Starlette(routes=routes)


async def main():
    cfg = load_config()
    app = create_app(cfg)

    https_config = uvicorn.Config(
        app,
        host=cfg.host,
        port=cfg.https_port,
        log_level="warning",
        proxy_headers=False,
        ws_max_size=2 * 1024 * 1024,
        ssl_certfile=cfg.cert if cfg.cert else None,
        ssl_keyfile=cfg.key if cfg.key else None,
    )
    https_server = uvicorn.Server(https_config)

    servers = [https_server]
    if cfg.cert and cfg.key:
        http_app = create_http_redirect_app(cfg)
        http_config = uvicorn.Config(
            http_app,
            host=cfg.host,
            port=cfg.http_port,
            log_level="warning",
            proxy_headers=False,
        )
        http_server = uvicorn.Server(http_config)
        servers.append(http_server)

    await asyncio.gather(*(s.serve() for s in servers))


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent.parent))
    asyncio.run(main())
