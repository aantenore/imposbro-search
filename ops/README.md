# IMPOSBRO operational assets

This directory contains deployer-facing examples, not proof of a live control.
Secrets and environment-specific destinations must remain in the platform
secret manager and deployment configuration.

- `postgres/backup-policy.example.env` documents non-secret backup policy and
  libpq variables.
- `../scripts/ops/backup-control-plane-postgres.sh` creates an encrypted custom
  PostgreSQL dump with manifest and checksum.
- `../scripts/ops/restore-control-plane-postgres.sh` verifies by default and
  restores only after empty-target, confirmation, and approval guardrails pass.
- `../scripts/ops/validate-ops-artifacts.sh` performs repository-level static
  checks and stubbed destructive-path tests.
- `../monitoring/alertmanager/alertmanager.enterprise.example.yml` supplies
  label-based page/ticket routing with secret-file destinations.
- `../docs/runbooks/` contains alert response and recovery procedures.

Production enablement still requires scheduled execution, secret injection,
off-site immutable storage, retention enforcement, exporter deployment,
Alertmanager routing, and captured restore-drill evidence.
