export const ALLOWED_ROLLOUT_TRANSITIONS = Object.freeze({
  draft: ['validating', 'cancelled'],
  validating: ['dual_write', 'failed', 'cancelled'],
  dual_write: ['backfill', 'rolling_back', 'failed'],
  backfill: ['verifying', 'rolling_back', 'failed'],
  verifying: ['backfill', 'cutover', 'rolling_back', 'failed'],
  cutover: ['drain', 'rolling_back', 'failed'],
  drain: ['completed', 'rolling_back', 'failed'],
  failed: ['rolling_back'],
  rolling_back: ['rolled_back', 'failed'],
  completed: [],
  cancelled: [],
  rolled_back: [],
});

export const RISKY_ROLLOUT_TRANSITIONS = new Set([
  'dual_write',
  'cutover',
  'completed',
  'failed',
  'cancelled',
  'rolling_back',
  'rolled_back',
]);

const PHASE_VARIANTS = Object.freeze({
  draft: 'default',
  validating: 'info',
  dual_write: 'warning',
  backfill: 'info',
  verifying: 'purple',
  cutover: 'warning',
  drain: 'warning',
  completed: 'success',
  failed: 'error',
  cancelled: 'default',
  rolling_back: 'warning',
  rolled_back: 'success',
});

export function phaseLabel(phase = '') {
  return String(phase)
    .split('_')
    .filter(Boolean)
    .map((part) => part[0]?.toUpperCase() + part.slice(1))
    .join(' ');
}

export function phaseVariant(phase) {
  return PHASE_VARIANTS[phase] || 'default';
}

export function rolloutTargets(policy = {}) {
  const targets = [];
  if (policy.default_cluster) targets.push(policy.default_cluster);
  for (const rule of policy.rules || []) {
    if (Array.isArray(rule.clusters)) {
      targets.push(...rule.clusters);
    } else if (rule.cluster) {
      targets.push(rule.cluster);
    }
  }
  return Array.from(new Set(targets.filter(Boolean)));
}

export function rolloutOperationalState(rollout) {
  if (!rollout) return { label: 'Unknown', variant: 'default', degraded: false };
  if (rollout.phase === 'failed' || rollout.failure_reason) {
    return { label: 'Error', variant: 'error', degraded: true };
  }

  const gates = rollout.gates || {};
  const lagExceeded = Number(gates.kafka_lag || 0) > Number(gates.max_kafka_lag || 0);
  if (
    Number(gates.unresolved_dlq || 0) > 0 ||
    lagExceeded ||
    rollout.phase === 'rolling_back'
  ) {
    return { label: 'Degraded', variant: 'warning', degraded: true };
  }
  return { label: 'Nominal', variant: 'success', degraded: false };
}

export function sortRollouts(rollouts = []) {
  return [...rollouts].sort((left, right) => {
    const byUpdatedAt = String(right.updated_at || '').localeCompare(
      String(left.updated_at || '')
    );
    return byUpdatedAt || String(left.rollout_id).localeCompare(String(right.rollout_id));
  });
}

export function isRevisionConflict(error) {
  return error?.status === 409 || error?.status === 428;
}

export function transitionConfirmation(targetPhase, rollout) {
  const collection = rollout?.collection || 'this collection';
  const messages = {
    dual_write: `Start dual-write for ${collection}. Writes will target both active and candidate policies.`,
    cutover: `Cut over reads for ${collection} to the candidate policy. Verify parity, lag, and DLQ evidence before continuing.`,
    completed: `Complete the rollout for ${collection}. The candidate policy becomes authoritative after the rollback window.`,
    failed: `Mark this rollout as failed. Coverage remains fail-safe until an explicit rollback.`,
    cancelled: `Cancel this draft rollout. No later transition will be possible.`,
    rolling_back: `Begin rollback for ${collection}. Reads and writes may temporarily span both policies.`,
    rolled_back: `Finalize rollback for ${collection}. The active policy remains authoritative.`,
  };
  return messages[targetPhase] || `Move this rollout to ${phaseLabel(targetPhase)}.`;
}
