# Contributing

## Development setup

```bash
git clone https://github.com/fr3e1/seccam.git
cd seccam
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements-dev.txt
.venv/bin/python -m pytest -q --timeout 60
node --test tests/js/*.test.mjs
```

## Guidelines

- All tests must pass (pytest and the node tests, see above)
- Keep dependencies minimal: no new Python packages without strong justification
- Frontend: vanilla ES2020 only — no frameworks, no CDNs, everything served locally
- Follow existing code style (type hints, dataclasses, async/await)
- Run `shellcheck` on any shell script changes

## Pull requests

- Small, focused changes
- Update documentation if behavior changes
- No emoji in code or docs
