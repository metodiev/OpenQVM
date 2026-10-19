# Contributing to OpenQVM

Thank you for contributing. OpenQVM values working software and honest reporting
over ambitious claims.

## Before you start

- Read [docs/architecture.md](docs/architecture.md) and
  [docs/development.md](docs/development.md).
- For security issues, follow [SECURITY.md](SECURITY.md), not a public issue.
- By contributing, you agree that your contributions are licensed under the
  Apache License 2.0.

## Development setup

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
source .venv/bin/activate
pip install -e ".[dev]"
```

Install QEMU if you want to exercise real virtualization. Unit and integration
tests do not require it.

## Workflow

1. Open or reference an issue that describes the behavior and acceptance criteria.
2. Create a focused branch.
3. Make small, reviewable commits with tests and documentation updates.
4. Run:
   ```bash
   ruff check src tests --select F,E9
   pytest tests/unit tests/integration -q
   ```
5. If you changed lifecycle, storage, networking or transfer code, run the real
   device test:
   ```bash
   OPENQVM_TEST_ISO=/path/to/alpine-virt.iso pytest tests/end_to_end -m e2e -q -s
   ```
6. Open a pull request describing what changed, what you ran, and what remains
   untested.

## Code guidelines

- Python 3.11+, type hints on public functions.
- No shell execution. Build argv lists and validate option values.
- Do not report success before QEMU/QMP/qemu-img confirms it.
- Do not fabricate metrics or capabilities. Use `None`/"Unavailable" and explain.
- Validate untrusted data at boundaries and keep path operations confined.
- Keep platform-specific code under `openqvm/platform`.
- Use atomic writes for persistent state.
- Add comments only where behavior is non-obvious (for example QEMU's snapshot
  fallback or QMP error semantics).
- Update documentation when interfaces or behavior change.

## Tests

- New validation or parsing logic needs unit tests.
- New orchestration logic needs an integration test with fakes.
- Features that depend on real QEMU behavior need an end-to-end test or a clear
  note that they were manually tested on a named platform.
- Never describe a skipped/e2e test as passing hardware acceleration on a
  platform you did not run it on.

## Commit messages

Use a short imperative subject, optionally a scope:

```
vm: reject hardware changes while QMP is unreachable
storage: cap package compression ratio
docs: document WHPX snapshot limitations
```

## Documentation

- User-facing behavior belongs in `README.md` and `docs/installation.md`.
- Internal structure and trust boundaries belong in `docs/architecture.md` and
  `docs/security.md`.
- Record completed and planned work in `ROADMAP.md`; keep it accurate.

## Code of conduct

Be respectful, assume good intent and focus on the technical problem. Harassment,
discrimination and disclosure of private information are not acceptable. Project
maintainers may remove contributions or participation that violate these
expectations.
