'use client';

import { useCallback, useEffect, useRef, useState, useTransition } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { ArrowLeft, GitPullRequest, Plus, RefreshCw, ShieldCheck } from 'lucide-react';
import { api } from '../lib/api';
import {
  ALLOWED_ROLLOUT_TRANSITIONS,
  isRevisionConflict,
  phaseLabel,
  phaseVariant,
  rolloutOperationalState,
  sortRollouts,
} from '../lib/routingRollouts';
import { Notification, useNotification } from '../hooks/useNotification';
import RoutingRolloutCreateForm from './RoutingRolloutCreateForm';
import RoutingRolloutDetail from './RoutingRolloutDetail';
import Button from './ui/Button';
import Card from './ui/Card';
import EmptyState from './ui/EmptyState';
import PageHeader from './ui/PageHeader';
import StatusBadge from './ui/StatusBadge';

async function fetchConsoleSnapshot() {
  const [rolloutResponse, routingMap, auditResponse] = await Promise.all([
    api.routingRollouts.list(),
    api.routing.getMap(),
    api.audit.list({ limit: 0, resourceType: 'routing_rollout' }),
  ]);
  return {
    rollouts: sortRollouts(rolloutResponse.rollouts || []),
    revision: rolloutResponse.revision,
    routingMap: {
      clusters: routingMap.clusters || [],
      collections: routingMap.collections || {},
    },
    auditEntries: auditResponse.entries || [],
  };
}

function LoadingState() {
  return (
    <div
      className="rounded-lg border border-border bg-card p-8 text-center text-sm text-muted-foreground"
      role="status"
      aria-live="polite"
    >
      Loading routing rollouts…
    </div>
  );
}

function RolloutList({ rollouts, selectedRolloutId }) {
  if (rollouts.length === 0) {
    return (
      <EmptyState
        icon={<GitPullRequest className="h-8 w-8" aria-hidden="true" />}
        title="No routing rollouts"
        description="Create a draft to change routing through the gated enterprise lifecycle."
      />
    );
  }

  return (
    <ul className="divide-y divide-border" aria-label="Routing rollouts">
      {rollouts.map((rollout) => {
        const health = rolloutOperationalState(rollout);
        const isSelected = rollout.rollout_id === selectedRolloutId;
        return (
          <li key={rollout.rollout_id}>
            <Link
              href={`/routing-rollouts/${encodeURIComponent(rollout.rollout_id)}`}
              aria-current={isSelected ? 'page' : undefined}
              className={`block p-4 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring ${
                isSelected ? 'bg-primary/10' : 'hover:bg-muted/40'
              }`}
            >
              <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                <div className="min-w-0">
                  <p className="font-semibold text-foreground">{rollout.collection}</p>
                  <p className="mt-1 truncate font-mono text-xs text-muted-foreground">
                    {rollout.rollout_id}
                  </p>
                  <p className="mt-2 text-xs text-muted-foreground">
                    Updated {new Date(rollout.updated_at).toLocaleString()} · version {rollout.version}
                  </p>
                </div>
                <div className="flex shrink-0 flex-wrap gap-2">
                  <StatusBadge variant={phaseVariant(rollout.phase)}>
                    {phaseLabel(rollout.phase)}
                  </StatusBadge>
                  <StatusBadge variant={health.variant}>{health.label}</StatusBadge>
                </div>
              </div>
            </Link>
          </li>
        );
      })}
    </ul>
  );
}

function ConflictRecovery({ conflict, isPending, onRefresh, onRetry, onDismiss }) {
  return (
    <div
      className="rounded-lg border border-amber-500/50 bg-amber-500/10 p-4 text-sm text-amber-200"
      role="alert"
      aria-live="assertive"
    >
      <p className="font-semibold">Control-plane state changed</p>
      <p className="mt-1">
        {conflict.message} The attempted mutation was not applied. Refresh the authoritative
        state or explicitly retry against its latest revision and rollout version.
      </p>
      {conflict.etag ? (
        <p className="mt-2 font-mono text-xs">Current ETag: {conflict.etag}</p>
      ) : null}
      <div className="mt-4 flex flex-wrap gap-2">
        <Button type="button" size="sm" variant="outline" onClick={onRefresh} loading={isPending}>
          Refresh state
        </Button>
        <Button type="button" size="sm" onClick={onRetry} loading={isPending}>
          Retry with latest
        </Button>
        <Button type="button" size="sm" variant="ghost" onClick={onDismiss} disabled={isPending}>
          Dismiss
        </Button>
      </div>
    </div>
  );
}

export default function RoutingRolloutsConsole({
  initialRolloutId = '',
  initialCollection = '',
}) {
  const router = useRouter();
  const requestGeneration = useRef(0);
  const [snapshot, setSnapshot] = useState({
    rollouts: [],
    revision: null,
    routingMap: { clusters: [], collections: {} },
    auditEntries: [],
  });
  const [loadState, setLoadState] = useState('loading');
  const [loadError, setLoadError] = useState('');
  const [mutationError, setMutationError] = useState('');
  const [conflict, setConflict] = useState(null);
  const [showCreate, setShowCreate] = useState(Boolean(initialCollection));
  const [isMutationPending, startMutation] = useTransition();
  const [isRefreshPending, startRefresh] = useTransition();
  const { notification, showSuccess, clearNotification } = useNotification();

  const loadSnapshot = useCallback(async ({ foreground = true } = {}) => {
    const generation = ++requestGeneration.current;
    if (foreground) setLoadState('loading');
    setLoadError('');
    try {
      const nextSnapshot = await fetchConsoleSnapshot();
      if (generation === requestGeneration.current) {
        setSnapshot(nextSnapshot);
        setLoadState('ready');
      }
      return nextSnapshot;
    } catch (error) {
      if (generation === requestGeneration.current) {
        setLoadError(error.message || 'Unable to load routing rollouts.');
        if (foreground) setLoadState('error');
      }
      throw error;
    }
  }, []);

  useEffect(() => {
    loadSnapshot().catch(() => {});
    return () => {
      requestGeneration.current += 1;
    };
  }, [loadSnapshot]);

  const selectedRollout = initialRolloutId
    ? snapshot.rollouts.find((rollout) => rollout.rollout_id === initialRolloutId)
    : null;

  const handleMutationError = useCallback((error, descriptor) => {
    if (isRevisionConflict(error)) {
      setConflict({
        descriptor,
        etag: error.metadata?.etag || '',
        message: error.message || 'The expected revision or rollout version is stale.',
      });
      setMutationError('');
      return;
    }
    setMutationError(error.message || 'The rollout mutation failed.');
  }, []);

  const executeMutation = useCallback(async (descriptor, baseSnapshot) => {
    if (!Number.isInteger(baseSnapshot.revision)) {
      throw new Error('No authoritative control-plane revision is available. Refresh first.');
    }

    let result;
    if (descriptor.kind === 'create') {
      result = await api.routingRollouts.create(
        descriptor.payload,
        { revision: baseSnapshot.revision }
      );
    } else {
      const latestRollout = baseSnapshot.rollouts.find(
        (rollout) => rollout.rollout_id === descriptor.rolloutId
      );
      if (!latestRollout) throw new Error('The rollout no longer exists in authoritative state.');
      if (descriptor.kind === 'transition') {
        const allowedTransitions = ALLOWED_ROLLOUT_TRANSITIONS[latestRollout.phase] || [];
        if (!allowedTransitions.includes(descriptor.payload.target_phase)) {
          throw new Error(
            `Transition to ${phaseLabel(descriptor.payload.target_phase)} is no longer valid from ${phaseLabel(latestRollout.phase)}.`
          );
        }
        result = await api.routingRollouts.transition(
          descriptor.rolloutId,
          {
            ...descriptor.payload,
            expected_version: latestRollout.version,
          },
          { revision: baseSnapshot.revision }
        );
      } else if (descriptor.kind === 'backfill') {
        result = await api.routingRollouts.runBackfillStep(
          descriptor.rolloutId,
          {
            ...descriptor.payload,
            expected_version: latestRollout.version,
          },
          { revision: baseSnapshot.revision }
        );
      } else if (descriptor.kind === 'parity') {
        result = await api.routingRollouts.verifyParity(
          descriptor.rolloutId,
          {
            ...descriptor.payload,
            expected_version: latestRollout.version,
          },
          { revision: baseSnapshot.revision }
        );
      } else {
        throw new Error(`Unsupported rollout mutation '${descriptor.kind}'.`);
      }
    }

    setSnapshot((current) => {
      const nextRollouts = current.rollouts.filter(
        (rollout) => rollout.rollout_id !== result.rollout.rollout_id
      );
      nextRollouts.push(result.rollout);
      const nextCollections = { ...current.routingMap.collections };
      if (result.rollout.phase === 'completed') {
        nextCollections[result.rollout.collection] = result.rollout.candidate_policy;
      }
      return {
        ...current,
        revision: result.revision,
        rollouts: sortRollouts(nextRollouts),
        routingMap: { ...current.routingMap, collections: nextCollections },
      };
    });
    setConflict(null);
    setMutationError('');
    showSuccess(
      descriptor.kind === 'create'
        ? `Draft rollout created for ${result.rollout.collection}.`
        : descriptor.kind === 'backfill'
          ? 'Backfill reconciler step recorded.'
          : descriptor.kind === 'parity'
            ? (result.passed ? 'Exact parity verified.' : 'Parity check recorded blocking differences.')
        : `Rollout moved to ${phaseLabel(result.rollout.phase)}.`
    );
    router.push(`/routing-rollouts/${encodeURIComponent(result.rollout.rollout_id)}`);
    api.audit.list({ limit: 0, resourceType: 'routing_rollout' })
      .then((auditResponse) => {
        setSnapshot((current) => ({
          ...current,
          auditEntries: auditResponse.entries || [],
        }));
      })
      .catch(() => {});
    return result;
  }, [router, showSuccess]);

  const submitDescriptor = useCallback((descriptor) => {
    startMutation(async () => {
      try {
        await executeMutation(descriptor, snapshot);
      } catch (error) {
        handleMutationError(error, descriptor);
      }
    });
  }, [executeMutation, handleMutationError, snapshot]);

  const refreshState = useCallback(() => {
    startRefresh(async () => {
      try {
        await loadSnapshot({ foreground: false });
      } catch (_) {}
    });
  }, [loadSnapshot]);

  const retryConflict = useCallback(() => {
    if (!conflict) return;
    startMutation(async () => {
      try {
        const latestSnapshot = await loadSnapshot({ foreground: false });
        await executeMutation(conflict.descriptor, latestSnapshot);
      } catch (error) {
        handleMutationError(error, conflict.descriptor);
      }
    });
  }, [conflict, executeMutation, handleMutationError, loadSnapshot]);

  const pageAction = (
    <div className="flex flex-wrap gap-2">
      <Button
        type="button"
        variant="outline"
        leftIcon={<RefreshCw aria-hidden="true" />}
        onClick={refreshState}
        loading={isRefreshPending}
      >
        Refresh
      </Button>
      <Button
        type="button"
        leftIcon={<Plus aria-hidden="true" />}
        onClick={() => setShowCreate((visible) => !visible)}
      >
        {showCreate ? 'Close draft form' : 'New rollout'}
      </Button>
    </div>
  );

  return (
    <div aria-busy={loadState === 'loading' || isRefreshPending}>
      <PageHeader
        title="Routing Rollouts"
        description="Revisioned, gated routing changes with explicit evidence, cutover, drain, and rollback phases."
        action={pageAction}
      />

      {notification ? (
        <div className="mb-6">
          <Notification {...notification} onClose={clearNotification} />
        </div>
      ) : null}

      {conflict ? (
        <div className="mb-6">
          <ConflictRecovery
            conflict={conflict}
            isPending={isMutationPending || isRefreshPending}
            onRefresh={refreshState}
            onRetry={retryConflict}
            onDismiss={() => setConflict(null)}
          />
        </div>
      ) : null}

      {mutationError ? (
        <div className="mb-6 rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-sm text-destructive" role="alert">
          <p className="font-semibold">Mutation not applied</p>
          <p className="mt-1">{mutationError}</p>
        </div>
      ) : null}

      {showCreate && loadState === 'ready' ? (
        <Card title="Create a side-effect-free draft" className="mb-6">
          <RoutingRolloutCreateForm
            key={initialCollection || 'new-rollout'}
            routingMap={snapshot.routingMap}
            initialCollection={initialCollection}
            isSubmitting={isMutationPending}
            onSubmit={(payload) => submitDescriptor({ kind: 'create', payload })}
          />
        </Card>
      ) : null}

      {loadState === 'loading' ? <LoadingState /> : null}

      {loadState === 'error' ? (
        <div className="rounded-lg border border-destructive/50 bg-destructive/10 p-6 text-destructive" role="alert">
          <p className="font-semibold">Unable to load routing rollouts</p>
          <p className="mt-1 text-sm">{loadError}</p>
          <Button type="button" variant="outline" className="mt-4" onClick={() => loadSnapshot().catch(() => {})}>
            Retry loading
          </Button>
        </div>
      ) : null}

      {loadState === 'ready' ? (
        <div className="grid gap-6 xl:grid-cols-[minmax(19rem,0.7fr)_minmax(0,1.3fr)]">
          <Card title={`Rollouts · revision ${snapshot.revision}`} noPadding>
            <RolloutList
              rollouts={snapshot.rollouts}
              selectedRolloutId={initialRolloutId}
            />
          </Card>

          <Card className="p-6">
            {initialRolloutId && selectedRollout ? (
              <RoutingRolloutDetail
                key={`${selectedRollout.rollout_id}:${selectedRollout.version}`}
                rollout={selectedRollout}
                revision={snapshot.revision}
                auditEntries={snapshot.auditEntries.filter(
                  (entry) => entry.resource_id === selectedRollout.rollout_id
                )}
                isSubmitting={isMutationPending}
                onTransition={(payload) => submitDescriptor({
                  kind: 'transition',
                  rolloutId: selectedRollout.rollout_id,
                  payload,
                })}
                onBackfillStep={(payload) => submitDescriptor({
                  kind: 'backfill',
                  rolloutId: selectedRollout.rollout_id,
                  payload,
                })}
                onVerifyParity={(payload) => submitDescriptor({
                  kind: 'parity',
                  rolloutId: selectedRollout.rollout_id,
                  payload,
                })}
              />
            ) : null}

            {initialRolloutId && !selectedRollout ? (
              <div role="alert" className="text-center">
                <ShieldCheck className="mx-auto h-9 w-9 text-muted-foreground" aria-hidden="true" />
                <h2 className="mt-4 text-lg font-semibold text-foreground">Rollout not found</h2>
                <p className="mt-2 text-sm text-muted-foreground">
                  The requested rollout is absent from the current authoritative revision.
                </p>
                <Link
                  href="/routing-rollouts"
                  className="mt-4 inline-flex items-center gap-2 rounded-md text-sm font-medium text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  <ArrowLeft aria-hidden="true" /> Back to all rollouts
                </Link>
              </div>
            ) : null}

            {!initialRolloutId ? (
              <EmptyState
                icon={<ShieldCheck className="h-9 w-9" aria-hidden="true" />}
                title="Select a rollout"
                description="Open a rollout to inspect policies, safety evidence, timestamps, and permitted transitions."
              />
            ) : null}
          </Card>
        </div>
      ) : null}
    </div>
  );
}
