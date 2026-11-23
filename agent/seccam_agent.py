#!/usr/bin/env python3
"""SecCam headless camera agent: streams V4L2 frames (via ffmpeg) to a SecCam server."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import re
import signal
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

AGENT_VERSION = "2.0.0"
DEFAULT_TOKEN_FILE = "~/.config/seccam-agent/token"
DEFAULT_DEVICE = "/dev/video0"
MAX_BUFFER = 8 * 1024 * 1024
STABLE_SESSION_S = 30.0
PING_INTERVAL_S = 20.0
EXIT_OK, EXIT_USAGE, EXIT_TOKEN, EXIT_NO_FFMPEG = 0, 2, 3, 4

log = logging.getLogger("seccam.agent")


class JpegSplitter:
    """Splits a byte stream of concatenated JPEGs (FF D8 ... FF D9) into frames."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, chunk: bytes) -> list[bytes]:
        self._buf += chunk
        frames: list[bytes] = []
        while True:
            start = self._buf.find(b"\xff\xd8")
            if start < 0:
                # keep a trailing 0xFF: it may be the first half of a start marker
                keep = 1 if self._buf[-1:] == b"\xff" else 0
                del self._buf[: len(self._buf) - keep]
                break
            if start:
                del self._buf[:start]
            end = self._buf.find(b"\xff\xd9", 2)
            if end < 0:
                break
            frames.append(bytes(self._buf[: end + 2]))
            del self._buf[: end + 2]
        if len(self._buf) > MAX_BUFFER:
            log.warning("frame buffer overflow, resetting")
            self._buf.clear()
        return frames


def ws_url(server: str) -> str:
    m = re.match(r"^(https?)://([^/?#\s]+)/?$", (server or "").strip())
    if not m:
        raise ValueError("server must look like http(s)://host[:port]")
    return f"{'wss' if m.group(1) == 'https' else 'ws'}://{m.group(2)}/ws/camera"


def build_ffmpeg_cmd(ffmpeg: str, device: str, width: int, height: int, fps: float,
                     input_format: str, quality: int) -> list[str]:
    return [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "v4l2", "-input_format", input_format,
            "-video_size", f"{width}x{height}", "-framerate", str(fps), "-i", device,
            "-f", "image2pipe", "-c:v", "mjpeg", "-q:v", str(quality), "-"]


def backoff_delay(attempt: int, base: float = 0.5, cap: float = 30.0,
                  rand: Callable[[], float] = random.random) -> float:
    d = min(cap, base * 2 ** max(0, attempt))
    return d * (0.7 + 0.3 * rand())


def hello_message(camera_id: str, name: str, version: str) -> dict:
    return {"type": "hello", "camera_id": camera_id, "name": name, "kind": "agent", "version": version}


def _num(v: object, default: float) -> float:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else default


def send_interval(settings: dict, max_fps: float) -> float:
    wanted = max(_num(settings.get("preview_fps"), 2.0), _num(settings.get("detect_fps"), 2.0))
    return 1.0 / min(max_fps, wanted)


def default_camera_id() -> str:
    host = re.sub(r"[^A-Za-z0-9_-]", "-", socket.gethostname()) or "host"
    return ("agent-" + host)[:64]


class FrameSource:
    """Runs ffmpeg and keeps only the latest JPEG frame. Restarts ffmpeg with backoff if it exits."""

    def __init__(self, cmd: list[str]) -> None:
        self._cmd = cmd
        self._seq = 0
        self._frame: Optional[bytes] = None
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._task: Optional[asyncio.Task] = None

    async def _spawn(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            *self._cmd, stdout=asyncio.subprocess.PIPE, stderr=None, stdin=asyncio.subprocess.DEVNULL)

    async def start(self) -> None:
        """Raises FileNotFoundError if ffmpeg is not installed."""
        await self._spawn()
        self._task = asyncio.create_task(self._supervise())

    async def _supervise(self) -> None:
        attempt = 0
        while True:
            assert self._proc is not None and self._proc.stdout is not None
            splitter = JpegSplitter()
            began = time.monotonic()
            while True:
                chunk = await self._proc.stdout.read(65536)
                if not chunk:
                    break
                for jpeg in splitter.feed(chunk):
                    self._seq += 1
                    self._frame = jpeg
            await self._proc.wait()
            if time.monotonic() - began > STABLE_SESSION_S:
                attempt = 0
            delay = backoff_delay(attempt)
            attempt += 1
            log.warning("ffmpeg exited (code %s); restarting in %.1fs", self._proc.returncode, delay)
            await asyncio.sleep(delay)
            try:
                await self._spawn()
            except OSError as e:
                log.error("cannot restart ffmpeg: %s", e)
                self._proc = None
                return

    def latest(self) -> Optional[tuple[int, bytes]]:
        return (self._seq, self._frame) if self._frame is not None else None

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        proc = self._proc
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 3)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()


async def run_session(url: str, token: str, frames, name: str, max_fps: float, ssl_ctx) -> int:
    """One websocket session. Returns the close code (1006 if the connection failed or dropped)."""
    from websockets.asyncio.client import connect
    from websockets.exceptions import ConnectionClosed

    settings: dict = {}

    async def reader(ws) -> None:
        async for msg in ws:
            if isinstance(msg, str):
                try:
                    data = json.loads(msg)
                except ValueError:
                    continue
                if isinstance(data, dict) and data.get("type") == "config" and isinstance(data.get("settings"), dict):
                    settings.clear()
                    settings.update(data["settings"])

    try:
        async with connect(url, additional_headers={"Authorization": f"Bearer {token}"},
                           ssl=ssl_ctx if url.startswith("wss://") else None,
                           max_size=2 * 1024 * 1024, open_timeout=15) as ws:
            read_task = asyncio.create_task(reader(ws))
            try:
                await ws.send(json.dumps(hello_message(default_camera_id(), name, AGENT_VERSION)))
                await ws.send(json.dumps({"type": "mode", "mode": "server"}))
                last_seq = 0
                last_send = 0.0
                last_ping = time.monotonic()
                while not read_task.done():
                    now = time.monotonic()
                    if now - last_ping >= PING_INTERVAL_S:
                        await ws.send(json.dumps({"type": "ping"}))
                        last_ping = now
                    interval = send_interval(settings, max_fps)
                    latest = frames.latest()
                    if latest is not None and latest[0] != last_seq and now - last_send >= interval:
                        last_seq = latest[0]
                        last_send = now
                        await ws.send(latest[1])
                    await asyncio.wait({read_task}, timeout=0.02)
            except ConnectionClosed:
                pass
            finally:
                read_task.cancel()
                await asyncio.gather(read_task, return_exceptions=True)
            await ws.wait_closed()
            return ws.close_code or 1006
    except (OSError, asyncio.TimeoutError, ConnectionClosed) as e:
        log.warning("connection failed: %s", e)
        return 1006
    except Exception as e:  # websockets InvalidHandshake / InvalidURI etc.
        log.warning("connection error: %s", e)
        return 1006


def make_ssl_context(ca_file: Optional[str], insecure: bool) -> ssl.SSLContext:
    if insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return ssl.create_default_context(cafile=ca_file) if ca_file else ssl.create_default_context()


def pair(server: str, code: str, name: str, token_file: str, ca_file: Optional[str], insecure: bool) -> int:
    server = server.strip().rstrip("/")
    try:
        ws_url(server)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return EXIT_USAGE
    body = json.dumps({"code": code, "name": name, "kind": "agent"}).encode()
    req = urllib.request.Request(server + "/api/pair", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    ctx = make_ssl_context(ca_file, insecure) if server.startswith("https://") else None
    try:
        with urllib.request.urlopen(req, timeout=20, context=ctx) as resp:
            data = json.loads(resp.read().decode())
        token = data["token"]
        if not isinstance(token, str) or not token:
            raise KeyError("token")
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode()).get("detail", "")
        except (ValueError, AttributeError):
            detail = ""
        print(f"pairing failed: {detail or e.reason} (HTTP {e.code})", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
        print(f"pairing failed: {getattr(e, 'reason', e)}", file=sys.stderr)
        return 1
    path = Path(token_file).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token + "\n")
    os.chmod(path, 0o600)
    print(f"paired as device {data.get('device_id', '?')}; token saved to {path}")
    return EXIT_OK


def list_devices() -> int:
    for dev in sorted(Path("/dev").glob("video*"), key=lambda p: (len(p.name), p.name)):
        label = "-"
        try:
            label = (Path("/sys/class/video4linux") / dev.name / "name").read_text().strip() or "-"
        except OSError:
            pass
        print(f"{dev}\t{label}")
    return EXIT_OK


async def run_agent(args: argparse.Namespace, token: str, url: str) -> int:
    cmd = build_ffmpeg_cmd(args.ffmpeg, args.device, args.width, args.height, args.fps,
                           args.input_format, args.quality)
    frames = FrameSource(cmd)
    try:
        await frames.start()
    except FileNotFoundError:
        print(f"ffmpeg not found: {args.ffmpeg}", file=sys.stderr)
        return EXIT_NO_FFMPEG
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    ctx = make_ssl_context(args.ca_file, args.insecure) if url.startswith("wss://") else None
    stop_wait = asyncio.create_task(stop.wait())
    attempt = 0
    code = EXIT_OK
    try:
        while not stop.is_set():
            began = time.monotonic()
            session = asyncio.create_task(run_session(url, token, frames, args.name or "", args.fps, ctx))
            await asyncio.wait({session, stop_wait}, return_when=asyncio.FIRST_COMPLETED)
            if not session.done():
                session.cancel()
                await asyncio.gather(session, return_exceptions=True)
                break
            close_code = session.result()
            if close_code == 4401:
                print("token rejected by the server (close 4401); pair again", file=sys.stderr)
                code = EXIT_TOKEN
                break
            if time.monotonic() - began > STABLE_SESSION_S:
                attempt = 0
            delay = backoff_delay(attempt)
            attempt += 1
            log.info("session ended (close %s); reconnecting in %.1fs", close_code, delay)
            try:
                await asyncio.wait_for(stop.wait(), delay)
            except asyncio.TimeoutError:
                pass
    finally:
        stop_wait.cancel()
        await frames.stop()
    return code


def build_parser() -> argparse.ArgumentParser:
    env = os.environ.get
    p = argparse.ArgumentParser(prog="seccam_agent.py", description="SecCam headless V4L2 camera agent")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--server", default=env("SECCAM_AGENT_SERVER", ""), help="https://host[:port]")
        sp.add_argument("--token-file", default=env("SECCAM_AGENT_TOKEN_FILE", DEFAULT_TOKEN_FILE))
        tls = sp.add_mutually_exclusive_group()
        tls.add_argument("--ca-file", default=None, help="PEM file with the server CA")
        tls.add_argument("--insecure", action="store_true", help="skip TLS verification (not recommended)")

    pp = sub.add_parser("pair", help="exchange a pairing code for a device token")
    common(pp)
    pp.add_argument("--code", required=True)
    pp.add_argument("--name", required=True)
    sub.add_parser("list-devices", help="list /dev/video* devices")
    rp = sub.add_parser("run", help="stream frames to the server")
    common(rp)
    rp.add_argument("--device", default=env("SECCAM_AGENT_DEVICE", DEFAULT_DEVICE))
    rp.add_argument("--width", type=int, default=1280)
    rp.add_argument("--height", type=int, default=720)
    rp.add_argument("--fps", type=float, default=5)
    rp.add_argument("--input-format", choices=["mjpeg", "yuyv422"], default="mjpeg")
    rp.add_argument("--quality", type=int, default=5)
    rp.add_argument("--ffmpeg", default="ffmpeg")
    rp.add_argument("--name", default="")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as e:
        return EXIT_OK if e.code in (0, None) else EXIT_USAGE
    if args.cmd == "list-devices":
        return list_devices()
    if not args.server:
        print("--server (or SECCAM_AGENT_SERVER) is required", file=sys.stderr)
        return EXIT_USAGE
    if args.cmd == "pair":
        return pair(args.server, args.code, args.name, args.token_file, args.ca_file, args.insecure)
    try:
        url = ws_url(args.server)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return EXIT_USAGE
    if args.fps <= 0 or args.width <= 0 or args.height <= 0:
        print("--fps, --width and --height must be positive", file=sys.stderr)
        return EXIT_USAGE
    try:
        token = Path(args.token_file).expanduser().read_text().strip()
    except OSError as e:
        print(f"cannot read token file: {e}; run the pair command first", file=sys.stderr)
        return EXIT_USAGE
    if not token:
        print("token file is empty; run the pair command first", file=sys.stderr)
        return EXIT_USAGE
    return asyncio.run(run_agent(args, token, url))


if __name__ == "__main__":
    sys.exit(main())
