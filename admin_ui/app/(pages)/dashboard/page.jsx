'use client';

import { useEffect, useState } from 'react';
import { LayoutDashboard, Server, Database, GitBranch, ExternalLink } from 'lucide-react';
import Link from 'next/link';
import { api } from '../../lib/api';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '../../components/ui/Card';
import { cn } from '../../lib/utils';

function FeatureCard({ href, icon: Icon, iconColor, title, description, external = false }) {
  const CardWrapper = external ? 'a' : Link;
  const props = external
    ? { href, target: '_blank', rel: 'noopener noreferrer' }
    : { href };

  return (
    <CardWrapper {...props} className="block h-full group">
      <Card className="h-full transition-all duration-200 hover:bg-card hover:border-primary/40 hover:shadow-card-hover">
        <CardHeader className="flex flex-row items-start justify-between space-y-0">
          <div className="rounded-xl bg-primary/10 p-2.5 transition-colors group-hover:bg-primary/20">
            <Icon className={cn('h-6 w-6', iconColor || 'text-primary')} />
          </div>
          {external && (
            <ExternalLink className="h-4 w-4 text-muted-foreground opacity-70 group-hover:opacity-100" />
          )}
        </CardHeader>
        <CardContent className="pt-0">
          <CardTitle className="text-lg font-semibold">{title}</CardTitle>
          <CardDescription className="mt-1.5 text-sm leading-relaxed">
            {description}
          </CardDescription>
        </CardContent>
      </Card>
    </CardWrapper>
  );
}

export default function DashboardPage() {
  const [health, setHealth] = useState(null);
  const [stats, setStats] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    async function fetchData() {
      try {
        const [h, s] = await Promise.all([
          api.health.get().catch(() => null),
          api.stats.get().catch(() => null),
        ]);
        if (!cancelled) {
          setHealth(h);
          setStats(s);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    fetchData();
    const t = setInterval(fetchData, 15000);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, []);

  const status = health?.status ?? 'unknown';
  const clusters = stats?.clusters ?? health?.clusters ?? '—';
  const collections = stats?.collections ?? health?.collections ?? '—';

  return (
    <div className="space-y-10">
      <div className="border-b border-border pb-8">
        <h1 className="text-3xl font-bold tracking-tight text-foreground sm:text-4xl">
          Welcome to <span className="text-primary">IMPOSBRO</span> Search
        </h1>
        <p className="mt-3 max-w-2xl text-base text-muted-foreground sm:text-lg">
          Your central hub for managing a distributed, sharded search infrastructure.
          Built on Typesense with enterprise-grade federation, high availability, and
          comprehensive monitoring.
        </p>
      </div>

      <div className="grid gap-4 sm:grid-cols-3">
        <Card>
          <CardHeader className="pb-2">
            <CardDescription className="text-xs uppercase tracking-wider opacity-90">System Status</CardDescription>
            <CardTitle className="flex items-center gap-2 text-xl font-semibold">
              {loading ? (
                <span className="text-muted-foreground">Loading…</span>
              ) : (
                <>
                  <span
                    className={cn(
                      'h-2.5 w-2.5 rounded-full animate-pulse',
                      status === 'healthy' ? 'bg-emerald-500' : 'bg-amber-500'
                    )}
                  />
                  {status === 'healthy' ? 'Operational' : status === 'degraded' ? 'Degraded' : 'Unknown'}
                </>
              )}
            </CardTitle>
          </CardHeader>
          <CardContent>
            {health && (
              <p className="text-xs text-muted-foreground">
                Redis: {health.redis ?? '—'} · Kafka: {health.kafka ?? '—'}
              </p>
            )}
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardDescription className="text-xs uppercase tracking-wider opacity-90">Clusters</CardDescription>
            <CardTitle className="text-2xl font-semibold tabular-nums">{loading ? '—' : clusters}</CardTitle>
          </CardHeader>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardDescription className="text-xs uppercase tracking-wider opacity-90">Collections</CardDescription>
            <CardTitle className="text-2xl font-semibold tabular-nums">{loading ? '—' : collections}</CardTitle>
          </CardHeader>
        </Card>
      </div>

      <div className="grid gap-6 md:grid-cols-2 lg:grid-cols-4">
        <FeatureCard
          href="/clusters"
          icon={Server}
          iconColor="text-primary"
          title="Manage Clusters"
          description="Register and configure your federated Typesense clusters for distributed storage."
        />
        <FeatureCard
          href="/collections"
          icon={Database}
          iconColor="text-emerald-500"
          title="Manage Collections"
          description="Create and manage search indexes across all clusters with custom schemas."
        />
        <FeatureCard
          href="/routing"
          icon={GitBranch}
          iconColor="text-violet-500"
          title="Configure Routing"
          description="Define document-level sharding rules to distribute data across clusters."
        />
        <FeatureCard
          href="http://localhost:3000"
          icon={LayoutDashboard}
          iconColor="text-amber-500"
          title="View Monitoring"
          description="Open Grafana to monitor system health, performance, and metrics."
          external
        />
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-lg font-semibold">Architecture Overview</CardTitle>
          <CardDescription>How IMPOSBRO Search is built</CardDescription>
        </CardHeader>
        <CardContent>
          <div className="grid gap-6 text-sm md:grid-cols-3">
            <div className="rounded-lg border border-border bg-muted/30 p-4">
              <h4 className="font-semibold text-primary">Ingestion Pipeline</h4>
              <p className="mt-2 text-muted-foreground leading-relaxed">
                Documents are routed through Kafka for reliable, asynchronous indexing
                with guaranteed delivery.
              </p>
            </div>
            <div className="rounded-lg border border-border bg-muted/30 p-4">
              <h4 className="font-semibold text-emerald-500">Federated Search</h4>
              <p className="mt-2 text-muted-foreground leading-relaxed">
                Scatter-gather queries across all relevant clusters with automatic
                result merging and ranking.
              </p>
            </div>
            <div className="rounded-lg border border-border bg-muted/30 p-4">
              <h4 className="font-semibold text-violet-500">High Availability</h4>
              <p className="mt-2 text-muted-foreground leading-relaxed">
                Configuration state is stored in a 3-node Typesense HA cluster with
                Raft consensus.
              </p>
            </div>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
