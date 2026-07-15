# Service-level objectives

Status: **policy and monitoring design; not live SLO evidence**.

This document defines the initial enterprise objectives for IMPOSBRO. The
recording and alert rules in `monitoring/prometheus/rules/` implement the
request-based objectives and operational proxies described below. A production
claim additionally requires deployed exporters, retained metrics, validated
Alertmanager delivery, representative traffic, and an agreed reporting owner.

## Scope and service boundary

The user-facing service is the Query API data plane backed by PostgreSQL,
Kafka, Redis, the indexing worker, and Typesense. In-scope route templates are
both canonical `/api/v1/{search,documents,ingest}/...` routes and their
temporary unversioned compatibility aliases. Admin, health, readiness, metrics,
and OpenAPI endpoints are excluded from the user-journey SLI.

The SLO reporting window is a rolling 30 days. All calculations use server-side
Prometheus observations. Synthetic probes should be reported separately until
their traffic and failure semantics are formally approved.

## Objectives

| Capability | SLI and good event | Objective | Allowed budget | Current measurement |
|---|---|---:|---:|---|
| Data-plane availability | Eligible requests whose final status is not `5xx` | 99.9% | 0.1%; 43m12s only as a time-equivalent over 30d | `http_requests_total`, request-based |
| Data-plane latency | Eligible requests completed in `<= 1.0s` | 99.0% | 1% of eligible requests | `http_request_duration_seconds_bucket{le="1.0"}` |
| Durable indexing publication | Evaluation intervals where oldest unpublished indexing event is `<= 60s` | 99.9% target | 0.1% of observed intervals | PostgreSQL exporter operational proxy; formal per-event history not yet retained |
| Configuration convergence | Accepted revisions applied by every serving Query API replica within `30s` | 99.9% target | 0.1% of accepted revisions | Per-replica revision gap is an operational proxy; formal revision-duration history not yet retained |

Availability good events include expected `2xx`, `3xx`, and `4xx` responses;
client-caused `4xx` responses are not service failures. Requests aborted before
the application observes a status require load-balancer metrics and are not yet
part of the application-only SLI. Partial federated search responses currently
return a successful status and therefore count as available; report the
`partial` response field separately before proposing a stricter correctness SLO.

The time-equivalent availability budget is a planning aid. Compliance remains
request-based so low-traffic and high-traffic intervals are weighted by the
number of eligible requests.

## Burn-rate policy

Burn rate is observed bad-event ratio divided by the SLO error budget. A value
of `1` consumes the budget at exactly the sustainable rate. Alert only when a
short and long window agree, which makes pages responsive without triggering on
a single isolated sample.

| Response | Short window | Long window | Threshold | Approximate 30d budget consumed |
|---|---:|---:|---:|---:|
| Page: fast burn | 5m | 1h | `> 14.4x` in both | 2% in 1h |
| Page: sustained burn | 30m | 6h | `> 6x` in both | 5% in 6h |
| Ticket: medium burn | 2h | 1d | `> 3x` in both | 10% in 1d |
| Ticket: slow burn | 6h | 3d | `> 1x` in both | 10% in 3d |

Request-based page alerts also require more than `0.05` eligible requests per
second over five minutes. This suppresses statistically meaningless pages at
very low traffic; missing targets and synthetic checks must cover total outage
during idle periods. Ticket alerts intentionally retain visibility at lower
traffic.

Outbox age and configuration revision gap have direct threshold alerts plus
fast/sustained multi-window burn alerts derived from 30-second samples. These
proxy burn rates are operational early-warning signals, not evidence of the
per-event or per-revision objectives above. Promote those objectives to formal
compliance only after event timestamps and convergence completion are durably
recorded.

## Exclusions and budget adjustments

No incident is silently removed. An exclusion requires an approved change or
incident record, owner, exact time interval, affected SLI, and rationale. The
monthly report must show both raw and adjusted results. Candidate exclusions:

- approved disaster-recovery or failover exercises that intentionally remove
  service and have an agreed SLO treatment;
- verified measurement corruption, reported with the telemetry gap;
- traffic proven to be a malicious volumetric attack only when the security
  policy explicitly assigns that risk outside the service boundary.

Planned maintenance is not automatically excluded. Dependency failures count
when they prevent the IMPOSBRO data-plane journey.

## Ownership and response

- The on-call service owner responds to `notify=page` alerts and follows the
  linked runbook.
- The platform/observability owner handles scrape, exporter, recording-rule, and
  Alertmanager failures.
- The product/service owner approves objectives and monthly budget policy.
- Security and data owners approve backup, retention, erasure, and DR evidence.

When either request SLO has exhausted 50% of its budget before half the window,
the service owner opens a reliability action. At 100%, pause discretionary
releases unless the incident commander and product owner document an exception.

## Evidence required before claiming compliance

1. Prometheus successfully loads the rule file and retains at least the full
   reporting window (or writes to an approved long-term store).
2. Every Query API and worker replica is discovered; per-replica health probes
   do not traverse a load balancer.
3. PostgreSQL and JSON exporters run with least-privilege credentials.
4. Alertmanager routes page and ticket severities to tested destinations, with
   a recorded synthetic alert delivery.
5. Grafana panels return data for all required series and identify missing
   telemetry rather than displaying a misleading zero.
6. A monthly report records numerator, denominator, exclusions, budget burn,
   incidents, and owner sign-off.

Repository validation proves syntax and cross-file contracts only. It does not
satisfy any of the live-evidence requirements above.
