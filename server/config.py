import os
import secrets
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    pin: str
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


def load_config() -> Config:
    # PIN: empty string if not set (no default)
    pin = os.getenv("SECCAM_PIN", "")

    # Data directory
    data_dir = Path(os.getenv("SECCAM_DATA_DIR", "./data")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    # Secret: generate once, store in data_dir/secret with mode 0600
    secret_file = data_dir / "secret"
    if os.getenv("SECCAM_SECRET", "").strip():
        secret = os.environ["SECCAM_SECRET"].strip()
    elif secret_file.exists():
        secret = secret_file.read_text().strip()
    else:
        secret = secrets.token_hex(32)
        secret_file.write_text(secret)
        secret_file.chmod(0o600)

    # Retention days
    retention_days = int(os.getenv("SECCAM_RETENTION_DAYS", "14"))

    # Model path default relative to repo root (parent of server/)
    repo_root = Path(__file__).resolve().parent.parent
    model_path = Path(os.getenv("SECCAM_MODEL_PATH", str(repo_root / "models" / "yolox_nano.onnx"))).resolve()

    # Network
    host = os.getenv("SECCAM_HOST", "0.0.0.0")
    https_port = int(os.getenv("SECCAM_HTTPS_PORT", "8443"))
    http_port = int(os.getenv("SECCAM_HTTP_PORT", "8080"))

    # Certs
    cert = os.getenv("SECCAM_CERT", "")
    key = os.getenv("SECCAM_KEY", "")
    ca_cert = os.getenv("SECCAM_CA_CERT", "")

    # Static dir
    static_dir = Path(os.getenv("SECCAM_STATIC_DIR", str(repo_root / "static"))).resolve()

    return Config(
        pin=pin,
        secret=secret,
        data_dir=data_dir,
        retention_days=retention_days,
        model_path=model_path,
        host=host,
        https_port=https_port,
        http_port=http_port,
        cert=cert,
        key=key,
        ca_cert=ca_cert,
        static_dir=static_dir,
    )
