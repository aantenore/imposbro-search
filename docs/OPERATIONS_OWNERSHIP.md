# Operations and support ownership contract

This repository defines roles, not organization-specific people. A deployment
is not accepted until its release record resolves every role below to an
on-call group, escalation target and approved runbook location. Empty,
individual-only or shared-mailbox-only ownership fails handover.

| Role | Accountable scope | Required handover evidence |
| --- | --- | --- |
| Service owner | Query API/Admin UI/worker behavior, API compatibility, SLO and incident commander | Service catalog ID, primary/secondary on-call, SLO approval, release/rollback authority |
| Platform owner | Kubernetes, ingress, DNS, PostgreSQL, network policy, secret sync and capacity | Cluster/environment IDs, maintenance window, access boundary, HA/restore and node-drain evidence |
| Streaming owner | Kafka availability, ACL/SASL, partitioning, retention, consumer lag and DLQ custody | Topic/ACL inventory, partition budget, retention approval, escalation and replay procedure |
| Search-data owner | Typesense clusters, schemas, tenant placement, document retention and erasure | Cluster inventory, residency/classification, deletion approver, capacity and backup responsibility |
| Identity/security owner | IdP clients/claims, workload credentials, threat/risk acceptance, audit sink and incident response | Client/audience/issuer IDs, scope mapping, rotation owner, WORM/SIEM destination, accepted-risk expiry |
| Observability owner | Collector, logs/metrics/traces, dashboards, alert routing and evidence retention | Backend IDs, retention/access policy, tested page/ticket routes and telemetry-missing procedure |
| Release owner | Branch/tag protection, CI exceptions, image/SBOM/attestation promotion and rollback | Protected ruleset, required checks, authorized releasers, GHCR policy and consumer verification record |
| Support lead | Intake, severity, customer communication, escalation and post-incident follow-up | Support hours, severity matrix, paging bridge, communication templates and response targets |
| Data protection/legal | Classification, retention, legal hold, residency, DSAR and backup expiry | Approved policy version, hold source, erasure/restore-suppression owner and review date |

## Severity and escalation baseline

- **SEV-1:** cross-tenant access, credential exposure, audit integrity loss,
  unrecoverable control-plane corruption, or broad outage. Page service,
  security and platform owners immediately; freeze mutations/releases and open
  the security/incident bridge.
- **SEV-2:** sustained SLO burn, rising unbounded lag, failed routing migration,
  partial shard outage beyond the approved window, restore failure or broken
  alert delivery. Page the service owner and affected dependency owner.
- **SEV-3:** isolated error, bounded retry/DLQ item, capacity forecast breach or
  non-urgent control gap. Create an owned ticket within the team target.

The environment may use stricter targets. It must not downgrade security,
tenant isolation, audit integrity, backup unrecoverability or complete data
loss based only on current traffic volume.

## Handover record

For every environment, attach a machine-readable or service-catalog record with:

- environment, region/residency, service tier and customer impact class;
- Git commit, release manifest, immutable image digests and Helm values reference;
- each role's group, primary/secondary escalation and coverage hours;
- dependency/provider account IDs and their escalation procedures;
- dashboard, logs, traces, audit sink, pager route and runbook index links;
- latest load, node-drain, TLS/secret rotation, DR/RPO/RTO and alert-delivery evidence;
- known risks with owner, compensating control, expiry and approval;
- last review time and next review deadline.

Repository examples and local evidence deliberately contain no personal contact
details or production account IDs. Those belong in the organization-owned
service catalog with access control and periodic review.
