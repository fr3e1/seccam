import ipaddress
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path

_ORIGIN_RE = re.compile(r"^https?://[A-Za-z0-9.\-\[\]:]+$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


@dataclass(frozen=True)
class Config:
    secret: str
    data_dir: Path
    retention_days: int
    model_path: Path
    host: str
    https_port: int
    http_port: int
    cert: str
    key: str
    ca_cert: str
    static_dir: Path
    legacy_pin: str
    trusted_proxies: tuple[str, ...]
    public_origin: str
    cookie_secure: str
    update_mode: str
    update_repo: str
    session_ttl_days: int


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, "").strip() or default


def _int(name: str, default: int, lo: int, hi: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name}: must be an integer") from None
    if not lo <= value <= hi:
        raise ValueError(f"{name}: must be between {lo} and {hi}")
    return value


def _choice(name: str, default: str, choices: tuple[str, ...]) -> str:
    value = _env(name, default)
    if value not in choices:
        raise ValueError(f"{name}: must be one of: {', '.join(choices)}")
    return value


def load_config() -> Config:
    repo_root = Path(__file__).resolve().parent.parent

    data_dir = Path(_env("SECCAM_DATA_DIR", "./data")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    # Secret: env, else <data_dir>/secret (generated once, mode 0600). Reserved; no longer signs cookies.
    secret = _env("SECCAM_SECRET")
    if not secret:
        secret_file = data_dir / "secret"
        if secret_file.exists():
            secret = secret_file.read_text().strip()
        else:
            secret = secrets.token_hex(32)
            secret_file.write_text(secret)
            secret_file.chmod(0o600)

    retention_days = _int("SECCAM_RETENTION_DAYS", 14, 1, 365)
    model_path = Path(_env("SECCAM_MODEL_PATH", str(repo_root / "models" / "yolox_nano.onnx"))).resolve()
    host = _env("SECCAM_HOST", "0.0.0.0")
    https_port = _int("SECCAM_HTTPS_PORT", 8443, 1, 65535)
    http_port = _int("SECCAM_HTTP_PORT", 8080, 1, 65535)
    static_dir = Path(_env("SECCAM_STATIC_DIR", str(repo_root / "static"))).resolve()

    proxies = []
    for item in _env("SECCAM_TRUSTED_PROXIES").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            proxies.append(str(ipaddress.ip_network(item, strict=False)))
        except ValueError:
            raise ValueError(f"SECCAM_TRUSTED_PROXIES: invalid IP or CIDR: {item}") from None

    public_origin = _env("SECCAM_PUBLIC_ORIGIN")
    if public_origin:
        public_origin = public_origin.rstrip("/")
        if not _ORIGIN_RE.match(public_origin):
            raise ValueError("SECCAM_PUBLIC_ORIGIN: must look like https://host[:port]")

    update_repo = _env("SECCAM_UPDATE_REPO", "fr3e1/seccam")
    if not _REPO_RE.match(update_repo):
        raise ValueError("SECCAM_UPDATE_REPO: must look like owner/name")

    return Config(
        secret=secret,
        data_dir=data_dir,
        retention_days=retention_days,
        model_path=model_path,
        host=host,
        https_port=https_port,
        http_port=http_port,
        cert=_env("SECCAM_CERT"),
        key=_env("SECCAM_KEY"),
        ca_cert=_env("SECCAM_CA_CERT"),
        static_dir=static_dir,
        legacy_pin=_env("SECCAM_PIN"),
        trusted_proxies=tuple(proxies),
        public_origin=public_origin,
        cookie_secure=_choice("SECCAM_COOKIE_SECURE", "auto", ("auto", "true", "false")),
        update_mode=_choice("SECCAM_UPDATE_MODE", "off", ("off", "systemd")),
        update_repo=update_repo,
        session_ttl_days=_int("SECCAM_SESSION_TTL_DAYS", 30, 1, 365),
    )
