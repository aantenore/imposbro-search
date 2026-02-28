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
    <CardWrapper {...props} className="block h-full">
      <Card className="h-full transition-colors hover:bg-accent/50 hover:border-primary/30">
        <CardHeader className="flex flex-row items-start justify-between space-y-0">
          <Icon className={cn('h-10 w-10', iconColor)} />
          {external && (
            <ExternalLink className="h-4 w-4 text-muted-foreground" />
          )}
        </CardHeader>
        <CardContent className="pt-0">
          <CardTitle className="text-xl">{title}</CardTitle>
          <CardDescription className="mt-2 leading-relaxed">
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
      <div>
        <h1 className="text-4xl font-bold tracking-tight text-foreground">
          Welcome to <span className="text-primary">IMPOSBRO</span> Search
        </h1>
        <p className="mt-4 max-w-3xl text-lg text-muted-foreground">
          Your central hub for managing a distributed, sharded search infrastructure.
          Built on Typesense with enterprise-grade federation, high availability, and
          comprehensive monitoring.
        </p>
      </div>

      <div className="grid gap-4 sm:grid-cols-3">
        <Card>
          <CardHeader className="pb-2">
            <CardDescription>System Status</CardDescription>
            <CardTitle className="flex items-center gap-2 text-2xl">
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
            <CardDescription>Clusters</CardDescription>
            <CardTitle className="text-2xl">{loading ? '—' : clusters}</CardTitle>
          </CardHeader>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardDescription>Collections</CardDescription>
            <CardTitle className="text-2xl">{loading ? '—' : collections}</CardTitle>
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
          <CardTitle>Architecture Overview</CardTitle>
          <CardDescription>How IMPOSBRO Search is built</CardDescription>
        </CardHeader>
        <CardContent>
          <div className="grid gap-6 text-sm md:grid-cols-3">
            <div>
              <h4 className="font-medium text-primary">Ingestion Pipeline</h4>
              <p className="mt-2 text-muted-foreground">
                Documents are routed through Kafka for reliable, asynchronous indexing
                with guaranteed delivery.
              </p>
            </div>
            <div>
              <h4 className="font-medium text-emerald-500">Federated Search</h4>
              <p className="mt-2 text-muted-foreground">
                Scatter-gather queries across all relevant clusters with automatic
                result merging and ranking.
              </p>
            </div>
            <div>
              <h4 className="font-medium text-violet-500">High Availability</h4>
              <p className="mt-2 text-muted-foreground">
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
