"""Deploy artefacts: scripts parse, unit files carry the section 16 directives, env example is complete,
make-release.sh produces a tarball the updater accepts."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from server import updater

ROOT = Path(__file__).resolve().parent.parent


def _read(rel: str) -> str:
    return (ROOT / rel).read_text()


def _shell_scripts() -> list[Path]:
    return sorted([*ROOT.glob("deploy/*.sh"), *ROOT.glob("scripts/*.sh"), *ROOT.glob("agent/*.sh")])


@pytest.mark.parametrize("script", _shell_scripts(), ids=lambda p: p.name)
def test_shell_syntax_and_strict_mode(script: Path) -> None:
    subprocess.run(["bash", "-n", str(script)], check=True)
    assert "set -euo pipefail" in script.read_text()


def test_main_service_directives() -> None:
    t = _read("deploy/seccam.service")
    for d in ("WorkingDirectory=/opt/seccam/current",
              "ExecStart=/opt/seccam/current/venv/bin/python -m server.main",
              "Restart=always", "ReadWritePaths=/var/lib/seccam", "ReadOnlyPaths=/opt/seccam",
              "ExecStartPre=+/opt/seccam/current/venv/bin/python -S -m server.updater recover "
              "--app-root /opt/seccam --data-dir /var/lib/seccam",
              "NoNewPrivileges=yes", "ProtectSystem=strict", "UMask=0077"):
        assert d in t


def test_update_path_directives() -> None:
    t = _read("deploy/seccam-update.path")
    assert "PathExists=/var/lib/seccam/updates/request.json" in t
    assert "Unit=seccam-update.service" in t
    unit, path = t.split("[Path]")
    assert "StartLimitIntervalSec=0" in unit
    assert "TriggerLimitIntervalSec=0" in path


def test_update_service_directives() -> None:
    t = _read("deploy/seccam-update.service")
    for d in ("Type=oneshot", "EnvironmentFile=/etc/seccam/seccam.env",
              "ExecStart=/opt/seccam/current/venv/bin/python -S -m server.updater apply "
              "--app-root /opt/seccam --data-dir /var/lib/seccam",
              "ExecStartPre=/opt/seccam/current/venv/bin/python -S -m server.updater recover "
              "--app-root /opt/seccam --data-dir /var/lib/seccam",
              "ExecStopPost=/bin/sleep 10", "WorkingDirectory=/opt/seccam/current",
              "TimeoutStartSec=900"):
        assert d in t
    assert "StartLimitIntervalSec=0" in t.split("[Service]")[0]
    assert "User=" not in t  # runs as root


def test_env_example_lists_every_config_var() -> None:
    used = set(re.findall(r"SECCAM_[A-Z_]+", _read("server/config.py")))
    listed = set(re.findall(r"SECCAM_[A-Z_]+", _read("deploy/seccam.env.example")))
    assert used - {"SECCAM_HEALTH_URL"} <= listed, sorted(used - listed)


def test_proxy_examples_preserve_host_and_upgrade_ws() -> None:
    nginx = _read("deploy/nginx.conf.example")
    assert "proxy_set_header Host $host;" in nginx
    assert "/ws/" in nginx and "Upgrade" in nginx and "127.0.0.1:8443" in nginx
    caddy = _read("deploy/Caddyfile.example")
    assert "reverse_proxy 127.0.0.1:8443" in caddy
    for t in (nginx, caddy):
        assert "SECCAM_PUBLIC_ORIGIN" in t and "SECCAM_TRUSTED_PROXIES=127.0.0.1" in t


def test_install_script_requirements() -> None:
    t = _read("deploy/install.sh")
    for s in ("--retention", "--trusted-proxies", "--public-origin", "--pin", "seccam-update.path",
              "SECCAM_UPDATE_MODE", "SECCAM_STATIC_DIR", "SECCAM_MODEL_PATH", "setup-token", "releases"):
        assert s in t


def test_proxmox_prints_setup_token_and_gitattributes() -> None:
    assert "setup-token" in _read("scripts/proxmox-install.sh")
    assert ".github/ export-ignore" in _read(".gitattributes")


def test_workflows() -> None:
    rel = _read(".github/workflows/release.yml")
    assert "v*.*.*" in rel and "make-release.sh" in rel and "gh release create" in rel
    ci = _read(".github/workflows/ci.yml")
    assert "v2-dev" in ci and "node --test tests/js/*.test.mjs" in ci and "agent/*.sh" in ci


def test_readme_documents_required_topics() -> None:
    t = _read("README.md")
    for s in ("Android Chrome", "Linux browser", "Headless agent", "reverse proxy", "SECCAM_PUBLIC_ORIGIN",
              "Updates", "Themes", "sudo -u seccam", "uv venv"):
        assert s in t


@pytest.mark.skipif(shutil.which("git") is None, reason="git missing")
def test_make_release_output_accepted_by_updater(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    for rel in ("server/app.py", "server/main.py", "static/index.html"):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text("x\n")
    (repo / "requirements.txt").write_text("fastapi\n")
    (repo / "VERSION").write_text("9.8.7\n")
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run([*git, "commit", "-q", "-m", "init"], cwd=repo, check=True)
    out = tmp_path / "dist"
    subprocess.run(["bash", str(ROOT / "scripts/make-release.sh"), "9.8.7", str(out)], cwd=repo, check=True)
    bad = subprocess.run(["bash", str(ROOT / "scripts/make-release.sh"), "1.0.0", str(out)], cwd=repo)
    assert bad.returncode != 0

    tarball = out / "seccam-9.8.7.tar.gz"
    sums = updater.parse_sha256sums((out / "SHA256SUMS").read_text())
    updater.verify_sha256(tarball, updater.expected_sha256(sums, tarball.name))
    dest = tmp_path / "x"
    top = updater.safe_extract_tarball(tarball, dest, "seccam-9.8.7")
    updater.validate_release_tree(top, "9.8.7")


def test_root_python_invocations_use_isolated_flag() -> None:
    for rel in ("deploy/seccam.service", "deploy/seccam-update.service"):
        for line in _read(rel).splitlines():
            if "server.updater" in line:
                assert "python -S -m server.updater" in line, line
    assert "ExecStartPost" not in _read("deploy/seccam-update.service")


def test_ca_key_never_group_readable() -> None:
    for rel in ("deploy/gen-certs.sh", "deploy/install.sh"):
        for n, line in enumerate(_read(rel).splitlines(), 1):
            if "ca.key" in line:
                assert not re.search(r"chmod\s+[0-7][1-7][0-7]|chown\s+root:seccam", line), f"{rel}:{n}: {line}"
    gen = _read("deploy/gen-certs.sh")
    assert 'chmod 600 "$DIR/ca.key"' in gen and 'chown root:root "$DIR/ca.key"' in gen
    assert 'chmod 640 "$DIR/server.key"' in gen and 'chown root:seccam "$DIR/server.key"' in gen
    assert 'nameConstraints=critical,' in gen and "permitted;DNS:localhost" in gen
    assert "permitted;IP:127.0.0.1/255.255.255.255" in gen
    inst = _read("deploy/install.sh")
    assert 'chmod 600 "$TLS_DIR/ca.key"' in inst and 'chown root:root "$TLS_DIR/ca.key"' in inst


def test_install_pip_wheels_only_no_pip_upgrade() -> None:
    t = _read("deploy/install.sh")
    assert "--only-binary=:all:" in t and "--upgrade pip" not in t


def test_workflows_hardened() -> None:
    sha_use = re.compile(r"uses:\s+\S+@[0-9a-f]{40}\s+#\s*v\d")
    for rel in (".github/workflows/ci.yml", ".github/workflows/release.yml"):
        t = _read(rel)
        uses = [ln for ln in t.splitlines() if "uses:" in ln]
        assert uses and all(sha_use.search(ln) for ln in uses), rel
    ci = _read(".github/workflows/ci.yml")
    assert re.search(r"^permissions:\n  contents: read", ci, re.M)
    rel = _read(".github/workflows/release.yml")
    assert re.search(r"^permissions: \{\}", rel, re.M)
    test_job, publish = rel.split("  publish:")
    assert "contents: read" in test_job and "pip install" in test_job and "pytest" in test_job
    assert "needs: test" in publish and "contents: write" in publish
    assert "persist-credentials: false" in publish and "pip" not in publish
    assert "make-release.sh" in publish and "gh release create" in publish
