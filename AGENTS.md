# Agent rules

openshell-tg adds Telegram approvals to OpenShell. It is not an extension of OpenShell.

- Read [docs/design.md](docs/design.md) before changing behaviour.
- Call only public `openshell` CLI commands, and only from `src/openshell_tg/osh.py`. Never patch, fork or reimplement OpenShell features.
- Rules, policies and credentials use OpenShell's native YAML. Do not invent a new rule format.
- Every user-facing text lives in `src/openshell_tg/messages.py`, in plain language, with one next step. No stack traces to the user.
- Never write secrets to disk in plain text, to logs, to SQLite or to Telegram messages.
- Keep it small: standard library plus PyYAML. Add a dependency only with a clear reason in the PR.
- Run `python -m pytest` before every commit. Tests mock the `openshell` CLI; they never need a real gateway.
