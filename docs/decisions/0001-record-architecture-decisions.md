# 0001. Record architecture decisions

- Status: accepted
- Date: 2026-11-08
- Requirements: NFR-OSS-05, REQUIREMENTS.md section 0.6

## Context

The specification asks that ambiguities be resolved with the simplest option and written down.
We need a lightweight, reviewable place for those records. Separately, two bootstrap choices need
recording: the PyPI placeholder package `lakeloom` 0.0.1 was published with repository URLs under
`lakeloom/lakeloom`, which is not the organisation that actually owns the code; and that
placeholder occupies version 0.0.1.

## Decision

- ADRs live in `docs/decisions/` as `NNNN-short-title.md`, in MADR-lite form: Context, Decision,
  Consequences. `0000-template.md` is the template.
- The canonical repository is `https://github.com/elcioabrahao/lakeloom`, not the placeholder's
  `lakeloom/lakeloom`. Package metadata uses the former.
- The first real version is `0.1.0.dev0`, above the placeholder `0.0.1`, following semantic
  versioning pre-release conventions.

## Consequences

- Decisions are versioned alongside code and reviewed in pull requests.
- The next placeholder release on PyPI will carry corrected URLs; the old 0.0.1 metadata stays as is.
- If the project later moves to a GitHub organisation, a new ADR supersedes the URL decision.
