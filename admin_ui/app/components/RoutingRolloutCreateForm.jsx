'use client';

import { useState } from 'react';
import { GitBranch } from 'lucide-react';
import Button from './ui/Button';
import Input, { Select, Textarea } from './ui/Input';

function policyDraft(routingMap, collection) {
  const policy = routingMap.collections?.[collection] || {};
  return {
    collection,
    defaultCluster: policy.default_cluster || 'default',
    rulesJson: JSON.stringify(policy.rules || [], null, 2),
    rollbackWindowSeconds: '900',
  };
}

export default function RoutingRolloutCreateForm({
  routingMap,
  initialCollection = '',
  isSubmitting,
  onSubmit,
}) {
  const collections = Object.keys(routingMap.collections || {}).sort();
  const validInitialCollection = collections.includes(initialCollection)
    ? initialCollection
    : '';
  const [draft, setDraft] = useState(() => policyDraft(routingMap, validInitialCollection));
  const [rulesError, setRulesError] = useState('');
  const [formError, setFormError] = useState('');
  const clusters = Array.from(new Set(['default', ...(routingMap.clusters || [])]));

  const handleCollectionChange = (event) => {
    const collection = event.target.value;
    setDraft(policyDraft(routingMap, collection));
    setRulesError('');
    setFormError('');
  };

  const handleSubmit = (event) => {
    event.preventDefault();
    setRulesError('');
    setFormError('');

    if (!draft.collection) {
      setFormError('Select a collection before creating a rollout.');
      return;
    }

    let rules;
    try {
      rules = JSON.parse(draft.rulesJson);
      if (!Array.isArray(rules)) throw new Error('Rules must be a JSON array.');
    } catch (error) {
      setRulesError(error.message || 'Rules must contain valid JSON.');
      return;
    }

    const rollbackWindowSeconds = Number(draft.rollbackWindowSeconds);
    if (
      !Number.isInteger(rollbackWindowSeconds) ||
      rollbackWindowSeconds < 60 ||
      rollbackWindowSeconds > 604800
    ) {
      setFormError('Rollback window must be an integer between 60 and 604800 seconds.');
      return;
    }

    onSubmit({
      candidate_policy: {
        collection: draft.collection,
        rules,
        default_cluster: draft.defaultCluster,
      },
      rollback_window_seconds: rollbackWindowSeconds,
    });
  };

  return (
    <form className="space-y-5" onSubmit={handleSubmit} noValidate>
      {formError ? (
        <p className="rounded-md border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive" role="alert">
          {formError}
        </p>
      ) : null}

      <div className="grid gap-4 md:grid-cols-2">
        <Select
          label="Collection"
          name="rollout_collection"
          value={draft.collection}
          onChange={handleCollectionChange}
          disabled={isSubmitting}
          required
        >
          <option value="">Select a collection</option>
          {collections.map((collection) => (
            <option key={collection} value={collection}>{collection}</option>
          ))}
        </Select>
        <Select
          label="Candidate default cluster"
          name="candidate_default_cluster"
          value={draft.defaultCluster}
          onChange={(event) => setDraft((current) => ({
            ...current,
            defaultCluster: event.target.value,
          }))}
          disabled={isSubmitting || !draft.collection}
        >
          {clusters.map((cluster) => (
            <option key={cluster} value={cluster}>{cluster}</option>
          ))}
        </Select>
      </div>

      <Textarea
        label="Candidate rules (JSON array)"
        name="candidate_rules"
        className="min-h-52 font-mono text-xs"
        value={draft.rulesJson}
        onChange={(event) => setDraft((current) => ({
          ...current,
          rulesJson: event.target.value,
        }))}
        error={rulesError}
        disabled={isSubmitting || !draft.collection}
        spellCheck="false"
      />

      <Input
        label="Rollback window (seconds)"
        name="rollback_window_seconds"
        type="number"
        min="60"
        max="604800"
        step="1"
        value={draft.rollbackWindowSeconds}
        onChange={(event) => setDraft((current) => ({
          ...current,
          rollbackWindowSeconds: event.target.value,
        }))}
        disabled={isSubmitting}
      />

      <div className="rounded-md border border-primary/30 bg-primary/10 p-3 text-sm text-muted-foreground">
        Creation is side-effect free: the candidate remains a draft until lifecycle gates are
        satisfied and an operator confirms each material transition.
      </div>

      <Button
        type="submit"
        leftIcon={<GitBranch aria-hidden="true" />}
        loading={isSubmitting}
        disabled={collections.length === 0}
      >
        Create draft rollout
      </Button>
    </form>
  );
}
