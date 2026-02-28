'use client';

import { cn } from '../lib/utils';

/**
 * Visual diagram of how document routing works.
 * Shows: Document → Rules (field=value → cluster) → Default cluster.
 */
export default function RoutingDiagram({ className, compact = false }) {
  return (
    <div
      className={cn(
        'rounded-lg border border-border bg-muted/20 p-4',
        compact && 'p-3',
        className
      )}
      role="img"
      aria-label="Routing flow: document is evaluated against rules in order; first match sends to that cluster; otherwise default cluster is used."
    >
      <p className="mb-3 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
        How routing works
      </p>
      <div className="flex flex-wrap items-center gap-2 text-sm">
        {/* Document */}
        <div className="flex items-center gap-1.5 rounded-md border border-border bg-card px-2.5 py-1.5">
          <span className="font-medium text-foreground">Document</span>
        </div>
        <Arrow />
        {/* Rules */}
        <div className="flex items-center gap-1.5 rounded-md border border-primary/40 bg-primary/5 px-2.5 py-1.5">
          <span className="text-foreground">Rule 1:</span>
          <code className="rounded bg-muted px-1 text-xs">field=value</code>
          <span className="text-muted-foreground">→</span>
          <span className="font-medium text-primary">Cluster A</span>
        </div>
        <span className="text-muted-foreground">…</span>
        <Arrow />
        {/* Default */}
        <div className="flex items-center gap-1.5 rounded-md border border-border bg-card px-2.5 py-1.5">
          <span className="text-muted-foreground">No match?</span>
          <span className="text-foreground">→</span>
          <span className="font-medium text-foreground">Default cluster</span>
        </div>
      </div>
      <p className="mt-2 text-xs text-muted-foreground">
        Rules are evaluated in order. First matching rule wins. If none match, the document goes to the default cluster.
      </p>
    </div>
  );
}

function Arrow() {
  return (
    <span className="shrink-0 text-muted-foreground" aria-hidden="true">
      →
    </span>
  );
}
