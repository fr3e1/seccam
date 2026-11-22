# Contributing

## Development setup

```bash
git clone https://github.com/fr3e1/seccam.git
cd seccam
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
```

## Guidelines

- All tests must pass (`pytest -q --timeout 30`)
- Keep dependencies minimal: no new Python packages without strong justification
- Frontend: vanilla ES2020 only — no frameworks, no CDNs, everything served locally
- Follow existing code style (type hints, dataclasses, async/await)
- Run `shellcheck` on any shell script changes

## Pull requests

- Small, focused changes
- Update documentation if behavior changes
- No emoji in code or docs
