# Operations runbook index

Status: **static response procedures; links do not prove alerts or paging are
deployed**.

Prometheus annotations use these repository URLs so an alert and its response
procedure change together. Page alerts require immediate on-call ownership;
ticket alerts require triage within the team service target. If telemetry is
missing, do not interpret an empty dashboard as a healthy zero.

| Alert | Severity / route | Runbook |
|---|---|---|
| `ImposbroQueryApiTargetDown` | critical / page | [Service availability](service-availability.md) |
| `ImposbroQueryApiTargetMissing` | critical / page | [Telemetry missing](telemetry-missing.md) |
| `ImposbroAvailabilityBudgetFastBurn` | critical / page | [Query API SLO burn](query-api-slo-burn.md) |
| `ImposbroAvailabilityBudgetSustainedBurn` | critical / page | [Query API SLO burn](query-api-slo-burn.md) |
| `ImposbroAvailabilityBudgetTicketBurn` | warning / ticket | [Query API SLO burn](query-api-slo-burn.md) |
| `ImposbroLatencyBudgetFastBurn` | critical / page | [Query API SLO burn](query-api-slo-burn.md) |
| `ImposbroLatencyBudgetSustainedBurn` | critical / page | [Query API SLO burn](query-api-slo-burn.md) |
| `ImposbroLatencyBudgetTicketBurn` | warning / ticket | [Query API SLO burn](query-api-slo-burn.md) |
| `ImposbroQueryApiElevated5xxRatio` | warning / ticket | [Query API SLO burn](query-api-slo-burn.md) |
| `ImposbroIndexingWorkerNotReady` | critical / page | [Service availability](service-availability.md) |
| `ImposbroIndexingDlqGrowth` | warning / ticket | [Outbox lag](outbox-lag.md) |
| `ImposbroIndexingConsumerLagSustained` | warning / ticket | [Outbox lag](outbox-lag.md) |
| `ImposbroIndexingConsumerLagCritical` | critical / page | [Outbox lag](outbox-lag.md) |
| `ImposbroUnresolvedDlq` | critical / page | [Outbox lag](outbox-lag.md) |
| `ImposbroIndexingOutboxBacklog` | warning / ticket | [Outbox lag](outbox-lag.md) |
| `ImposbroIndexingOutboxCritical` | critical / page | [Outbox lag](outbox-lag.md) |
| `ImposbroOutboxLagBudgetFastBurn` | critical / page | [Outbox lag](outbox-lag.md) |
| `ImposbroOutboxLagBudgetSustainedBurn` | critical / page | [Outbox lag](outbox-lag.md) |
| `ImposbroControlPlaneOutboxLag` | warning / ticket | [Outbox lag](outbox-lag.md) |
| `ImposbroControlPlaneOutboxCritical` | critical / page | [Outbox lag](outbox-lag.md) |
| `ImposbroConfigReplicaStale` | warning / ticket | [Configuration convergence](config-convergence.md) |
| `ImposbroConfigReplicaStaleCritical` | critical / page | [Configuration convergence](config-convergence.md) |
| `ImposbroConfigConvergenceBudgetFastBurn` | critical / page | [Configuration convergence](config-convergence.md) |
| `ImposbroConfigConvergenceBudgetSustainedBurn` | critical / page | [Configuration convergence](config-convergence.md) |
| `ImposbroRoutingRolloutFailed` | critical / page | [Routing rollout failure](routing-rollout-failure.md) |
| `ImposbroRoutingRollbackStalled` | critical / page | [Routing rollout failure](routing-rollout-failure.md) |
| `ImposbroRoutingRollbackWindowElapsed` | warning / ticket | [Routing rollout failure](routing-rollout-failure.md) |
| `ImposbroEnterpriseTelemetryMissing` | warning / ticket | [Telemetry missing](telemetry-missing.md) |
| `ImposbroAuditDeliveryUnconfigured` | critical / page | [Audit delivery](audit-delivery.md) |
| `ImposbroAuditDeliveryBacklog` | warning / ticket | [Audit delivery](audit-delivery.md) |
| `ImposbroAuditDeliveryFailure` | critical / page | [Audit delivery](audit-delivery.md) |

Supporting procedures:

- [Encrypted PostgreSQL backup and guarded restore](postgres-backup-restore.md)
- [Data deletion convergence](data-deletion-convergence.md)
- [Credential and trust-material rotation](credential-rotation.md)
- [Routing cutover failure and rollback](routing-rollout-failure.md)
- [Kubernetes disruption and load smoke](kubernetes-kind-enterprise-smoke.md)
- [Owned outbox retention](outbox-retention.md)

Every incident record should capture alert fingerprint, start/end UTC, affected
environment and revision, dashboard snapshot or query link, mitigations,
validation, customer impact, and follow-up owner. Redact credentials and
customer payloads.
