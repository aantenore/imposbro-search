'use client';

import { useState } from 'react';
import { ArrowRight, ShieldAlert } from 'lucide-react';
import {
  ALLOWED_ROLLOUT_TRANSITIONS,
  RISKY_ROLLOUT_TRANSITIONS,
  phaseLabel,
  phaseVariant,
  rolloutOperationalState,
  rolloutTargets,
  transitionConfirmation,
} from '../lib/routingRollouts';
import Button from './ui/Button';
import ConfirmationModal from './ui/ConfirmationModal';
import { Checkbox, Select, Textarea } from './ui/Input';
import StatusBadge from './ui/StatusBadge';

const DATE_FORMATTER = new Intl.DateTimeFormat(undefined, {
  dateStyle: 'medium',
  timeStyle: 'medium',
});

const BOOLEAN_GATES = [
  ['validation_passed', 'Validation passed'],
  ['capacity_passed', 'Capacity passed'],
];

function formatTimestamp(value) {
  if (!value) return 'Not set';
  const timestamp = new Date(value);
  return Number.isNaN(timestamp.getTime()) ? String(value) : DATE_FORMATTER.format(timestamp);
}

function TargetList({ targets, emptyLabel }) {
  return (
    <div className="flex flex-wrap gap-2">
      {targets.length > 0 ? targets.map((target) => (
        <StatusBadge key={target} variant="info">{target}</StatusBadge>
      )) : <span className="text-sm text-muted-foreground">{emptyLabel}</span>}
    </div>
  );
}

function PolicySummary({ label, policy }) {
  const targets = rolloutTargets(policy);
  return (
    <div className="min-w-0 rounded-lg border border-border bg-muted/20 p-4">
      <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        {label}
      </p>
      <div className="mt-3">
        <TargetList targets={targets} emptyLabel="No target resolved" />
      </div>
      <p className="mt-3 text-xs text-muted-foreground">
        {(policy.rules || []).length} rule{(policy.rules || []).length === 1 ? '' : 's'} · default{' '}
        <span className="font-mono text-foreground">{policy.default_cluster || 'default'}</span>
      </p>
      <details className="mt-3 text-xs">
        <summary className="cursor-pointer rounded text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
          Inspect policy JSON
        </summary>
        <pre className="mt-2 max-h-64 overflow-auto rounded-md bg-background p-3 text-muted-foreground">
          {JSON.stringify(policy, null, 2)}
        </pre>
      </details>
    </div>
  );
}

function RolloutEvidence({ gates }) {
  const evidence = [
    ['Validation', gates.validation_passed],
    ['Capacity', gates.capacity_passed],
    ['Backfill', gates.backfill_complete],
    ['Parity', gates.parity_passed],
  ];

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-2">
        {evidence.map(([label, passed]) => (
          <StatusBadge key={label} variant={passed ? 'success' : 'default'}>
            {label}: {passed ? 'passed' : 'pending'}
          </StatusBadge>
        ))}
      </div>
      <dl className="grid gap-3 text-sm sm:grid-cols-3">
        <div>
          <dt className="text-muted-foreground">Unresolved DLQ</dt>
          <dd className="mt-1 font-mono text-foreground">{gates.unresolved_dlq || 0}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Kafka lag / budget</dt>
          <dd className="mt-1 font-mono text-foreground">
            {gates.kafka_lag || 0} / {gates.max_kafka_lag || 0}
          </dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Source barrier</dt>
          <dd className="mt-1 break-all font-mono text-foreground">
            {gates.source_barrier || 'Not set'}
          </dd>
        </div>
      </dl>
      {gates.parity_digest ? (
        <p className="break-all text-xs text-muted-foreground">
          Parity digest: <span className="font-mono text-foreground">{gates.parity_digest}</span>
        </p>
      ) : null}
    </div>
  );
}

function TransitionForm({ rollout, isSubmitting, onTransition }) {
  const allowedTransitions = ALLOWED_ROLLOUT_TRANSITIONS[rollout.phase] || [];
  const [targetPhase, setTargetPhase] = useState(allowedTransitions[0] || '');
  const [gates, setGates] = useState(() => ({
    validation_passed: false,
    capacity_passed: false,
    ...(rollout.gates || {}),
  }));
  const [failureReason, setFailureReason] = useState(rollout.failure_reason || '');
  const [validationError, setValidationError] = useState('');
  const [pendingPayload, setPendingPayload] = useState(null);

  if (allowedTransitions.length === 0) {
    return (
      <p className="rounded-md border border-border bg-muted/20 p-4 text-sm text-muted-foreground">
        This rollout is terminal. No further lifecycle transitions are allowed.
      </p>
    );
  }

  const prepareTransition = (event) => {
    event.preventDefault();
    setValidationError('');
    if (!targetPhase) {
      setValidationError('Select a target phase.');
      return;
    }
    if (targetPhase === 'failed' && !failureReason.trim()) {
      setValidationError('A failure reason is required when marking a rollout failed.');
      return;
    }

    const payload = {
      target_phase: targetPhase,
      expected_version: rollout.version,
      failure_reason: targetPhase === 'failed' ? failureReason.trim() : '',
    };
    if (targetPhase === 'dual_write') {
      payload.gates = {
        validation_passed: Boolean(gates.validation_passed),
        capacity_passed: Boolean(gates.capacity_passed),
      };
    }

    if (RISKY_ROLLOUT_TRANSITIONS.has(targetPhase)) {
      setPendingPayload(payload);
    } else {
      onTransition(payload);
    }
  };

  const confirmTransition = () => {
    const payload = pendingPayload;
    setPendingPayload(null);
    if (payload) onTransition(payload);
  };

  return (
    <>
      {pendingPayload ? (
        <ConfirmationModal
          title={`Confirm ${phaseLabel(pendingPayload.target_phase)}`}
          resourceName={rollout.collection}
          resourceType="routing rollout"
          message={transitionConfirmation(pendingPayload.target_phase, rollout)}
          confirmText={`Move to ${phaseLabel(pendingPayload.target_phase)}`}
          onConfirm={confirmTransition}
          onCancel={() => setPendingPayload(null)}
        />
      ) : null}

      <form className="space-y-5" onSubmit={prepareTransition} noValidate>
        {validationError ? (
          <p className="rounded-md border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive" role="alert">
            {validationError}
          </p>
        ) : null}

        <Select
          label="Target phase"
          name="target_phase"
          value={targetPhase}
          onChange={(event) => setTargetPhase(event.target.value)}
          disabled={isSubmitting}
        >
          {allowedTransitions.map((phase) => (
            <option key={phase} value={phase}>{phaseLabel(phase)}</option>
          ))}
        </Select>

        {targetPhase === 'dual_write' ? (
          <fieldset className="rounded-lg border border-border p-4">
            <legend className="px-2 text-sm font-semibold text-foreground">
              Operator preflight attestations
            </legend>
            <div className="grid gap-4 sm:grid-cols-2">
              {BOOLEAN_GATES.map(([name, label]) => (
                <Checkbox
                  key={name}
                  label={label}
                  checked={Boolean(gates[name])}
                  onChange={(event) => setGates((current) => ({
                    ...current,
                    [name]: event.target.checked,
                  }))}
                  disabled={isSubmitting}
                />
              ))}
            </div>
          </fieldset>
        ) : (
          <p className="rounded-md border border-border bg-muted/20 p-3 text-sm text-muted-foreground">
            Backfill checkpoints, parity, lag, and DLQ evidence are read-only and recorded by
            platform reconcilers.
          </p>
        )}

        {targetPhase === 'failed' ? (
          <Textarea
            label="Failure reason"
            name="failure_reason"
            className="min-h-24"
            value={failureReason}
            onChange={(event) => setFailureReason(event.target.value)}
            disabled={isSubmitting}
            required
          />
        ) : null}

        <Button
          type="submit"
          variant={RISKY_ROLLOUT_TRANSITIONS.has(targetPhase) ? 'destructive' : 'default'}
          loading={isSubmitting}
        >
          Review transition to {phaseLabel(targetPhase)}
        </Button>
      </form>
    </>
  );
}

export default function RoutingRolloutDetail({
  rollout,
  revision,
  auditEntries = [],
  isSubmitting,
  onTransition,
  onBackfillStep,
  onVerifyParity,
}) {
  const operationalState = rolloutOperationalState(rollout);
  const activeTargets = rolloutTargets(rollout.active_policy);
  const candidateTargets = rolloutTargets(rollout.candidate_policy);
  const gates = rollout.gates || {};

  return (
    <article className="space-y-6" aria-labelledby="rollout-detail-title">
      <header className="flex flex-col gap-4 border-b border-border pb-5 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <p className="text-sm text-muted-foreground">Collection</p>
          <h2 id="rollout-detail-title" className="mt-1 text-2xl font-semibold text-foreground">
            {rollout.collection}
          </h2>
          <p className="mt-2 break-all font-mono text-xs text-muted-foreground">
            {rollout.rollout_id}
          </p>
        </div>
        <div className="flex flex-wrap gap-2" aria-label="Rollout lifecycle status">
          <StatusBadge variant={phaseVariant(rollout.phase)}>{phaseLabel(rollout.phase)}</StatusBadge>
          <StatusBadge variant={operationalState.variant}>{operationalState.label}</StatusBadge>
          <StatusBadge variant="default">Revision {revision}</StatusBadge>
          <StatusBadge variant="default">Version {rollout.version}</StatusBadge>
        </div>
      </header>

      {operationalState.degraded ? (
        <div className="flex gap-3 rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-sm text-destructive" role="alert">
          <ShieldAlert className="mt-0.5 h-5 w-5 shrink-0" aria-hidden="true" />
          <div>
            <p className="font-semibold">Rollout requires operator attention</p>
            <p className="mt-1">
              {rollout.failure_reason || `DLQ ${gates.unresolved_dlq || 0}; Kafka lag ${gates.kafka_lag || 0} / ${gates.max_kafka_lag || 0}.`}
            </p>
          </div>
        </div>
      ) : null}

      <section aria-labelledby="route-comparison-title">
        <h3 id="route-comparison-title" className="text-lg font-semibold text-foreground">
          Source and candidate targets
        </h3>
        <div className="mt-3 grid items-stretch gap-3 lg:grid-cols-[1fr_auto_1fr]">
          <PolicySummary label="Active source policy" policy={rollout.active_policy} />
          <div className="hidden items-center text-muted-foreground lg:flex">
            <ArrowRight aria-hidden="true" />
          </div>
          <PolicySummary label="Candidate target policy" policy={rollout.candidate_policy} />
        </div>
        <p className="sr-only">
          Active targets: {activeTargets.join(', ') || 'none'}. Candidate targets:{' '}
          {candidateTargets.join(', ') || 'none'}.
        </p>
      </section>

      <section aria-labelledby="timeline-title">
        <h3 id="timeline-title" className="text-lg font-semibold text-foreground">Timeline</h3>
        <dl className="mt-3 grid gap-4 rounded-lg border border-border bg-muted/20 p-4 text-sm sm:grid-cols-2 xl:grid-cols-4">
          <div>
            <dt className="text-muted-foreground">Created</dt>
            <dd className="mt-1 text-foreground">{formatTimestamp(rollout.created_at)}</dd>
          </div>
          <div>
            <dt className="text-muted-foreground">Updated</dt>
            <dd className="mt-1 text-foreground">{formatTimestamp(rollout.updated_at)}</dd>
          </div>
          <div>
            <dt className="text-muted-foreground">Cutover</dt>
            <dd className="mt-1 text-foreground">{formatTimestamp(rollout.cutover_at)}</dd>
          </div>
          <div>
            <dt className="text-muted-foreground">Rollback deadline</dt>
            <dd className="mt-1 text-foreground">{formatTimestamp(rollout.rollback_deadline)}</dd>
          </div>
        </dl>
        <p className="mt-2 text-xs text-muted-foreground">
          Created by <span className="font-mono text-foreground">{rollout.created_by}</span> · rollback
          window {rollout.rollback_window_seconds}s
        </p>
      </section>

      <section aria-labelledby="evidence-title">
        <h3 id="evidence-title" className="text-lg font-semibold text-foreground">Current evidence</h3>
        <div className="mt-3 rounded-lg border border-border bg-muted/20 p-4">
          <RolloutEvidence gates={gates} />
        </div>
      </section>

      {rollout.phase === 'backfill' || rollout.phase === 'verifying' ? (
        <section aria-labelledby="reconciler-title">
          <h3 id="reconciler-title" className="text-lg font-semibold text-foreground">
            Reconciler actions
          </h3>
          <p className="mt-1 text-sm text-muted-foreground">
            These actions measure and persist evidence server-side. Operators cannot edit the
            resulting checkpoint or parity digest.
          </p>
          <div className="mt-3 rounded-lg border border-border bg-muted/20 p-4">
            {rollout.phase === 'backfill' ? (
              <Button
                type="button"
                onClick={() => onBackfillStep({
                  expected_version: rollout.version,
                  max_documents: 100,
                })}
                loading={isSubmitting}
              >
                Run next 100-document step
              </Button>
            ) : null}
            {rollout.phase === 'verifying' ? (
              <Button
                type="button"
                onClick={() => onVerifyParity({
                  expected_version: rollout.version,
                  sample_limit: 100,
                })}
                loading={isSubmitting}
              >
                Verify exact parity
              </Button>
            ) : null}
            <details className="mt-4 text-xs">
              <summary className="cursor-pointer rounded text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
                Inspect read-only reconciler checkpoint
              </summary>
              <pre className="mt-2 max-h-64 overflow-auto rounded-md bg-background p-3 text-muted-foreground">
                {JSON.stringify(rollout.backfill_checkpoint || {}, null, 2)}
              </pre>
            </details>
          </div>
        </section>
      ) : null}

      <section aria-labelledby="audit-title">
        <h3 id="audit-title" className="text-lg font-semibold text-foreground">Audit trail</h3>
        <div className="mt-3 rounded-lg border border-border bg-muted/20 p-4">
          {auditEntries.length > 0 ? (
            <ol className="space-y-3">
              {auditEntries.map((entry) => (
                <li key={entry.id} className="border-b border-border pb-3 last:border-0 last:pb-0">
                  <div className="flex flex-wrap items-start justify-between gap-2 text-sm">
                    <span className="font-medium text-foreground">
                      {phaseLabel(entry.action)}
                    </span>
                    <StatusBadge variant={entry.status === 'success' ? 'success' : 'warning'}>
                      {entry.status || 'unknown'}
                    </StatusBadge>
                  </div>
                  <p className="mt-1 text-xs text-muted-foreground">
                    {formatTimestamp(entry.timestamp)} · actor{' '}
                    <span className="font-mono text-foreground">{entry.actor || 'unknown'}</span>
                  </p>
                  {Object.keys(entry.details || {}).length > 0 ? (
                    <pre className="mt-2 max-h-32 overflow-auto rounded bg-background p-2 text-xs text-muted-foreground">
                      {JSON.stringify(entry.details, null, 2)}
                    </pre>
                  ) : null}
                </li>
              ))}
            </ol>
          ) : (
            <p className="text-sm text-muted-foreground">
              No audit events for this rollout were returned in the current audit window.
            </p>
          )}
        </div>
      </section>

      <section aria-labelledby="transition-title">
        <h3 id="transition-title" className="text-lg font-semibold text-foreground">
          Lifecycle transition
        </h3>
        <p className="mt-1 text-sm text-muted-foreground">
          Transitions are checked against rollout version {rollout.version} and control-plane
          revision {revision}. Gate failures leave the current phase unchanged.
        </p>
        <div className="mt-4 rounded-lg border border-border p-4">
          <TransitionForm
            rollout={rollout}
            isSubmitting={isSubmitting}
            onTransition={onTransition}
          />
        </div>
      </section>
    </article>
  );
}
