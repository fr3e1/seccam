"""Self-updater (CONTRACT §12, §12.1).

Runs as root from the update unit. Everything that touches the network or spawns processes is injectable.
The web process only ever writes updates/request.json (see request_update); everything else here is root-side
and treats data_dir as hostile (lstat / O_NOFOLLOW / O_EXCL everywhere).
"""
from __future__ import annotations

import argparse
import contextlib
import errno
import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import ssl
import stat
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional
from urllib.parse import quote, urljoin, urlparse

from .version import __version__

log = logging.getLogger("seccam")

GITHUB_API = "https://api.github.com"
MAX_TARBALL = 100 * 1024 * 1024
MAX_EXTRACTED = 300 * 1024 * 1024
MAX_JSON = 1024 * 1024
MAX_MEMBERS = 20000
MAX_REQUEST = 4096
MAX_STATUS = 64 * 1024
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
NAME_RE = re.compile(r"^[A-Za-z0-9._/+-]+$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
ALLOWED_HOSTS = ("api.github.com", "github.com", "objects.githubusercontent.com",
                 "release-assets.githubusercontent.com")
STALE_AFTER_S = 1800
SYSTEMD_DIR = Path("/etc/systemd/system")

STATES = ("idle", "requested", "downloading", "verifying", "installing", "restarting", "health_check",
          "succeeded", "rolled_back", "failed")
TERMINAL = ("idle", "succeeded", "rolled_back", "failed")


class UpdateError(Exception):
    pass


@dataclass(frozen=True)
class ReleaseInfo:
    version: str
    tag: str
    published_at: str
    notes: str
    html_url: str
    tarball_url: str
    sums_url: str


HttpGet = Callable[[str, int], bytes]


# ---------------------------------------------------------------- network

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):  # type: ignore[override]
        return None


def _check_url(url: str) -> None:
    p = urlparse(url)
    if p.scheme != "https" or (p.hostname or "").lower() not in ALLOWED_HOSTS:
        raise UpdateError("refusing URL outside the allowed hosts")


def default_http_get(url: str, max_bytes: int) -> bytes:
    opener = urllib.request.build_opener(_NoRedirect)
    for _ in range(6):  # initial request + up to 5 redirects
        _check_url(url)
        headers = {"User-Agent": f"seccam-updater/{__version__}"}
        if urlparse(url).hostname == "api.github.com":
            headers["Accept"] = "application/vnd.github+json"
        try:
            with opener.open(urllib.request.Request(url, headers=headers), timeout=30) as resp:
                length = resp.headers.get("Content-Length")
                if length and length.isdigit() and int(length) > max_bytes:
                    raise UpdateError("download too large")
                body = resp.read(max_bytes + 1)
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                loc = e.headers.get("Location")
                if not loc:
                    raise UpdateError("redirect without Location") from None
                url = urljoin(url, loc)
                continue
            raise UpdateError(f"HTTP error {e.code}") from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise UpdateError(f"network error: {type(e).__name__}") from None
        if len(body) > max_bytes:
            raise UpdateError("download too large")
        return body
    raise UpdateError("too many redirects")


# ---------------------------------------------------------------- versions / release metadata

def parse_version(s: str) -> tuple[int, int, int]:
    t = s.strip()
    if t.startswith("v"):
        t = t[1:]
    if not VERSION_RE.match(t):
        raise ValueError(f"invalid version: {s!r}")
    a, b, c = t.split(".")
    return int(a), int(b), int(c)


def is_newer(candidate: str, current: str) -> bool:
    return parse_version(candidate) > parse_version(current)


def fetch_latest_release(http_get: HttpGet = default_http_get, repo: str = "fr3e1/seccam") -> ReleaseInfo:
    if not REPO_RE.match(repo):
        raise UpdateError("invalid repo")
    body = http_get(f"{GITHUB_API}/repos/{repo}/releases/latest", MAX_JSON)
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise UpdateError("bad release JSON") from None
    if not isinstance(data, dict):
        raise UpdateError("bad release JSON")
    tag = data.get("tag_name")
    if not isinstance(tag, str):
        raise UpdateError("release has no tag_name")
    version = tag[1:] if tag.startswith("v") else tag
    if not VERSION_RE.match(version):
        raise UpdateError("release tag is not X.Y.Z")
    html_url = data.get("html_url")
    prefix = f"https://github.com/{repo}/"
    if not isinstance(html_url, str) or not html_url.startswith(prefix):
        raise UpdateError("bad html_url")
    published = data.get("published_at")
    if not isinstance(published, str):
        raise UpdateError("bad published_at")
    notes = data.get("body")
    notes = notes[:20000] if isinstance(notes, str) else ""
    assets = data.get("assets")
    if not isinstance(assets, list):
        raise UpdateError("release has no assets")
    urls: dict[str, str] = {}
    for a in assets:
        if isinstance(a, dict) and isinstance(a.get("name"), str) and isinstance(a.get("browser_download_url"), str):
            urls[a["name"]] = a["browser_download_url"]
    tar_name = f"seccam-{version}.tar.gz"
    for name in (tar_name, "SHA256SUMS"):
        if name not in urls:
            raise UpdateError(f"release asset missing: {name}")
        if not urls[name].startswith(f"https://github.com/{repo}/releases/download/"):
            raise UpdateError(f"bad asset URL for {name}")
    return ReleaseInfo(version, tag, published, notes, html_url, urls[tar_name], urls["SHA256SUMS"])


# ---------------------------------------------------------------- checksums

_SUM_RE = re.compile(r"^([0-9a-fA-F]{64})  ?\*?(\S.*)$")


def parse_sha256sums(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _SUM_RE.match(line)
        if m:
            out[m.group(2).strip()] = m.group(1).lower()
    return out


def expected_sha256(sums: dict[str, str], filename: str) -> str:
    try:
        return sums[filename]
    except KeyError:
        raise UpdateError(f"no checksum for {filename}") from None


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_sha256(path: Path, expected_hex: str) -> None:
    if sha256_file(path) != expected_hex.strip().lower():
        raise UpdateError("checksum mismatch")


# ---------------------------------------------------------------- tarball

def _validate_member_name(name: str, expected_top: str) -> str:
    n = name[:-1] if name.endswith("/") else name
    if not n or n.startswith("/") or not NAME_RE.match(n):
        raise UpdateError(f"unsafe tar member name: {name!r}")
    parts = n.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise UpdateError(f"unsafe tar member name: {name!r}")
    if parts[0] != expected_top:
        raise UpdateError("unexpected top-level directory in tarball")
    return n


def safe_extract_tarball(tar_path: Path, dest: Path, expected_top: str) -> Path:
    if not NAME_RE.match(expected_top) or "/" in expected_top or expected_top in (".", ".."):
        raise UpdateError("bad expected_top")
    dest.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(tar_path, "r:gz") as tf:
            members: list[tuple[tarfile.TarInfo, str]] = []
            seen: set[str] = set()
            total = 0
            for m in tf:
                if len(members) >= MAX_MEMBERS:
                    raise UpdateError("too many tar members")
                name = _validate_member_name(m.name, expected_top)
                if not (m.isreg() or m.isdir()) or m.issparse() \
                        or any(k.startswith("GNU.sparse") for k in m.pax_headers):
                    raise UpdateError(f"unsupported tar member type: {name}")
                if name in seen:
                    raise UpdateError(f"duplicate tar member: {name}")
                seen.add(name)
                if m.isreg():
                    if m.size < 0:
                        raise UpdateError("bad tar member size")
                    total += m.size
                    if total > MAX_EXTRACTED:
                        raise UpdateError("tarball too large when extracted")
                members.append((m, name))
            root = dest / expected_top
            os.makedirs(root, mode=0o755, exist_ok=True)
            dirs = [root]
            for m, name in members:
                if hasattr(tarfile, "data_filter"):  # extra defence on 3.11.4+; result is identical without it
                    try:
                        tarfile.data_filter(m, str(dest))
                    except tarfile.FilterError as e:
                        raise UpdateError(f"tar filter refused member: {e}") from None
                target = dest / name
                if m.isdir():
                    os.makedirs(target, mode=0o755, exist_ok=True)
                    dirs.append(target)
                    continue
                os.makedirs(target.parent, mode=0o755, exist_ok=True)
                src = tf.extractfile(m)
                if src is None:
                    raise UpdateError(f"cannot read tar member: {name}")
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "wb") as out:
                    shutil.copyfileobj(src, out)
                    os.fchmod(out.fileno(), 0o755 if m.mode & 0o100 else 0o644)
            for d in dirs:
                os.chmod(d, 0o755)
    except (tarfile.TarError, OSError, EOFError, zlib.error) as e:
        raise UpdateError(f"cannot extract tarball: {type(e).__name__}") from None
    return dest / expected_top


def check_requirements(path: Path) -> None:
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-") or "://" in line or "@" in line:
            raise UpdateError("requirements.txt contains options, URLs or direct references")


def validate_release_tree(root: Path, version: str) -> None:
    try:
        found = (root / "VERSION").read_text().strip()
    except OSError:
        raise UpdateError("release has no VERSION file") from None
    if found != version:
        raise UpdateError("release VERSION does not match")
    for rel in ("server/app.py", "server/main.py", "static/index.html", "requirements.txt"):
        if not (root / rel).is_file():
            raise UpdateError(f"release is missing {rel}")


# ---------------------------------------------------------------- status / request files

def _updates_dir(data_dir: Path, create: bool = False) -> Path:
    d = Path(data_dir) / "updates"
    try:
        st = os.lstat(d)
    except FileNotFoundError:
        if not create:
            raise UpdateError("updates directory missing") from None
        os.mkdir(d, 0o750)
        st = os.lstat(d)
    except OSError:
        raise UpdateError("updates directory unreadable") from None
    if not stat.S_ISDIR(st.st_mode):
        raise UpdateError("updates is not a real directory")
    return d


def _lexists(p: Path) -> bool:
    try:
        os.lstat(p)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return True


def _idle(message: str = "") -> dict:
    return {"state": "idle", "version": None, "previous_version": None, "message": message, "updated_at": None}


def _read_small(path: Path, limit: int) -> Optional[bytes]:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > limit:
            return None
        return os.read(fd, limit + 1)
    except OSError:
        return None
    finally:
        os.close(fd)


def read_status(data_dir: Path, now: Optional[float] = None) -> dict:
    now = time.time() if now is None else now
    status = _idle()
    try:
        d = _updates_dir(data_dir)
    except UpdateError:
        return status
    raw = _read_small(d / "status.json", MAX_STATUS)
    if raw is not None:
        try:
            s = json.loads(raw)
            if isinstance(s, dict) and s.get("state") in STATES:
                ver, prev, msg, upd = (s.get("version"), s.get("previous_version"), s.get("message"),
                                       s.get("updated_at"))
                if (ver is None or isinstance(ver, str)) and (prev is None or isinstance(prev, str)) \
                        and isinstance(msg, str) and (upd is None or (isinstance(upd, int) and not isinstance(upd, bool))):
                    status = {"state": s["state"], "version": ver, "previous_version": prev,
                              "message": msg, "updated_at": upd}
        except (ValueError, UnicodeDecodeError):
            pass
    if status["state"] not in TERMINAL:
        upd = status["updated_at"]
        if not isinstance(upd, int) or now - upd / 1000.0 > STALE_AFTER_S:
            status["state"] = "failed"
            status["message"] = "updater stopped unexpectedly (stale status)"
    if status["state"] in TERMINAL and _lexists(d / "request.json"):
        status["state"] = "requested"
    return status


def request_update(data_dir: Path, version: str, requested_by: str) -> None:
    if not isinstance(version, str) or not VERSION_RE.match(version):
        raise ValueError("invalid version")
    d = _updates_dir(data_dir, create=True)
    final = d / "request.json"
    st = read_status(data_dir)
    if _lexists(final) or st["state"] not in TERMINAL:
        raise UpdateError("update pending")
    payload = json.dumps({"version": version, "requested_at": int(time.time() * 1000),
                          "requested_by": str(requested_by)}).encode()
    tmp = d / f".request.{os.getpid()}.tmp"
    with contextlib.suppress(FileNotFoundError):
        os.unlink(tmp)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
    try:
        with os.fdopen(fd, "wb") as f:
            os.fchmod(f.fileno(), 0o640)
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(tmp, final)  # no-clobber publish
        except FileExistsError:
            raise UpdateError("update pending") from None
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
    log.info("update to %s requested by %s", version, requested_by)


def write_status(data_dir: Path, state: str, version: Optional[str], previous_version: Optional[str],
                 message: str) -> None:
    d = _updates_dir(data_dir)
    payload = json.dumps({"state": state, "version": version, "previous_version": previous_version,
                          "message": message, "updated_at": int(time.time() * 1000)}).encode()
    tmp = d / ".status.json.tmp"
    try:
        st = os.lstat(tmp)
    except FileNotFoundError:
        pass
    else:
        if stat.S_ISDIR(st.st_mode):
            os.rmdir(tmp)
        else:
            os.unlink(tmp)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    try:
        with os.fdopen(fd, "wb") as f:
            os.fchmod(f.fileno(), 0o644)
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, d / "status.json")
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _clear_symlinked_updates(data_dir: Path) -> Path:
    """updates/ is a symlink: remove only the link, recreate a real dir and record failure so the path unit clears."""
    link = Path(data_dir) / "updates"
    if not os.path.islink(link):
        raise UpdateError("updates is not a real directory")
    os.unlink(link)
    owner = os.lstat(data_dir)
    os.mkdir(link, 0o750)
    with contextlib.suppress(PermissionError):
        os.chown(link, owner.st_uid, owner.st_gid)
    write_status(data_dir, "failed", None, None, "updates directory was a symlink; removed it")
    log.error("updates/ was a symlink; link removed")
    raise UpdateError("updates was a symlink (removed)")


def _consume_request(data_dir: Path) -> tuple[Optional[str], Optional[str]]:
    """Read and remove request.json (§12.1). Returns (version, refusal_reason). UpdateError if updates/ is unsafe."""
    try:
        d = _updates_dir(data_dir)
    except UpdateError:
        d = _clear_symlinked_updates(data_dir)
    path = d / "request.json"
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None, None
    reason: Optional[str] = None
    data: Optional[bytes] = None
    if not stat.S_ISREG(st.st_mode):
        reason = "request.json is not a regular file"
    else:
        data = _read_small(path, MAX_REQUEST)
        if data is None:
            reason = "request.json unreadable or too large"
    # remove the entry itself, whatever it is
    try:
        if stat.S_ISDIR(st.st_mode):
            os.rmdir(path)
        else:
            os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError:
        reason = (reason or "request.json") + " (could not be removed)"
    if reason:
        return None, reason
    assert data is not None
    try:
        obj = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        return None, "request.json is not valid JSON"
    ver = obj.get("version") if isinstance(obj, dict) else None
    if not isinstance(ver, str) or not VERSION_RE.match(ver):
        return None, "request.json has a bad version"
    return ver, None


# ---------------------------------------------------------------- health

def _loopback_get(url: str) -> bytes:
    p = urlparse(url)
    if p.scheme not in ("http", "https") or (p.hostname or "").lower() not in ("127.0.0.1", "::1", "localhost"):
        raise UpdateError("health URL must be loopback")
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=ctx))
    try:
        with opener.open(url, timeout=5) as resp:
            return resp.read(MAX_STATUS)
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise UpdateError(f"health request failed: {type(e).__name__}") from None


def health_url_from_env(env: Mapping[str, str]) -> str:
    explicit = env.get("SECCAM_HEALTH_URL", "").strip()
    if explicit:
        return explicit
    scheme = "https" if env.get("SECCAM_CERT", "").strip() else "http"
    return f"{scheme}://127.0.0.1:{env.get('SECCAM_HTTPS_PORT', '').strip() or '8443'}/api/health"


def health_check(url: str, expected_version: str, timeout_s: float = 90.0, interval_s: float = 2.0,
                 get: Optional[Callable[[str], bytes]] = None,
                 sleep: Callable[[float], None] = time.sleep) -> bool:
    get = get or _loopback_get
    deadline = time.monotonic() + timeout_s
    attempts = int(timeout_s / interval_s) + 1 if interval_s > 0 else 1
    for i in range(attempts):
        try:
            data = json.loads(get(url))
            if isinstance(data, dict) and data.get("ok") is True and data.get("version") == expected_version:
                return True
        except (UpdateError, OSError, ValueError):
            pass
        if i + 1 >= attempts or time.monotonic() >= deadline:
            break
        sleep(interval_s)
    return False


def default_health(expected_version: str) -> bool:
    return health_check(health_url_from_env(os.environ), expected_version)


# ---------------------------------------------------------------- filesystem helpers

@contextlib.contextmanager
def _lock(app_root: Path) -> Iterator[bool]:
    """Yield True if the update lock was taken (non-blocking), False if another process holds it."""
    fd = os.open(Path(app_root) / ".update.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            if e.errno in (errno.EAGAIN, errno.EACCES):
                yield False
                return
            raise
        yield True
    finally:
        os.close(fd)  # closing releases the flock


def _sh(run: Callable[..., Any], argv: list[str], check: bool = True, timeout: Optional[float] = 900) -> None:
    try:
        run(argv, check=check, capture_output=True, text=True, timeout=timeout)
    except subprocess.CalledProcessError as e:
        tail = (e.stderr or "")[-300:] if isinstance(e.stderr, str) else ""
        raise UpdateError(f"command failed: {argv[0]} {argv[1] if len(argv) > 1 else ''} ({tail.strip()})") from None
    except (OSError, subprocess.SubprocessError) as e:
        raise UpdateError(f"command failed: {argv[0]} ({type(e).__name__})") from None


def _current_version(app_root: Path) -> Optional[str]:
    try:
        v = (Path(app_root) / "current" / "VERSION").read_text().strip()
    except OSError:
        return None
    return v if VERSION_RE.match(v) else None


def _flip(app_root: Path, version: str) -> None:
    app_root = Path(app_root)
    tmp = app_root / "current.new"
    with contextlib.suppress(FileNotFoundError):
        os.unlink(tmp)
    os.symlink(f"releases/{version}", tmp)
    os.replace(tmp, app_root / "current")


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with contextlib.suppress(FileNotFoundError):
        os.unlink(tmp)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _backups_dir(app_root: Path) -> Path:
    d = Path(app_root) / "backups"
    d.mkdir(mode=0o700, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def _journal_path(app_root: Path) -> Path:
    return Path(app_root) / "backups" / "update-journal.json"


def _drop_journal(app_root: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        os.unlink(_journal_path(app_root))


def _check_db_file(db: Path, data_dir: Path) -> None:
    """Defence in depth (the real protection is the privilege-dropped child): lstat only, never open."""
    st = os.lstat(db)
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.lstat(data_dir).st_uid:
        raise UpdateError("seccam.db is not a regular file owned by the data dir owner")


def _drop_to(uid: int, gid: int) -> None:
    """Drop root privileges to the data dir owner (no-op when not running as root)."""
    if os.geteuid() == 0:
        os.setgroups([])
        os.setgid(gid)
        os.setuid(uid)


def _fork_dropped(uid: int, gid: int, work: Callable[[int], None], *, feed: Optional[Path] = None,
                  sink: Optional[int] = None) -> None:
    """Run work(fd) in a child dropped to uid/gid. Root never opens a path inside data_dir.

    feed: the parent streams this (root-owned) file to the child, work gets the read end.
    sink: work gets a write end and the parent copies everything the child writes into this fd."""
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:  # child
        code = 1
        try:
            if feed is not None:
                os.close(w)
                fd = r
            else:
                os.close(r)
                fd = w
            _drop_to(uid, gid)
            work(fd)
            code = 0
        except BaseException as e:  # noqa: BLE001
            print(f"seccam-updater: db helper failed: {type(e).__name__}: {e}", file=sys.stderr)
        finally:
            os._exit(code)
    err: Optional[BaseException] = None
    try:
        if feed is not None:
            os.close(r)
            with os.fdopen(w, "wb") as pipe_w, open(feed, "rb") as src:
                try:
                    shutil.copyfileobj(src, pipe_w)
                except BrokenPipeError:
                    pass
        else:
            os.close(w)
            with os.fdopen(r, "rb") as pipe_r, os.fdopen(os.dup(sink), "wb") as dst:
                shutil.copyfileobj(pipe_r, dst)
    except BaseException as e:  # noqa: BLE001
        err = e
    _, status = os.waitpid(pid, 0)
    if err is not None:
        raise err
    if status != 0:
        raise UpdateError("database helper failed")


def _child_serialize(db: str, data_dir: str, fd: int) -> None:
    conn = sqlite3.connect(f"file:{quote(db)}?mode=ro", uri=True)
    try:
        if hasattr(conn, "serialize"):
            data = conn.serialize()
        else:  # Python < 3.11 builds without serialize: back up into a scratch db owned by the dropped user
            import tempfile
            with tempfile.TemporaryDirectory(dir=data_dir) as td:
                tmp = os.path.join(td, "b.db")
                dst = sqlite3.connect(tmp)
                try:
                    conn.backup(dst)
                finally:
                    dst.close()
                with open(tmp, "rb") as f:
                    data = f.read()
    finally:
        conn.close()
    with os.fdopen(fd, "wb") as out:
        out.write(data)


def _backup_db(db: Path, dest: Path, data_dir: Path) -> None:
    _check_db_file(db, data_dir)
    owner = os.lstat(data_dir)
    with contextlib.suppress(FileNotFoundError):
        os.unlink(dest)
    out = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        _fork_dropped(owner.st_uid, owner.st_gid, lambda fd: _child_serialize(str(db), str(data_dir), fd), sink=out)
        os.fsync(out)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(dest)
        raise
    finally:
        os.close(out)


def _child_restore(data_dir: str, fd: int) -> None:
    base = Path(data_dir)
    for suffix in ("-wal", "-shm"):
        p = base / f"seccam.db{suffix}"
        try:
            st = os.lstat(p)
        except FileNotFoundError:
            continue
        if stat.S_ISDIR(st.st_mode):
            raise UpdateError(f"{p.name} is a directory")
        os.unlink(p)  # regular file, or a planted symlink (only the link is removed)
    db = base / "seccam.db"
    mode = 0o600
    try:
        st = os.lstat(db)
        if stat.S_ISREG(st.st_mode):
            mode = stat.S_IMODE(st.st_mode)
    except FileNotFoundError:
        pass
    tmp = base / ".seccam.db.restore"
    try:
        st = os.lstat(tmp)
    except FileNotFoundError:
        pass
    else:
        if stat.S_ISDIR(st.st_mode):
            raise UpdateError("restore tmp path is a directory")
        os.unlink(tmp)
    ofd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(ofd, "wb") as out, os.fdopen(fd, "rb") as src:
            shutil.copyfileobj(src, out)
            out.flush()
            os.fsync(out.fileno())
            os.fchmod(out.fileno(), mode)
        os.replace(tmp, db)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def restore_db(backup: Path, data_dir: Path) -> None:
    """Root only reads the (root-owned) backup and lstat()s data_dir; every write inside data_dir is done by a
    child dropped to the data dir owner."""
    data_dir = Path(data_dir)
    owner = os.lstat(data_dir)
    _fork_dropped(owner.st_uid, owner.st_gid, lambda fd: _child_restore(str(data_dir), fd), feed=Path(backup))


def _unit_files_changed(release: Path) -> bool:
    deploy = release / "deploy"
    if not deploy.is_dir():
        return False
    for f in sorted(list(deploy.glob("*.service")) + list(deploy.glob("*.path"))):
        try:
            if (SYSTEMD_DIR / f.name).read_bytes() != f.read_bytes():
                return True
        except OSError:
            return True
    return False


def _prune(app_root: Path, keep: set[str]) -> None:
    app_root = Path(app_root)
    rel_dir = app_root / "releases"
    versions: list[tuple[tuple[int, int, int], Path]] = []
    for p in rel_dir.iterdir():
        if p.name.startswith(".tmp-"):
            shutil.rmtree(p, ignore_errors=True)
        elif VERSION_RE.match(p.name):
            versions.append((parse_version(p.name), p))
    versions.sort(reverse=True)
    protected = set(keep) | {p.name for _, p in versions[:3]}
    used_venvs: set[str] = set()
    for _, p in versions:
        if p.name in protected:
            with contextlib.suppress(OSError):
                used_venvs.add(os.path.basename(os.readlink(p / "venv")))
        else:
            shutil.rmtree(p, ignore_errors=True)
    venvs = app_root / "venvs"
    if venvs.is_dir():
        for v in venvs.iterdir():
            if v.name not in used_venvs:
                shutil.rmtree(v, ignore_errors=True)
    backups = sorted((p for p in (app_root / "backups").glob("pre-*.db") if p.is_file()),
                     key=lambda p: p.stat().st_mtime, reverse=True)
    for p in backups[3:]:
        with contextlib.suppress(OSError):
            os.unlink(p)


# ---------------------------------------------------------------- apply / rollback / recover

def _safe_status(data_dir: Path, state: str, version: Optional[str], prev: Optional[str], msg: str) -> None:
    try:
        write_status(data_dir, state, version, prev, msg)
    except (UpdateError, OSError) as e:
        log.error("cannot write update status: %s", e)


def _rollback(app_root: Path, data_dir: Path, prev: Optional[str], backup: Optional[Path],
              run: Callable[..., Any], health: Callable[[str], bool], service: str) -> bool:
    """Stop, restore the DB, flip back, start, check health. True if the previous version is healthy."""
    try:
        _sh(run, ["systemctl", "stop", service], check=False)
        if backup is not None:
            restore_db(backup, data_dir)
        if prev is None:
            return False
        _flip(app_root, prev)
        _sh(run, ["systemctl", "start", service], check=False)
        return bool(health(prev))
    except Exception as e:  # noqa: BLE001 - rollback must report, not raise
        log.error("rollback failed: %s", e)
        return False


def _safe_backup_path(app_root: Path, value: Any) -> Optional[Path]:
    if not isinstance(value, str) or not value:
        return None
    p = Path(value)
    root = (Path(app_root) / "backups").resolve()
    try:
        if p.resolve().parent != root:
            return None
    except OSError:
        return None
    return p


def apply_update(app_root: Path, data_dir: Path, version: Optional[str] = None, *, repo: str = "fr3e1/seccam",
                 http_get: HttpGet = default_http_get, run: Callable[..., Any] = subprocess.run,
                 health: Callable[[str], bool] = default_health, service: str = "seccam.service",
                 force: bool = False) -> str:
    app_root, data_dir = Path(app_root), Path(data_dir)
    with _lock(app_root) as got:
        if not got:
            raise UpdateError("another update is running")
        prev = _current_version(app_root)
        ver: Optional[str] = version
        staging: Optional[Path] = None
        journaled = False
        backup: Optional[Path] = None
        try:
            _safe_status(data_dir, "downloading", version, prev, "checking for the release")
            if ver is not None and not VERSION_RE.match(ver):
                raise UpdateError("invalid version")
            rel = fetch_latest_release(http_get, repo)
            ver = ver or rel.version
            if ver != rel.version:
                raise UpdateError("requested version is not the latest release")
            if not force and prev is not None and not is_newer(ver, prev):
                raise UpdateError("release is not newer than the installed version")
            # 3 download + verify
            _safe_status(data_dir, "downloading", ver, prev, f"downloading {ver}")
            staging = app_root / "staging" / ver
            shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir(parents=True, mode=0o700)
            tar_name = f"seccam-{ver}.tar.gz"
            tar_path = staging / tar_name
            tar_path.write_bytes(http_get(rel.tarball_url, MAX_TARBALL))
            sums = parse_sha256sums(http_get(rel.sums_url, MAX_JSON).decode("utf-8", "replace"))
            _safe_status(data_dir, "verifying", ver, prev, "verifying checksum")
            verify_sha256(tar_path, expected_sha256(sums, tar_name))
            # 4 extract
            _safe_status(data_dir, "installing", ver, prev, "installing")
            releases = app_root / "releases"
            releases.mkdir(exist_ok=True)
            target = releases / ver
            reuse = False
            if _lexists(target):
                try:
                    validate_release_tree(target, ver)
                    reuse = True
                except UpdateError:
                    shutil.rmtree(target, ignore_errors=True)
            if not reuse:
                tmp = releases / f".tmp-{ver}"
                shutil.rmtree(tmp, ignore_errors=True)
                top = safe_extract_tarball(tar_path, tmp, f"seccam-{ver}")
                validate_release_tree(top, ver)
                os.rename(top, target)
                shutil.rmtree(tmp, ignore_errors=True)
            # 5 venv
            req = target / "requirements.txt"
            check_requirements(req)
            key = hashlib.sha256(req.read_bytes()).hexdigest()[:16]
            venv = app_root / "venvs" / key
            if not (venv / "bin" / "python").exists():
                (app_root / "venvs").mkdir(exist_ok=True)
                _sh(run, [sys.executable, "-m", "venv", str(venv)])
                _sh(run, [str(venv / "bin" / "pip"), "install", "--no-cache-dir", "--only-binary=:all:",
                          "--no-input", "--disable-pip-version-check", "-r", str(req)])
            link = target / "venv"
            if os.path.islink(link):
                os.unlink(link)
            elif _lexists(link):
                raise UpdateError("release contains a venv entry")
            os.symlink(f"../../venvs/{key}", link)
            # 6 DB backup + journal
            bdir = _backups_dir(app_root)
            db = data_dir / "seccam.db"
            if _lexists(db):
                backup = bdir / f"pre-{ver}.db"
                _backup_db(db, backup, data_dir)
            _atomic_write(_journal_path(app_root), json.dumps(
                {"from": prev, "to": ver, "db_backup": str(backup) if backup else None,
                 "started_at": int(time.time() * 1000)}).encode(), 0o600)
            journaled = True
        except (UpdateError, OSError, ValueError, sqlite3.Error) as e:
            msg = str(e) if isinstance(e, UpdateError) else f"{type(e).__name__}: {e}"
            log.error("update failed before the switch: %s", msg)
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)
            _safe_status(data_dir, "failed", ver, prev, msg)
            return "failed"

        assert ver is not None and journaled
        reason = ""
        try:
            _flip(app_root, ver)  # 7
            _safe_status(data_dir, "restarting", ver, prev, "restarting the service")
            _sh(run, ["systemctl", "restart", service])  # 8
            _safe_status(data_dir, "health_check", ver, prev, "waiting for the new version to answer")
            ok = bool(health(ver))  # 9
            if not ok:
                reason = "new version failed its health check"
        except Exception as e:  # noqa: BLE001 - any failure after the flip triggers a rollback
            ok = False
            reason = f"{type(e).__name__}: {e}"
        if ok:
            message = f"updated to {ver}"
            try:
                if _unit_files_changed(app_root / "releases" / ver):
                    message += "; unit files changed; re-run deploy/install.sh"
                _drop_journal(app_root)
                _prune(app_root, {ver} | ({prev} if prev else set()))
                shutil.rmtree(staging, ignore_errors=True)
            except OSError as e:
                log.warning("cleanup after update failed: %s", e)
            _safe_status(data_dir, "succeeded", ver, prev, message)
            return "succeeded"
        log.error("update to %s failed (%s); rolling back", ver, reason)
        healthy = _rollback(app_root, data_dir, prev, backup, run, health, service)
        _drop_journal(app_root)
        shutil.rmtree(staging, ignore_errors=True)
        if healthy:
            _safe_status(data_dir, "rolled_back", ver, prev, f"rolled back to {prev}: {reason}")
            return "rolled_back"
        _safe_status(data_dir, "failed", ver, prev, f"update failed and rollback did not recover: {reason}")
        return "failed"


def recover(app_root: Path, data_dir: Path, *, restart: bool = False, run: Callable[..., Any] = subprocess.run,
            health: Callable[[str], bool] = default_health, service: str = "seccam.service") -> Optional[str]:
    try:
        app_root, data_dir = Path(app_root), Path(data_dir)
        with _lock(app_root) as got:
            if not got:
                return None
            jp = _journal_path(app_root)
            if not _lexists(jp):
                return None
            try:
                journal = json.loads(jp.read_text())
            except (OSError, ValueError):
                log.error("unreadable update journal; removing it")
                _drop_journal(app_root)
                return None
            if not isinstance(journal, dict):
                _drop_journal(app_root)
                return None
            frm, to = journal.get("from"), journal.get("to")
            frm = frm if isinstance(frm, str) and VERSION_RE.match(frm) else None
            to = to if isinstance(to, str) and VERSION_RE.match(to) else None
            backup = _safe_backup_path(app_root, journal.get("db_backup"))
            if backup is not None and not backup.is_file():
                log.error("journal backup %s is missing; the database is left as is", backup)
                backup = None
            if restart:
                _sh(run, ["systemctl", "stop", service], check=False)
            if backup is not None:
                restore_db(backup, data_dir)
            if frm is not None:
                _flip(app_root, frm)
            if restart:
                _sh(run, ["systemctl", "start", service], check=False)
                if frm is not None and not health(frm):
                    log.error("previous version %s did not become healthy after recovery", frm)
            _drop_journal(app_root)
            _safe_status(data_dir, "rolled_back", to, frm, "recovered from an interrupted update")
            return "rolled_back"
    except Exception as e:  # noqa: BLE001 - recover must never raise
        print(f"seccam-updater: recover failed: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------- CLI

def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="seccam-updater")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pa = sub.add_parser("apply")
    pa.add_argument("--version")
    pa.add_argument("--force", action="store_true")
    pa.add_argument("--app-root", required=True)
    pa.add_argument("--data-dir", required=True)
    pa.add_argument("--service", default="seccam.service")
    pr = sub.add_parser("recover")
    pr.add_argument("--app-root", required=True)
    pr.add_argument("--data-dir", required=True)
    pc = sub.add_parser("check")
    pc.add_argument("--repo", default="fr3e1/seccam")
    args = ap.parse_args(argv)

    if args.cmd == "check":
        try:
            rel = fetch_latest_release(default_http_get, args.repo)
            print(f"{__version__} {rel.version} {'yes' if is_newer(rel.version, __version__) else 'no'}")
            return 0
        except (UpdateError, ValueError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 1

    app_root, data_dir = Path(args.app_root), Path(args.data_dir)
    if args.cmd == "recover":
        recover(app_root, data_dir)
        return 0

    try:
        req_version, refusal = _consume_request(data_dir)
    except UpdateError as e:
        log.error("cannot read update request: %s", e)
        return 1
    if refusal:
        log.error("update request refused: %s", refusal)
        _safe_status(data_dir, "failed", None, _current_version(app_root), f"update request refused: {refusal}")
        return 1
    recover(app_root, data_dir, restart=True, service=args.service)
    try:
        state = apply_update(app_root, data_dir, args.version or req_version, service=args.service,
                             force=args.force)
    except UpdateError as e:
        log.error("update not applied: %s", e)
        return 1
    return 0 if state == "succeeded" else 1


if __name__ == "__main__":
    sys.exit(main())
