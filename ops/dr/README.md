# Isolated PostgreSQL DR and retention exercise

`run-live-postgres-dr.sh` creates two disposable PostgreSQL 17 containers on an
internal Docker network, migrates and seeds the source, exercises dry-run and
applied outbox retention, creates a real age-encrypted custom-format backup,
and restores it transactionally into the empty target.

Run only when no other local integration stack is competing for Docker:

```text
ops/dr/run-live-postgres-dr.sh
```

Optional non-secret targets are `DR_RPO_TARGET_SECONDS`,
`DR_RTO_TARGET_SECONDS`, `DR_RUN_ID`, and `DR_EVIDENCE_BASENAME`. Secrets and
temporary age identity material are generated at runtime, never printed, and
removed with the containers and volumes. The report is written to
`docs/evidence/dr-*.json` with a SHA-256 sidecar and mode 0600.

The report proves only this isolated, quiesced synthetic-data contract. It does
not exercise production scheduling, immutable storage, key-custody recovery,
region failover, application traffic, or cross-store deletion convergence.
