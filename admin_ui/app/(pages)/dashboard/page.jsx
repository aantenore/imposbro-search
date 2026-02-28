'use client';

import { useEffect, useState } from 'react';
import { LayoutDashboard, Server, Database, GitBranch, ExternalLink } from 'lucide-react';
import Link from 'next/link';
import { api } from '../../lib/api';

/**
 * Dashboard Feature Card
 * 
 * A clickable card that links to a feature section
 */
function FeatureCard({ href, icon: Icon, iconColor, title, description, external = false }) {
  const CardWrapper = external ? 'a' : Link;
  const props = external
    ? { href, target: '_blank', rel: 'noopener noreferrer' }
    : { href };

  return (
    <CardWrapper
      {...props}
      className="group relative bg-gray-800/50 border border-gray-700 p-6 rounded-xl hover:bg-gray-700/50 hover:border-gray-600 transition-all duration-200 hover:shadow-lg hover:shadow-black/20"
    >
      <div className="flex items-start justify-between">
        <Icon className={`w-10 h-10 mb-4 ${iconColor}`} />
        {external && (
          <ExternalLink className="w-4 h-4 text-gray-500 group-hover:text-gray-400" />
        )}
      </div>
      <h2 className="text-xl font-semibold mb-2 text-white group-hover:text-blue-400 transition-colors">
        {title}
      </h2>
      <p className="text-gray-400 text-sm leading-relaxed">
        {description}
      </p>
    </CardWrapper>
  );
}

/**
 * Dashboard Page
 * 
 * The main landing page for the IMPOSBRO Search Admin Panel.
 * Provides navigation to all major features.
 */
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
    <div>
      {/* Hero Section */}
      <div className="mb-10">
        <h1 className="text-4xl font-bold mb-4 text-white tracking-tight">
          Welcome to <span className="text-blue-400">IMPOSBRO</span> Search
        </h1>
        <p className="text-gray-400 text-lg max-w-3xl leading-relaxed">
          Your central hub for managing a distributed, sharded search infrastructure.
          Built on Typesense with enterprise-grade federation, high availability, and
          comprehensive monitoring.
        </p>
      </div>

      {/* Quick Stats (live from API) */}
      <div className="mb-10 grid grid-cols-1 sm:grid-cols-3 gap-4">
        <div className="bg-gradient-to-br from-blue-600/20 to-blue-800/20 border border-blue-700/50 rounded-xl p-5">
          <p className="text-blue-300 text-sm font-medium mb-1">System Status</p>
          <p className="text-2xl font-bold text-white flex items-center gap-2">
            {loading ? (
              <span className="text-gray-500">Loading…</span>
            ) : (
              <>
                <span className={`w-2.5 h-2.5 rounded-full ${status === 'healthy' ? 'bg-green-400' : 'bg-amber-400'} animate-pulse`}></span>
                {status === 'healthy' ? 'Operational' : status === 'degraded' ? 'Degraded' : 'Unknown'}
              </>
            )}
          </p>
          {health && (
            <p className="text-gray-500 text-xs mt-1">
              Redis: {health.redis ?? '—'} · Kafka: {health.kafka ?? '—'}
            </p>
          )}
        </div>
        <div className="bg-gradient-to-br from-purple-600/20 to-purple-800/20 border border-purple-700/50 rounded-xl p-5">
          <p className="text-purple-300 text-sm font-medium mb-1">Clusters</p>
          <p className="text-2xl font-bold text-white">{loading ? '—' : clusters}</p>
        </div>
        <div className="bg-gradient-to-br from-green-600/20 to-green-800/20 border border-green-700/50 rounded-xl p-5">
          <p className="text-green-300 text-sm font-medium mb-1">Collections</p>
          <p className="text-2xl font-bold text-white">{loading ? '—' : collections}</p>
        </div>
      </div>

      {/* Feature Cards Grid */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">
        <FeatureCard
          href="/clusters"
          icon={Server}
          iconColor="text-blue-400"
          title="Manage Clusters"
          description="Register and configure your federated Typesense clusters for distributed storage."
        />
        <FeatureCard
          href="/collections"
          icon={Database}
          iconColor="text-green-400"
          title="Manage Collections"
          description="Create and manage search indexes across all clusters with custom schemas."
        />
        <FeatureCard
          href="/routing"
          icon={GitBranch}
          iconColor="text-purple-400"
          title="Configure Routing"
          description="Define document-level sharding rules to distribute data across clusters."
        />
        <FeatureCard
          href="http://localhost:3000"
          icon={LayoutDashboard}
          iconColor="text-yellow-400"
          title="View Monitoring"
          description="Open Grafana to monitor system health, performance, and metrics."
          external
        />
      </div>

      {/* Architecture Overview */}
      <div className="mt-12 bg-gray-800/30 border border-gray-700 rounded-xl p-8">
        <h3 className="text-xl font-semibold text-white mb-4">Architecture Overview</h3>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-6 text-sm">
          <div>
            <h4 className="text-blue-400 font-medium mb-2">Ingestion Pipeline</h4>
            <p className="text-gray-400">
              Documents are routed through Kafka for reliable, asynchronous indexing
              with guaranteed delivery.
            </p>
          </div>
          <div>
            <h4 className="text-green-400 font-medium mb-2">Federated Search</h4>
            <p className="text-gray-400">
              Scatter-gather queries across all relevant clusters with automatic
              result merging and ranking.
            </p>
          </div>
          <div>
            <h4 className="text-purple-400 font-medium mb-2">High Availability</h4>
            <p className="text-gray-400">
              Configuration state is stored in a 3-node Typesense HA cluster with
              Raft consensus.
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}
