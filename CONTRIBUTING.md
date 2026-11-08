# Contributing to Lakeloom

Thanks for your interest! Please read the [Code of Conduct](CODE_OF_CONDUCT.md) first.

## Development setup

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12.

```bash
git clone https://github.com/elcioabrahao/lakeloom
cd lakeloom
uv sync
```

## Checks

```bash
uv run pytest                 # unit tests (contract tests are excluded by default)
uv run pytest -m contract     # needs the docker compose dev stack
uv run ruff check
uv run ruff format --check
uv run pyright
```

### Troubleshooting (macOS)

If `uv run` cannot import `lakeloom` (macOS marks `.venv` files with the hidden flag), run
`chflags -R nohidden .venv`.

## Commits and pull requests

- Use [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `docs:`, `chore:` ...).
- Sign off every commit with the Developer Certificate of Origin: `git commit -s`.
- Reference requirement IDs from `REQUIREMENTS.md` (e.g. `FR-GIT-03`, `NFR-SEC-01`) in commit
  messages, module docstrings and test names (`test_fr_git_03_...`).
- Add or update tests with every behaviour change, and update `CHANGELOG.md` under `[Unreleased]`.

## Architecture decisions

Non-trivial or ambiguous decisions are recorded as ADRs in `docs/decisions/`. Copy
`docs/decisions/0000-template.md` to the next number (`NNNN-short-title.md`), fill in Context,
Decision and Consequences, and include it in your pull request.

## Security

Report vulnerabilities privately; see [SECURITY.md](SECURITY.md).
