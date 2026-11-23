from pathlib import Path


def _read() -> str:
    try:
        text = (Path(__file__).resolve().parent.parent / "VERSION").read_text().strip()
    except OSError:
        return "0.0.0"
    return text or "0.0.0"


__version__: str = _read()
