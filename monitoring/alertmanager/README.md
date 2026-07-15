# Alertmanager reference routing

`alertmanager.enterprise.example.yml` routes `notify="page"` alerts to an
urgent receiver and `notify="ticket"` alerts to a work-queue receiver. Webhook
URLs are read from mounted secret files; no destination is committed.

The example is not automatically deployed. Before production, configure the
organization's supported pager/ticket integration, HA peers and persistent
state, authentication/TLS, ownership, grouping, maintenance windows, and
escalation policy. Validate configuration with `amtool check-config`, inject a
synthetic critical and warning alert, capture delivery/resolution evidence, and
verify the receiver failure path. Never point a test at the production pager
without an approved drill window.
