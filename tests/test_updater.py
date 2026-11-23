import hashlib
import io
import json
import os
import stat
import tarfile
import time
from pathlib import Path

import pytest

from server import updater as U
from server.updater import UpdateError

REPO = "fr3e1/seccam"
BASE = f"https://github.com/{REPO}"


# ------------------------------------------------------------ helpers

def release_json(version="2.1.0", **over):
    tag = f"v{version}"
    d = {
        "tag_name": tag, "published_at": "2026-01-01T00:00:00Z", "body": "notes",
        "html_url": f"{BASE}/releases/tag/{tag}",
        "assets": [
            {"name": f"seccam-{version}.tar.gz", "browser_download_url": f"{BASE}/releases/download/{tag}/seccam-{version}.tar.gz"},
            {"name": "SHA256SUMS", "browser_download_url": f"{BASE}/releases/download/{tag}/SHA256SUMS"},
        ],
    }
    d.update(over)
    return d


def make_tar(path: Path, version="2.1.0", top=None, extra=None, files=None):
    top = top or f"seccam-{version}"
    files = files if files is not None else {
        "VERSION": f"{version}\n", "requirements.txt": "fastapi>=0.110\n", "server/app.py": "x",
        "server/main.py": "x", "static/index.html": "<html>", "deploy/seccam.service": "[Unit]\n",
    }
    with tarfile.open(path, "w:gz") as tf:
        for name, content in files.items():
            data = content.encode()
            ti = tarfile.TarInfo(f"{top}/{name}")
            ti.size = len(data)
            ti.mode = 0o644
            tf.addfile(ti, io.BytesIO(data))
        for ti, data in (extra or []):
            tf.addfile(ti, io.BytesIO(data) if data is not None else None)
    return path


def tinfo(name, type_=tarfile.REGTYPE, data=b"", linkname="", mode=0o644):
    ti = tarfile.TarInfo(name)
    ti.type = type_
    ti.size = len(data) if type_ == tarfile.REGTYPE else 0
    ti.linkname = linkname
    ti.mode = mode
    return ti


@pytest.fixture(params=["with_filter", "without_filter"])
def both_filters(request, monkeypatch):
    if request.param == "without_filter":
        monkeypatch.delattr(tarfile, "data_filter", raising=False)
    return request.param


class Env:
    def __init__(self, tmp_path):
        self.app = tmp_path / "opt"
        self.data = tmp_path / "data"
        self.app.mkdir()
        self.data.mkdir()
        (self.data / "updates").mkdir()
        self.calls = []
        self.tar = tmp_path / "rel.tar.gz"

    def install_current(self, version="2.0.0"):
        rel = self.app / "releases" / version
        rel.mkdir(parents=True)
        (rel / "VERSION").write_text(version + "\n")
        os.symlink(f"releases/{version}", self.app / "current")

    def run(self, argv, **kw):
        self.calls.append(list(argv))
        if len(argv) >= 3 and argv[1:3] == ["-m", "venv"]:
            (Path(argv[3]) / "bin").mkdir(parents=True)
            (Path(argv[3]) / "bin" / "python").write_text("")

    def http_get(self, version="2.1.0", tamper=False):
        make_tar(self.tar, version)
        body = self.tar.read_bytes()
        digest = hashlib.sha256(body).hexdigest()
        if tamper:
            digest = "0" * 64
        tag = f"v{version}"
        urls = {
            f"{U.GITHUB_API}/repos/{REPO}/releases/latest": json.dumps(release_json(version)).encode(),
            f"{BASE}/releases/download/{tag}/seccam-{version}.tar.gz": body,
            f"{BASE}/releases/download/{tag}/SHA256SUMS": f"{digest}  seccam-{version}.tar.gz\n".encode(),
        }

        def get(url, max_bytes):
            if url not in urls:
                raise UpdateError("unexpected url")
            return urls[url]
        return get

    def apply(self, health=lambda v: True, version=None, **kw):
        return U.apply_update(self.app, self.data, version, http_get=kw.pop("http_get", None) or self.http_get(),
                              run=self.run, health=health, **kw)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(U, "SYSTEMD_DIR", tmp_path / "systemd")
    return Env(tmp_path)


# ------------------------------------------------------------ versions & release

def test_parse_and_compare():
    assert U.parse_version("v1.2.3") == (1, 2, 3)
    assert U.parse_version("10.0.1") == (10, 0, 1)
    for bad in ("1.2", "1.2.3-rc1", "", "a.b.c", "v"):
        with pytest.raises(ValueError):
            U.parse_version(bad)
    assert U.is_newer("2.0.10", "2.0.9")
    assert not U.is_newer("2.0.0", "2.0.0")
    assert not U.is_newer("1.9.9", "2.0.0")


def test_fetch_latest_good():
    seen = []

    def get(url, mx):
        seen.append((url, mx))
        return json.dumps(release_json()).encode()
    r = U.fetch_latest_release(get, REPO)
    assert seen == [(f"{U.GITHUB_API}/repos/{REPO}/releases/latest", U.MAX_JSON)]
    assert (r.version, r.tag, r.notes) == ("2.1.0", "v2.1.0", "notes")
    assert r.tarball_url.endswith("/seccam-2.1.0.tar.gz") and r.sums_url.endswith("/SHA256SUMS")


@pytest.mark.parametrize("mut", [
    lambda d: d.update(tag_name="v2.1.0-rc1"),
    lambda d: d.update(tag_name="latest"),
    lambda d: d.update(assets=d["assets"][:1]),
    lambda d: d.update(assets=d["assets"][1:]),
    lambda d: d.update(html_url="https://evil.example/x"),
    lambda d: d["assets"][0].update(browser_download_url="https://evil.example/seccam-2.1.0.tar.gz"),
    lambda d: d.update(assets="nope"),
])
def test_fetch_latest_rejects(mut):
    d = release_json()
    mut(d)
    with pytest.raises(UpdateError):
        U.fetch_latest_release(lambda u, m: json.dumps(d).encode(), REPO)


def test_fetch_latest_bad_json_and_http_error():
    with pytest.raises(UpdateError):
        U.fetch_latest_release(lambda u, m: b"not json", REPO)
    with pytest.raises(UpdateError):
        U.fetch_latest_release(lambda u, m: b"[]", REPO)

    def boom(u, m):
        raise UpdateError("HTTP error 404")
    with pytest.raises(UpdateError):
        U.fetch_latest_release(boom, REPO)


def test_notes_truncated():
    d = release_json(body="x" * 30000)
    assert len(U.fetch_latest_release(lambda u, m: json.dumps(d).encode(), REPO).notes) == 20000


# ------------------------------------------------------------ http hosts

def test_default_http_get_hosts():
    for url in ("http://api.github.com/x", "https://evil.example/x", "https://github.com.evil.example/x"):
        with pytest.raises(UpdateError):
            U.default_http_get(url, 10)


def test_default_http_get_redirects_validated(monkeypatch):
    import urllib.error
    import email.message

    class FakeOpener:
        def __init__(self):
            self.urls = []

        def open(self, req, timeout=None):
            self.urls.append(req.full_url)
            h = email.message.Message()
            h["Location"] = "http://objects.githubusercontent.com/x" if len(self.urls) == 1 else "https://evil.example/y"
            raise urllib.error.HTTPError(req.full_url, 302, "found", h, None)
    op = FakeOpener()
    monkeypatch.setattr(U.urllib.request, "build_opener", lambda *a: op)
    with pytest.raises(UpdateError):
        U.default_http_get("https://github.com/a", 10)
    assert op.urls == ["https://github.com/a"]  # http redirect target never requested


def test_default_http_get_redirect_to_non_allowlisted(monkeypatch):
    import urllib.error
    import email.message

    class FakeOpener:
        def open(self, req, timeout=None):
            h = email.message.Message()
            h["Location"] = "https://evil.example/y"
            raise urllib.error.HTTPError(req.full_url, 302, "found", h, None)
    monkeypatch.setattr(U.urllib.request, "build_opener", lambda *a: FakeOpener())
    with pytest.raises(UpdateError, match="allowed hosts"):
        U.default_http_get("https://github.com/a", 10)


# ------------------------------------------------------------ checksums

def test_parse_sha256sums_and_expected():
    h = "a" * 64
    H = "B" * 64
    s = U.parse_sha256sums(f"# c\n\n{h}  one.tar.gz\n{H} *two.tar.gz\nbadline\n")
    assert s == {"one.tar.gz": h, "two.tar.gz": "b" * 64}
    assert U.expected_sha256(s, "one.tar.gz") == h
    with pytest.raises(UpdateError, match="no checksum for three.tar.gz"):
        U.expected_sha256(s, "three.tar.gz")
    with pytest.raises(UpdateError):
        U.expected_sha256(s, "one.tar")


def test_verify_sha256(tmp_path):
    f = tmp_path / "f"
    f.write_bytes(b"hello")
    U.verify_sha256(f, hashlib.sha256(b"hello").hexdigest())
    with pytest.raises(UpdateError, match="checksum mismatch"):
        U.verify_sha256(f, "0" * 64)


# ------------------------------------------------------------ tar extraction

def test_extract_ok(tmp_path, both_filters):
    t = make_tar(tmp_path / "a.tar.gz")
    top = U.safe_extract_tarball(t, tmp_path / "out", "seccam-2.1.0")
    assert top == tmp_path / "out" / "seccam-2.1.0"
    assert (top / "server" / "app.py").read_text() == "x"
    assert stat.S_IMODE((top / "VERSION").stat().st_mode) == 0o644
    assert stat.S_IMODE((top / "server").stat().st_mode) == 0o755
    U.validate_release_tree(top, "2.1.0")


def test_extract_exec_bit(tmp_path, both_filters):
    t = make_tar(tmp_path / "a.tar.gz", extra=[(tinfo("seccam-2.1.0/run.sh", data=b"#!", mode=0o755), b"#!"),
                                               (tinfo("seccam-2.1.0/odd", data=b"x", mode=0o011), b"x")])
    top = U.safe_extract_tarball(t, tmp_path / "out", "seccam-2.1.0")
    assert stat.S_IMODE((top / "run.sh").stat().st_mode) == 0o755
    assert stat.S_IMODE((top / "odd").stat().st_mode) == 0o644


def _bad(name, type_=tarfile.REGTYPE, linkname="", data=b"x"):
    return [(tinfo(name, type_, data, linkname), data if type_ == tarfile.REGTYPE else None)]


@pytest.mark.parametrize("extra", [
    _bad("/etc/passwd"),
    _bad("seccam-2.1.0/../evil"),
    _bad("seccam-2.1.0/a/../../evil"),
    _bad("seccam-2.1.0/ln", tarfile.SYMTYPE, "/etc/passwd"),
    _bad("seccam-2.1.0/hl", tarfile.LNKTYPE, "seccam-2.1.0/VERSION"),
    _bad("seccam-2.1.0/dev", tarfile.CHRTYPE),
    _bad("seccam-2.1.0/blk", tarfile.BLKTYPE),
    _bad("seccam-2.1.0/fifo", tarfile.FIFOTYPE),
    _bad("other-top/file"),
    _bad("seccam-2.1.0/sp ace"),
    _bad("seccam-2.1.0/VERSION"),  # duplicate
    _bad("seccam-2.1.0//x"),
    _bad("seccam-2.1.0/./x"),
])
def test_extract_rejects(tmp_path, both_filters, extra):
    t = make_tar(tmp_path / "a.tar.gz", extra=extra)
    with pytest.raises(UpdateError):
        U.safe_extract_tarball(t, tmp_path / "out", "seccam-2.1.0")
    assert not (tmp_path / "evil").exists()


def test_extract_wrong_top_and_oversize_and_count(tmp_path, monkeypatch, both_filters):
    t = make_tar(tmp_path / "a.tar.gz")
    with pytest.raises(UpdateError):
        U.safe_extract_tarball(t, tmp_path / "o1", "seccam-9.9.9")
    monkeypatch.setattr(U, "MAX_EXTRACTED", 10)
    with pytest.raises(UpdateError, match="too large"):
        U.safe_extract_tarball(t, tmp_path / "o2", "seccam-2.1.0")
    monkeypatch.setattr(U, "MAX_EXTRACTED", 10**9)
    monkeypatch.setattr(U, "MAX_MEMBERS", 2)
    with pytest.raises(UpdateError, match="too many"):
        U.safe_extract_tarball(t, tmp_path / "o3", "seccam-2.1.0")


def test_extract_not_a_tarball(tmp_path):
    p = tmp_path / "x.tar.gz"
    p.write_bytes(b"garbage")
    with pytest.raises(UpdateError):
        U.safe_extract_tarball(p, tmp_path / "o", "seccam-2.1.0")


def test_extract_refuses_existing_file(tmp_path):
    t = make_tar(tmp_path / "a.tar.gz")
    out = tmp_path / "out"
    (out / "seccam-2.1.0").mkdir(parents=True)
    (out / "seccam-2.1.0" / "VERSION").write_text("planted")
    with pytest.raises(UpdateError):
        U.safe_extract_tarball(t, out, "seccam-2.1.0")


def test_validate_release_tree(tmp_path):
    t = make_tar(tmp_path / "a.tar.gz")
    top = U.safe_extract_tarball(t, tmp_path / "o", "seccam-2.1.0")
    with pytest.raises(UpdateError):
        U.validate_release_tree(top, "2.2.0")
    (top / "static" / "index.html").unlink()
    with pytest.raises(UpdateError, match="index.html"):
        U.validate_release_tree(top, "2.1.0")


def test_check_requirements(tmp_path):
    p = tmp_path / "r.txt"
    p.write_text("# c\n\nfastapi>=0.1,<1\nnumpy\n")
    U.check_requirements(p)
    for bad in ("--index-url x", "-r other.txt", "pkg @ file:///x", "https://x/y.whl", "git+ssh://x/y"):
        p.write_text("fastapi\n" + bad + "\n")
        with pytest.raises(UpdateError):
            U.check_requirements(p)


# ------------------------------------------------------------ status / request

def test_read_status_idle(tmp_path):
    s = U.read_status(tmp_path)
    assert s == {"state": "idle", "version": None, "previous_version": None, "message": "", "updated_at": None}


def test_write_and_read_status(tmp_path):
    (tmp_path / "updates").mkdir()
    U.write_status(tmp_path, "installing", "2.1.0", "2.0.0", "hi")
    s = U.read_status(tmp_path)
    assert (s["state"], s["version"], s["previous_version"], s["message"]) == ("installing", "2.1.0", "2.0.0", "hi")
    assert set(s) == {"state", "version", "previous_version", "message", "updated_at"}
    assert stat.S_IMODE((tmp_path / "updates" / "status.json").stat().st_mode) == 0o644


def test_stale_status(tmp_path):
    (tmp_path / "updates").mkdir()
    U.write_status(tmp_path, "downloading", "2.1.0", "2.0.0", "x")
    now = time.time()
    assert U.read_status(tmp_path, now)["state"] == "downloading"
    s = U.read_status(tmp_path, now + U.STALE_AFTER_S + 5)
    assert s["state"] == "failed" and "stale" in s["message"]
    U.write_status(tmp_path, "succeeded", "2.1.0", "2.0.0", "x")
    assert U.read_status(tmp_path, now + 10 * U.STALE_AFTER_S)["state"] == "succeeded"


def test_status_garbage_is_idle(tmp_path):
    (tmp_path / "updates").mkdir()
    (tmp_path / "updates" / "status.json").write_text('{"state":"bogus"}')
    assert U.read_status(tmp_path)["state"] == "idle"
    (tmp_path / "updates" / "status.json").write_text("nope")
    assert U.read_status(tmp_path)["state"] == "idle"


def test_request_update_basic_and_pending(tmp_path):
    U.request_update(tmp_path, "2.1.0", "admin")
    d = tmp_path / "updates"
    assert stat.S_IMODE(d.stat().st_mode) == 0o750
    f = d / "request.json"
    assert stat.S_IMODE(f.stat().st_mode) == 0o640
    body = json.loads(f.read_text())
    assert body["version"] == "2.1.0" and body["requested_by"] == "admin" and isinstance(body["requested_at"], int)
    assert U.read_status(tmp_path)["state"] == "requested"
    with pytest.raises(UpdateError, match="update pending"):
        U.request_update(tmp_path, "2.1.0", "admin")
    assert not [p for p in d.iterdir() if p.name.endswith(".tmp")]


def test_request_update_validation_and_nonterminal(tmp_path):
    with pytest.raises(ValueError):
        U.request_update(tmp_path, "v2.1.0", "a")
    with pytest.raises(ValueError):
        U.request_update(tmp_path, "2.1", "a")
    (tmp_path / "updates").mkdir()
    U.write_status(tmp_path, "health_check", "2.1.0", "2.0.0", "")
    with pytest.raises(UpdateError, match="update pending"):
        U.request_update(tmp_path, "2.1.0", "a")
    U.write_status(tmp_path, "failed", "2.1.0", "2.0.0", "")
    U.request_update(tmp_path, "2.1.0", "a")


def test_request_update_stale_counts_terminal(tmp_path, monkeypatch):
    (tmp_path / "updates").mkdir()
    U.write_status(tmp_path, "installing", "2.1.0", "2.0.0", "")
    real = time.time
    monkeypatch.setattr(U.time, "time", lambda: real() + U.STALE_AFTER_S + 60)
    U.request_update(tmp_path, "2.1.0", "a")


def test_symlinked_updates_dir(tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    os.symlink(target, data / "updates")
    with pytest.raises(UpdateError):
        U.write_status(data, "failed", None, None, "x")
    with pytest.raises(UpdateError):
        U.request_update(data, "2.1.0", "a")
    assert list(target.iterdir()) == []
    assert U.read_status(data)["state"] == "idle"


def test_symlinked_status_json_replaced(tmp_path):
    (tmp_path / "updates").mkdir()
    victim = tmp_path / "x"
    victim.write_text("precious")
    os.symlink(victim, tmp_path / "updates" / "status.json")
    assert U.read_status(tmp_path)["state"] == "idle"
    U.write_status(tmp_path, "succeeded", "2.1.0", "2.0.0", "ok")
    st = os.lstat(tmp_path / "updates" / "status.json")
    assert stat.S_ISREG(st.st_mode)
    assert victim.read_text() == "precious"


def test_symlinked_status_tmp_unlinked(tmp_path):
    (tmp_path / "updates").mkdir()
    victim = tmp_path / "x"
    victim.write_text("precious")
    os.symlink(victim, tmp_path / "updates" / ".status.json.tmp")
    U.write_status(tmp_path, "succeeded", "2.1.0", "2.0.0", "ok")
    assert victim.read_text() == "precious"
    assert U.read_status(tmp_path)["state"] == "succeeded"


# ------------------------------------------------------------ refused request.json via main

def _main_apply(env, *extra):
    return U.main(["apply", "--app-root", str(env.app), "--data-dir", str(env.data), *extra])


def _setup_refusal(env, kind, tmp_path):
    req = env.data / "updates" / "request.json"
    victim = tmp_path / "victim"
    victim.write_text("precious")
    if kind == "symlink":
        os.symlink(victim, req)
    elif kind == "oversize":
        req.write_text(json.dumps({"version": "2.1.0", "pad": "x" * 5000}))
    elif kind == "badjson":
        req.write_text("{nope")
    elif kind == "badversion":
        req.write_text(json.dumps({"version": "../../etc"}))
    elif kind == "notobject":
        req.write_text("[1]")
    elif kind == "dir":
        req.mkdir()
    return req, victim


@pytest.mark.parametrize("kind", ["symlink", "oversize", "badjson", "badversion", "notobject", "dir"])
def test_refused_request(env, tmp_path, kind):
    req, victim = _setup_refusal(env, kind, tmp_path)
    env.install_current()
    assert _main_apply(env) == 1
    assert not os.path.lexists(req)
    assert victim.read_text() == "precious"
    s = U.read_status(env.data)
    assert s["state"] == "failed" and "refused" in s["message"]
    assert env.calls == []


def test_main_apply_symlinked_updates_dir(env, tmp_path):
    os.rmdir(env.data / "updates")
    target = tmp_path / "t"
    target.mkdir()
    (target / "request.json").write_text(json.dumps({"version": "2.1.0"}))
    os.symlink(target, env.data / "updates")
    assert _main_apply(env) == 1
    assert (target / "request.json").exists()  # target untouched
    assert sorted(p.name for p in target.iterdir()) == ["request.json"]
    up = env.data / "updates"
    assert not up.is_symlink() and up.is_dir()  # only the link was removed
    assert U.read_status(env.data)["state"] == "failed"


def test_apply_symlinked_db_fails_before_flip(env, tmp_path):
    env.install_current("2.0.0")
    victim = tmp_path / "victim.db"
    victim.write_bytes(b"secret")
    os.symlink(victim, env.data / "seccam.db")
    assert env.apply() == "failed"
    assert os.readlink(env.app / "current") == "releases/2.0.0"
    assert not list((env.app / "backups").glob("pre-*.db"))
    assert victim.read_bytes() == b"secret"
    assert "not a regular file" in U.read_status(env.data)["message"]
    assert ["systemctl", "restart", "seccam.service"] not in env.calls


# ------------------------------------------------------------ health

def test_health_url_from_env():
    assert U.health_url_from_env({"SECCAM_HEALTH_URL": "http://127.0.0.1:1/h"}) == "http://127.0.0.1:1/h"
    assert U.health_url_from_env({"SECCAM_CERT": "/c", "SECCAM_HTTPS_PORT": "443"}) == "https://127.0.0.1:443/api/health"
    assert U.health_url_from_env({"SECCAM_CERT": "/c"}) == "https://127.0.0.1:8443/api/health"
    assert U.health_url_from_env({"SECCAM_HTTPS_PORT": "80", "SECCAM_HTTP_PORT": "9"}) == "http://127.0.0.1:80/api/health"
    assert U.health_url_from_env({"SECCAM_CERT": "  "}) == "http://127.0.0.1:8443/api/health"
    assert U.health_url_from_env({}) == "http://127.0.0.1:8443/api/health"


def test_health_check_polls():
    answers = [UpdateError("down"), b"{bad", json.dumps({"ok": True, "version": "1.0.0"}).encode(),
               json.dumps({"ok": False, "version": "2.0.0"}).encode(),
               json.dumps({"ok": True, "version": "2.0.0"}).encode()]
    sleeps = []

    def get(url):
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a
    assert U.health_check("u", "2.0.0", get=get, sleep=sleeps.append)
    assert len(sleeps) == 4


def test_health_check_gives_up():
    sleeps = []
    assert not U.health_check("u", "2.0.0", timeout_s=10, interval_s=2, get=lambda u: b"{}", sleep=sleeps.append)
    assert 1 <= len(sleeps) <= 6


def test_default_health_get_loopback_only():
    with pytest.raises(UpdateError):
        U._loopback_get("https://example.com/api/health")
    with pytest.raises(UpdateError):
        U._loopback_get("file:///etc/passwd")


# ------------------------------------------------------------ apply_update

def test_apply_succeeds(env):
    env.install_current("2.0.0")
    for v in ("1.0.0", "1.1.0", "1.2.0"):  # old releases to prune
        (env.app / "releases" / v).mkdir()
    (env.data / "seccam.db").write_bytes(b"")
    import sqlite3
    c = sqlite3.connect(env.data / "seccam.db")
    c.execute("create table t(x)")
    c.execute("insert into t values (7)")
    c.commit()
    c.close()
    healthy = []
    assert env.apply(health=lambda v: healthy.append(v) or True) == "succeeded"
    assert healthy == ["2.1.0"]
    assert os.readlink(env.app / "current") == "releases/2.1.0"
    assert (env.app / "current" / "VERSION").read_text().strip() == "2.1.0"
    assert ["systemctl", "restart", "seccam.service"] in env.calls
    pip = [c for c in env.calls if c[0].endswith("/pip")][0]
    assert "--only-binary=:all:" in pip and "--no-cache-dir" in pip and "-r" in pip
    assert (env.app / "releases" / "2.1.0" / "venv").is_symlink()
    assert (env.app / "releases" / "2.1.0" / "venv" / "bin" / "python").exists()
    s = U.read_status(env.data)
    assert s["state"] == "succeeded" and s["version"] == "2.1.0" and s["previous_version"] == "2.0.0"
    assert not (env.app / "backups" / "update-journal.json").exists()
    assert not (env.app / "staging" / "2.1.0").exists()
    names = sorted(p.name for p in (env.app / "releases").iterdir())
    assert "2.1.0" in names and "2.0.0" in names and "1.0.0" not in names  # newest 3 kept
    assert (env.app / "backups" / "pre-2.1.0.db").exists()
    assert stat.S_IMODE((env.app / "backups").stat().st_mode) == 0o700
    # backups/journal never in the data dir
    assert sorted(p.name for p in env.data.iterdir()) == ["seccam.db", "updates"]


def test_apply_reuses_venv_and_release(env):
    env.install_current("2.0.0")
    assert env.apply() == "succeeded"
    n_pip = len([c for c in env.calls if c[0].endswith("/pip")])
    assert n_pip == 1
    # a second apply of the same version with --force reuses release + venv
    assert env.apply(force=True) == "succeeded"
    assert len([c for c in env.calls if c[0].endswith("/pip")]) == 1


def test_apply_not_newer(env):
    env.install_current("2.1.0")
    assert env.apply() == "failed"
    assert os.readlink(env.app / "current") == "releases/2.1.0"
    assert U.read_status(env.data)["state"] == "failed"
    assert env.apply(force=True) == "succeeded"


def test_apply_version_mismatch(env):
    env.install_current("2.0.0")
    assert env.apply(version="2.5.0") == "failed"
    assert env.calls == []


def test_apply_checksum_mismatch(env):
    env.install_current("2.0.0")
    assert env.apply(http_get=env.http_get(tamper=True)) == "failed"
    assert os.readlink(env.app / "current") == "releases/2.0.0"
    assert not (env.app / "releases" / "2.1.0").exists()
    assert "checksum" in U.read_status(env.data)["message"]


def test_apply_missing_sums_entry(env):
    env.install_current("2.0.0")
    inner = env.http_get()

    def get(url, mx):
        if url.endswith("SHA256SUMS"):
            return ("a" * 64 + "  other.tar.gz\n").encode()
        return inner(url, mx)
    assert env.apply(http_get=get) == "failed"
    assert "no checksum for seccam-2.1.0.tar.gz" in U.read_status(env.data)["message"]


def test_apply_bad_requirements(env):
    env.install_current("2.0.0")
    make_tar(env.tar, files={"VERSION": "2.1.0\n", "requirements.txt": "--index-url http://x\n", "server/app.py": "x",
                             "server/main.py": "x", "static/index.html": "x"})
    body = env.tar.read_bytes()
    base = env.http_get()

    def get(url, mx):
        if url.endswith(".tar.gz"):
            return body
        if url.endswith("SHA256SUMS"):
            return f"{hashlib.sha256(body).hexdigest()}  seccam-2.1.0.tar.gz\n".encode()
        return base(url, mx)
    assert env.apply(http_get=get) == "failed"
    assert not any(c[0].endswith("/pip") for c in env.calls)
    assert os.readlink(env.app / "current") == "releases/2.0.0"


def test_apply_health_failure_rolls_back(env):
    env.install_current("2.0.0")
    db = env.data / "seccam.db"
    import sqlite3
    c = sqlite3.connect(db)
    c.execute("create table t(x)")
    c.execute("insert into t values (1)")
    c.commit()
    c.close()
    original = db.read_bytes()
    checked = []

    def health(v):
        checked.append(v)
        if v == "2.1.0":
            db.write_bytes(b"corrupted by new version")
            (env.data / "seccam.db-wal").write_bytes(b"wal")
            return False
        return True
    assert env.apply(health=health) == "rolled_back"
    assert checked == ["2.1.0", "2.0.0"]
    assert os.readlink(env.app / "current") == "releases/2.0.0"
    c = sqlite3.connect(db)
    assert c.execute("select x from t").fetchall() == [(1,)]
    c.close()
    assert not (env.data / "seccam.db-wal").exists()
    assert ["systemctl", "stop", "seccam.service"] in env.calls
    assert ["systemctl", "start", "seccam.service"] in env.calls
    assert not (env.app / "backups" / "update-journal.json").exists()
    s = U.read_status(env.data)
    assert s["state"] == "rolled_back" and s["version"] == "2.1.0"
    assert original  # sanity


def test_apply_restart_error_rolls_back(env):
    env.install_current("2.0.0")
    base_run = env.run

    def run(argv, **kw):
        base_run(argv, **kw)
        if argv[:2] == ["systemctl", "restart"]:
            raise OSError("boom")
    env.run = run
    assert env.apply() == "rolled_back"
    assert os.readlink(env.app / "current") == "releases/2.0.0"


def test_apply_rollback_unhealthy_is_failed(env):
    env.install_current("2.0.0")
    assert env.apply(health=lambda v: False) == "failed"
    assert not (env.app / "backups" / "update-journal.json").exists()
    assert U.read_status(env.data)["state"] == "failed"


def test_apply_lock_held(env):
    import fcntl
    env.install_current("2.0.0")
    fd = os.open(env.app / ".update.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        with pytest.raises(UpdateError):
            env.apply()
        assert env.calls == []
    finally:
        os.close(fd)
    assert env.apply() == "succeeded"


def test_apply_unit_files_changed_message(env):
    env.install_current("2.0.0")
    assert env.apply() == "succeeded"
    assert "re-run deploy/install.sh" in U.read_status(env.data)["message"]
    unit_dir = env.app.parent / "systemd"
    unit_dir.mkdir()
    (unit_dir / "seccam.service").write_text("[Unit]\n")
    assert env.apply(force=True) == "succeeded"
    assert "re-run" not in U.read_status(env.data)["message"]


def test_apply_without_db(env):
    env.install_current("2.0.0")
    assert env.apply() == "succeeded"
    assert not (env.app / "backups" / "pre-2.1.0.db").exists()


# ------------------------------------------------------------ restore_db / recover

def _journal(env, backup, frm="2.0.0", to="2.1.0"):
    (env.app / "backups").mkdir(exist_ok=True)
    (env.app / "backups" / "update-journal.json").write_text(json.dumps(
        {"from": frm, "to": to, "db_backup": str(backup) if backup else None, "started_at": 1}))


def test_restore_db(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "seccam.db").write_bytes(b"new")
    os.chmod(data / "seccam.db", 0o640)
    (data / "seccam.db-wal").write_bytes(b"w")
    (data / "seccam.db-shm").write_bytes(b"s")
    bk = tmp_path / "b.db"
    bk.write_bytes(b"old")
    U.restore_db(bk, data)
    assert (data / "seccam.db").read_bytes() == b"old"
    assert stat.S_IMODE((data / "seccam.db").stat().st_mode) == 0o640
    assert sorted(p.name for p in data.iterdir()) == ["seccam.db"]


def test_restore_db_planted_symlinks(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    victim = tmp_path / "victim"
    victim.write_text("precious")
    os.symlink(victim, data / "seccam.db-wal")
    os.symlink(victim, data / ".seccam.db.restore")
    os.symlink(victim, data / "seccam.db")
    bk = tmp_path / "b.db"
    bk.write_bytes(b"old")
    U.restore_db(bk, data)
    assert victim.read_text() == "precious"
    assert (data / "seccam.db").read_bytes() == b"old" and not (data / "seccam.db").is_symlink()


def test_recover_no_journal(env):
    assert U.recover(env.app, env.data) is None


def test_recover_restart_false_no_systemctl(env):
    env.install_current("2.0.0")
    (env.app / "releases" / "2.1.0").mkdir()
    os.unlink(env.app / "current")
    os.symlink("releases/2.1.0", env.app / "current")
    (env.data / "seccam.db").write_bytes(b"new")
    bk = env.app / "backups" / "pre-2.1.0.db"
    (env.app / "backups").mkdir()
    bk.write_bytes(b"old")
    _journal(env, bk)
    assert U.recover(env.app, env.data, restart=False, run=env.run, health=lambda v: 1 / 0) == "rolled_back"
    assert env.calls == []
    assert os.readlink(env.app / "current") == "releases/2.0.0"
    assert (env.data / "seccam.db").read_bytes() == b"old"
    assert not (env.app / "backups" / "update-journal.json").exists()
    s = U.read_status(env.data)
    assert s["state"] == "rolled_back" and "interrupted" in s["message"]


def test_recover_restart_true_calls_systemctl(env):
    env.install_current("2.0.0")
    (env.app / "backups").mkdir()
    _journal(env, None)
    seen = []
    assert U.recover(env.app, env.data, restart=True, run=env.run, health=lambda v: seen.append(v) or True) == "rolled_back"
    assert env.calls == [["systemctl", "stop", "seccam.service"], ["systemctl", "start", "seccam.service"]]
    assert seen == ["2.0.0"]


def test_recover_noop_while_locked(env):
    import fcntl
    env.install_current("2.0.0")
    (env.app / "backups").mkdir()
    _journal(env, None)
    fd = os.open(env.app / ".update.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        assert U.recover(env.app, env.data, run=env.run) is None
    finally:
        os.close(fd)
    assert (env.app / "backups" / "update-journal.json").exists()


def test_recover_ignores_backup_outside_backups(env, tmp_path):
    env.install_current("2.0.0")
    (env.data / "seccam.db").write_bytes(b"live")
    evil = tmp_path / "evil.db"
    evil.write_bytes(b"evil")
    (env.app / "backups").mkdir()
    _journal(env, evil)
    assert U.recover(env.app, env.data) == "rolled_back"
    assert (env.data / "seccam.db").read_bytes() == b"live"


def test_recover_never_raises(env):
    (env.app / "backups").mkdir()
    (env.app / "backups" / "update-journal.json").write_text("{bad")
    assert U.recover(env.app, env.data) is None
    assert U.recover(env.app / "missing", env.data) is None


# ------------------------------------------------------------ CLI

def test_main_recover_always_zero(env):
    assert U.main(["recover", "--app-root", str(env.app / "nope"), "--data-dir", str(env.data)]) == 0
    assert U.main(["recover", "--app-root", str(env.app), "--data-dir", str(env.data)]) == 0


def test_main_apply_uses_request_version(env, monkeypatch):
    env.install_current("2.0.0")
    (env.data / "updates" / "request.json").write_text(json.dumps({"version": "2.1.0"}))
    seen = {}

    def fake_apply(app, data, version, **kw):
        seen["version"], seen["force"] = version, kw["force"]
        return "succeeded"
    monkeypatch.setattr(U, "apply_update", fake_apply)
    assert _main_apply(env) == 0
    assert seen == {"version": "2.1.0", "force": False}
    assert not (env.data / "updates" / "request.json").exists()
    monkeypatch.setattr(U, "apply_update", lambda *a, **k: "rolled_back")
    assert _main_apply(env, "--version", "2.2.0", "--force") == 1


def test_main_apply_error_returns_1(env, monkeypatch):
    def boom(*a, **k):
        raise UpdateError("locked")
    monkeypatch.setattr(U, "apply_update", boom)
    assert _main_apply(env) == 1


def test_main_check(monkeypatch, capsys):
    monkeypatch.setattr(U, "fetch_latest_release",
                        lambda http_get=None, repo=REPO: U.ReleaseInfo("99.0.0", "v99.0.0", "", "", "", "", ""))
    assert U.main(["check"]) == 0
    out = capsys.readouterr().out.split()
    assert out[1] == "99.0.0" and out[2] == "yes"


def test_main_check_error(monkeypatch):
    def boom(*a, **k):
        raise UpdateError("offline")
    monkeypatch.setattr(U, "fetch_latest_release", boom)
    assert U.main(["check"]) == 1


# ------------------------------------------------------------ privilege-dropped DB I/O

def _guard_parent(monkeypatch, data_dir):
    """Fail if the parent (root) process opens/writes anything inside data_dir or opens the DB in sqlite."""
    import sqlite3
    parent = os.getpid()
    inside = str(data_dir)
    real_open, real_replace, real_connect = os.open, os.replace, sqlite3.connect

    def chk(path):
        if os.getpid() == parent and str(path).startswith(inside):
            raise AssertionError(f"parent touched {path}")

    def o(path, *a, **k):
        chk(path)
        return real_open(path, *a, **k)

    def r(src, dst, *a, **k):
        chk(src)
        chk(dst)
        return real_replace(src, dst, *a, **k)

    def c(target, *a, **k):
        chk(str(target).replace("file:", "", 1))
        return real_connect(target, *a, **k)
    monkeypatch.setattr(os, "open", o)
    monkeypatch.setattr(os, "replace", r)
    monkeypatch.setattr(sqlite3, "connect", c)


def test_backup_and_restore_never_touch_data_dir_in_parent(env, monkeypatch):
    import sqlite3
    db = env.data / "seccam.db"
    c = sqlite3.connect(db)
    c.execute("create table t(x)")
    c.execute("insert into t values (5)")
    c.commit()
    c.close()
    _guard_parent(monkeypatch, env.data)
    bk = env.app / "pre.db"
    U._backup_db(db, bk, env.data)
    assert stat.S_IMODE(bk.stat().st_mode) == 0o600
    (env.data / "seccam.db-wal").write_bytes(b"w")
    db.write_bytes(b"corrupt")
    U.restore_db(bk, env.data)
    monkeypatch.undo()
    c = sqlite3.connect(db)
    assert c.execute("select x from t").fetchall() == [(5,)]
    c.close()
    assert not (env.data / "seccam.db-wal").exists()


def test_backup_symlinked_db_refused_and_helper_failure(env, tmp_path):
    victim = tmp_path / "v"
    victim.write_bytes(b"s")
    os.symlink(victim, env.data / "seccam.db")
    with pytest.raises(UpdateError, match="regular file"):
        U._backup_db(env.data / "seccam.db", env.app / "pre.db", env.data)
    assert not (env.app / "pre.db").exists()
    # helper failure (not a database) surfaces as UpdateError and leaves no partial backup
    os.unlink(env.data / "seccam.db")
    (env.data / "seccam.db").write_bytes(b"this is not sqlite" * 100)
    with pytest.raises(UpdateError, match="helper failed"):
        U._backup_db(env.data / "seccam.db", env.app / "pre.db", env.data)
    assert not (env.app / "pre.db").exists()


def test_drop_to_when_root(monkeypatch):
    calls = []
    monkeypatch.setattr(U.os, "geteuid", lambda: 0)
    monkeypatch.setattr(U.os, "setgroups", lambda g: calls.append(("groups", list(g))))
    monkeypatch.setattr(U.os, "setgid", lambda g: calls.append(("gid", g)))
    monkeypatch.setattr(U.os, "setuid", lambda u: calls.append(("uid", u)))
    U._drop_to(990, 991)
    assert calls == [("groups", []), ("gid", 991), ("uid", 990)]  # groups, then gid, then uid
    calls.clear()
    monkeypatch.setattr(U.os, "geteuid", lambda: 1000)
    U._drop_to(990, 991)
    assert calls == []


def test_fork_dropped_child_runs_dropped(env, monkeypatch, tmp_path):
    """The child calls _drop_to before doing any work."""
    marker = tmp_path / "marker"
    parent = os.getpid()

    def fake_drop(uid, gid):
        assert os.getpid() != parent
        marker.write_text(f"{uid}:{gid}")
    monkeypatch.setattr(U, "_drop_to", fake_drop)
    (env.app / "b.db").write_bytes(b"x")
    U.restore_db(env.app / "b.db", env.data)
    st = os.lstat(env.data)
    assert marker.read_text() == f"{st.st_uid}:{st.st_gid}"
    assert (env.data / "seccam.db").read_bytes() == b"x"
