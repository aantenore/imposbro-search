# Enterprise evidence artifacts

Run `scripts/e2e/run-enterprise-e2e.sh` to create a JSON artifact in this
directory. Every artifact records the exact Git commit, dirty-worktree flag,
dependency mode, scenario durations, and only non-sensitive assertions.
Database URLs, API keys, passwords, tokens, and URL credentials are redacted by
the writer before the atomic file replacement. The machine-readable contract is
`tests/enterprise/evidence.schema.json`.

Generated E2E, DR, Kind, benchmark, and checksum files are intentionally
gitignored. A committed mutable `*-latest` file cannot be bound to the commit
that contains it and would become stale on the next commit. Hosted workflows
retain immutable artifacts named by the exact candidate SHA; those artifacts,
not a checked-in local snapshot, are release evidence.

Initialization predeclares the exact scenario set required by the selected
dependency mode. An interrupted or partial run therefore remains `incomplete`,
and a non-zero harness exit is recorded as `failed`; missing scenarios can
never be finalized as `passed`.

When a Compose run fails after containers are created, the cleanup hook adds a
bounded, redacted application-log tail under `diagnostics` before teardown.
This preserves root-cause evidence without storing raw credentials.

Status semantics are strict:

- `passed`: the scenario executed and all assertions were observed;
- `failed`: it executed but an assertion or dependency failed;
- `not_run`: it could not execute in the current environment;
- top-level `incomplete`: at least one required scenario is `not_run`.

A top-level `passed` certifies only this integration harness contract. It is not
a production-environment certification; the provenance therefore always sets
`production_certification` to `false`. Production acceptance still requires the
deployment-specific security, HA, backup/restore, load, and disaster-recovery
evidence defined by the project delivery contract.

Local isolated run (requires an active Docker daemon):

```bash
E2E_DEPENDENCY_MODE=compose scripts/e2e/run-enterprise-e2e.sh
```

Static validation without a daemon never produces live-pass evidence:

```bash
E2E_DEPENDENCY_MODE=static \
E2E_EVIDENCE_BASENAME=enterprise-e2e-local-static.json \
scripts/e2e/run-enterprise-e2e.sh
```

For externally managed dependencies, export the documented `E2E_*` endpoints
and credentials plus executable, argument-free hooks:
`E2E_STOP_REDIS_HOOK`, `E2E_START_REDIS_HOOK`, and
`E2E_RESTART_QUERY_B_HOOK`. The cleanup trap always invokes the Redis start hook
after a successful stop, including on failure. External resources are never
deleted. Compose volumes are preserved unless `E2E_PRUNE_VOLUMES=true` is set
explicitly.
