'use client';

import { useState, useEffect, useCallback } from 'react';
import { Plus, X, GitBranch, Trash2, Play } from 'lucide-react';
import { api } from '../../lib/api';
import { useNotification, Notification } from '../../hooks/useNotification';
import { ConfirmationModal } from '../../components/ui';
import Card from '../../components/ui/Card';
import Button, { IconButton } from '../../components/ui/Button';
import Input, { Checkbox, Select, Textarea } from '../../components/ui/Input';
import PageHeader from '../../components/ui/PageHeader';
import EmptyState from '../../components/ui/EmptyState';
import RoutingDiagram from '../../components/RoutingDiagram';

const OPERATORS = [
    { value: 'equals', label: 'Equals' },
    { value: 'in', label: 'In list' },
    { value: 'glob', label: 'Glob' },
    { value: 'range', label: 'Range' },
];

function normalizeRuleTargets(rule, fallbackCluster = '') {
    const rawTargets = Array.isArray(rule.clusters)
        ? rule.clusters
        : [rule.cluster || fallbackCluster];
    return Array.from(new Set(rawTargets.filter(Boolean)));
}

function clusterOptions(clusters) {
    return ['default', ...clusters.filter((cluster) => cluster !== 'default')];
}

function splitValues(value) {
    return String(value || '')
        .split(',')
        .map((item) => item.trim())
        .filter(Boolean);
}

function valuesText(values) {
    return Array.isArray(values) ? values.join(', ') : '';
}

function optionalNumber(value) {
    if (value === null || value === undefined || String(value).trim() === '') {
        return undefined;
    }
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : undefined;
}

function optionalInteger(value) {
    const parsed = optionalNumber(value);
    if (parsed === undefined || parsed < 0) {
        return undefined;
    }
    return Math.trunc(parsed);
}

function normalizeEditableRule(rule, fallbackCluster = '') {
    const operator = rule.operator || 'equals';
    return {
        field: rule.field || '',
        operator,
        value: rule.value ?? '',
        valuesText: valuesText(rule.values),
        pattern: rule.pattern ?? (operator === 'glob' ? rule.value ?? '' : ''),
        min: rule.min === undefined || rule.min === null ? '' : String(rule.min),
        max: rule.max === undefined || rule.max === null ? '' : String(rule.max),
        priority: rule.priority === undefined || rule.priority === null ? '' : String(rule.priority),
        clusters: normalizeRuleTargets(rule, fallbackCluster),
    };
}

function isRuleComplete(rule) {
    if (!rule.field || normalizeRuleTargets(rule).length === 0) {
        return false;
    }
    const operator = rule.operator || 'equals';
    if (operator === 'equals') {
        return String(rule.value || '').trim() !== '';
    }
    if (operator === 'in') {
        return splitValues(rule.valuesText).length > 0;
    }
    if (operator === 'glob') {
        return String(rule.pattern || rule.value || '').trim() !== '';
    }
    if (operator === 'range') {
        const min = optionalNumber(rule.min);
        const max = optionalNumber(rule.max);
        if (min === undefined && max === undefined) {
            return false;
        }
        return min === undefined || max === undefined || min <= max;
    }
    return false;
}

function toApiRule(rule) {
    const targetClusters = normalizeRuleTargets(rule);
    const operator = rule.operator || 'equals';
    const base = { field: rule.field };

    if (operator !== 'equals') {
        base.operator = operator;
    }
    if (operator === 'equals') {
        base.value = String(rule.value).trim();
    } else if (operator === 'in') {
        base.values = splitValues(rule.valuesText);
    } else if (operator === 'glob') {
        base.pattern = String(rule.pattern || rule.value).trim();
    } else if (operator === 'range') {
        const min = optionalNumber(rule.min);
        const max = optionalNumber(rule.max);
        if (min !== undefined) base.min = min;
        if (max !== undefined) base.max = max;
    }

    const priority = optionalInteger(rule.priority);
    if (priority !== undefined) {
        base.priority = priority;
    }

    if (targetClusters.length === 1) {
        return { ...base, cluster: targetClusters[0] };
    }
    return { ...base, clusters: targetClusters };
}

function formatRuleCondition(rule) {
    const operator = rule.operator || 'equals';
    if (operator === 'in') {
        return `${rule.field} in [${(rule.values || []).join(', ')}]`;
    }
    if (operator === 'glob') {
        return `${rule.field} matches ${rule.pattern || rule.value}`;
    }
    if (operator === 'range') {
        const lower = rule.min === undefined ? '*' : rule.min;
        const upper = rule.max === undefined ? '*' : rule.max;
        return `${rule.field} in ${lower}..${upper}`;
    }
    return `${rule.field} = ${rule.value}`;
}

/**
 * Routing Page
 *
 * Manages document-level routing rules for collections.
 * Rules are evaluated by priority/order; first match determines the target cluster.
 */
export default function RoutingPage() {
    const [collections, setCollections] = useState([]);
    const [clusters, setClusters] = useState([]);
    const [selectedCollection, setSelectedCollection] = useState('');
    const [availableFields, setAvailableFields] = useState([]);
    const [rules, setRules] = useState([]);
    const [defaultCluster, setDefaultCluster] = useState('');
    const [currentRules, setCurrentRules] = useState({});
    const [ruleToDelete, setRuleToDelete] = useState(null);
    const [isSubmitting, setIsSubmitting] = useState(false);
    const [isLoadingSchema, setIsLoadingSchema] = useState(false);
    const [previewDocument, setPreviewDocument] = useState('{\n  "tenant_id": "acme"\n}');
    const [previewResult, setPreviewResult] = useState(null);
    const [isPreviewing, setIsPreviewing] = useState(false);
    const { notification, showSuccess, showError } = useNotification();

    const fetchData = useCallback(async () => {
        try {
            const data = await api.routing.getMap();
            const colls = Object.keys(data.collections || {});
            const clus = data.clusters || [];
            setCollections(colls);
            setClusters(clus);
            setCurrentRules(data.collections || {});
            setDefaultCluster((prev) => prev || 'default');
        } catch (err) {
            showError(err.message);
        }
    }, [showError]);

    useEffect(() => {
        fetchData();
    }, [fetchData]);

    useEffect(() => {
        setPreviewResult(null);
        if (!selectedCollection) {
            setAvailableFields([]);
            setRules([]);
            return;
        }
        let cancelled = false;
        setIsLoadingSchema(true);
        Promise.all([
            api.collections.get(selectedCollection),
            api.routing.getMap(),
        ])
            .then(([schemaData, mapData]) => {
                if (cancelled) return;
                setAvailableFields(schemaData.fields || []);
                const existing = (mapData.collections || {})[selectedCollection];
                const clus = mapData.clusters || [];
                if (existing?.rules?.length) {
                    setRules(existing.rules.map((rule) => (
                        normalizeEditableRule(rule, clus[0] || 'default')
                    )));
                    setDefaultCluster(existing.default_cluster || 'default');
                } else {
                    setRules([]);
                    setDefaultCluster(existing?.default_cluster || 'default');
                }
            })
            .catch(() => {
                if (!cancelled) {
                    showError(`Could not load schema for ${selectedCollection}.`);
                    setAvailableFields([]);
                    setRules([]);
                }
            })
            .finally(() => {
                if (!cancelled) setIsLoadingSchema(false);
            });
        return () => { cancelled = true; };
    }, [selectedCollection, showError]);

    const handleAddRule = () => {
        if (availableFields.length === 0) return;
        setRules(prev => [
            ...prev,
            normalizeEditableRule({
                field: availableFields[0].name,
                value: '',
                cluster: clusters[0] || 'default',
            }, clusters[0] || 'default'),
        ]);
    };

    const handleRemoveRule = (index) => {
        setRules(prev => prev.filter((_, i) => i !== index));
        setPreviewResult(null);
    };

    const handleRuleChange = (index, event) => {
        const { name, value } = event.target;
        setRules(prev => {
            const newRules = [...prev];
            newRules[index] = { ...newRules[index], [name]: value };
            return newRules;
        });
        setPreviewResult(null);
    };

    const handleRuleClusterToggle = (index, clusterName, checked) => {
        setRules(prev => prev.map((rule, ruleIndex) => {
            if (ruleIndex !== index) return rule;
            const current = normalizeRuleTargets(rule);
            const nextClusters = checked
                ? Array.from(new Set([...current, clusterName]))
                : current.filter((name) => name !== clusterName);
            return { ...rule, clusters: nextClusters };
        }));
        setPreviewResult(null);
    };

    const draftApiRules = () => rules.filter(isRuleComplete).map(toApiRule);

    const handlePreview = async () => {
        if (!selectedCollection || !defaultCluster) {
            showError('Please select a collection and a default cluster.');
            return;
        }

        let document;
        try {
            document = JSON.parse(previewDocument);
        } catch (_) {
            showError('Preview document must be valid JSON.');
            return;
        }

        setIsPreviewing(true);
        try {
            const result = await api.routing.preview({
                collection: selectedCollection,
                document,
                rules: draftApiRules(),
                default_cluster: defaultCluster,
            });
            setPreviewResult(result);
        } catch (err) {
            showError(err.message);
        } finally {
            setIsPreviewing(false);
        }
    };

    const handleSubmit = async (e) => {
        e.preventDefault();

        if (!selectedCollection || !defaultCluster) {
            showError('Please select a collection and a default cluster.');
            return;
        }

        setIsSubmitting(true);
        try {
            await api.routing.setRules({
                collection: selectedCollection,
                rules: draftApiRules(),
                default_cluster: defaultCluster,
            });
            showSuccess(`Routing rules for '${selectedCollection}' saved successfully!`);
            fetchData();
        } catch (err) {
            showError(err.message);
        } finally {
            setIsSubmitting(false);
        }
    };

    const handleDeleteConfirm = async () => {
        if (!ruleToDelete) return;

        try {
            await api.routing.deleteRules(ruleToDelete);
            showSuccess(`Routing rules for '${ruleToDelete}' have been deleted.`);
            fetchData();
        } catch (err) {
            showError(err.message);
        } finally {
            setRuleToDelete(null);
        }
    };

    const renderConditionInput = (rule, index) => {
        if ((rule.operator || 'equals') === 'in') {
            return (
                <Input
                    name="valuesText"
                    placeholder="IT, FR, DE"
                    value={rule.valuesText}
                    onChange={(e) => handleRuleChange(index, e)}
                />
            );
        }
        if (rule.operator === 'glob') {
            return (
                <Input
                    name="pattern"
                    placeholder="vip-*"
                    value={rule.pattern}
                    onChange={(e) => handleRuleChange(index, e)}
                />
            );
        }
        if (rule.operator === 'range') {
            return (
                <div className="grid grid-cols-2 gap-2">
                    <Input
                        type="number"
                        name="min"
                        placeholder="Min"
                        value={rule.min}
                        onChange={(e) => handleRuleChange(index, e)}
                    />
                    <Input
                        type="number"
                        name="max"
                        placeholder="Max"
                        value={rule.max}
                        onChange={(e) => handleRuleChange(index, e)}
                    />
                </div>
            );
        }
        return (
            <Input
                name="value"
                placeholder="Value"
                value={rule.value}
                onChange={(e) => handleRuleChange(index, e)}
            />
        );
    };

    const configuredRules = Object.entries(currentRules)
        .filter(([_, rule]) => rule.rules?.length > 0);

    return (
        <div>
            {ruleToDelete && (
                <ConfirmationModal
                    resourceName={ruleToDelete}
                    resourceType="routing rules"
                    message={`Are you sure you want to delete all routing rules for "${ruleToDelete}"? The collection will revert to using the default cluster.`}
                    confirmText="Delete Rules"
                    onConfirm={handleDeleteConfirm}
                    onCancel={() => setRuleToDelete(null)}
                />
            )}

            <PageHeader
                title="Document Routing Rules"
                description="Define ordered document rules with equals, list, glob, range, priority, and fan-out targets."
            />

            {notification && (
                <div className="mb-6">
                    <Notification {...notification} />
                </div>
            )}

            <div className="mb-8">
                <RoutingDiagram />
            </div>

            <div className="grid grid-cols-1 lg:grid-cols-2 gap-8">
                <Card title="Set / Update Rules">
                    <form onSubmit={handleSubmit} className="space-y-6">
                        <div>
                            <label className="mb-2 block text-sm font-medium text-foreground">
                                1. Select collection
                            </label>
                            <Select
                                value={selectedCollection}
                                onChange={(e) => setSelectedCollection(e.target.value)}
                                disabled={isLoadingSchema}
                            >
                                <option value="">- Select a collection -</option>
                                {collections.map(c => (
                                    <option key={c} value={c}>{c}</option>
                                ))}
                            </Select>
                            {collections.length === 0 && (
                                <p className="mt-1 text-xs text-muted-foreground">Create a collection first (Collections).</p>
                            )}
                        </div>

                        {selectedCollection && (
                            <>
                                <div>
                                    <label className="block text-sm font-medium text-foreground mb-1">
                                        2. Rules
                                    </label>
                                    <p className="mb-2 text-xs text-muted-foreground">
                                        Lower priority values evaluate first; ties keep the visible order.
                                    </p>

                                    {rules.map((rule, index) => (
                                        <div
                                            key={index}
                                            className="grid grid-cols-12 gap-3 items-start mt-2 p-3 bg-muted/30 rounded-lg border border-border"
                                        >
                                            <div className="col-span-12 md:col-span-3">
                                                <Select
                                                    name="field"
                                                    value={rule.field}
                                                    onChange={(e) => handleRuleChange(index, e)}
                                                    disabled={availableFields.length === 0}
                                                >
                                                    <option value="">Field</option>
                                                    {availableFields.map(f => (
                                                        <option key={f.name} value={f.name}>{f.name}</option>
                                                    ))}
                                                </Select>
                                            </div>
                                            <div className="col-span-12 md:col-span-2">
                                                <Select
                                                    name="operator"
                                                    value={rule.operator}
                                                    onChange={(e) => handleRuleChange(index, e)}
                                                >
                                                    {OPERATORS.map((operator) => (
                                                        <option key={operator.value} value={operator.value}>
                                                            {operator.label}
                                                        </option>
                                                    ))}
                                                </Select>
                                            </div>
                                            <div className="col-span-12 md:col-span-3">
                                                {renderConditionInput(rule, index)}
                                            </div>
                                            <div className="col-span-6 md:col-span-1">
                                                <Input
                                                    type="number"
                                                    min="0"
                                                    step="1"
                                                    name="priority"
                                                    placeholder="Prio"
                                                    value={rule.priority}
                                                    onChange={(e) => handleRuleChange(index, e)}
                                                />
                                            </div>
                                            <div className="col-span-12 md:col-span-2">
                                                <p className="mb-2 text-xs font-medium uppercase text-muted-foreground">
                                                    Targets
                                                </p>
                                                <div className="flex flex-wrap gap-2">
                                                    {clusters.map(c => (
                                                        <Checkbox
                                                            key={c}
                                                            label={c}
                                                            checked={normalizeRuleTargets(rule).includes(c)}
                                                            onChange={(e) => handleRuleClusterToggle(index, c, e.target.checked)}
                                                        />
                                                    ))}
                                                </div>
                                            </div>
                                            <IconButton
                                                type="button"
                                                variant="danger"
                                                className="col-span-6 justify-self-end md:col-span-1"
                                                onClick={() => handleRemoveRule(index)}
                                                title="Remove rule"
                                            >
                                                <X size={16} />
                                            </IconButton>
                                        </div>
                                    ))}

                                    <Button
                                        type="button"
                                        variant="ghost"
                                        size="sm"
                                        leftIcon={<Plus size={16} />}
                                        onClick={handleAddRule}
                                        className="mt-3"
                                        disabled={availableFields.length === 0}
                                    >
                                        Add Rule
                                    </Button>
                                </div>

                                <div>
                                    <label className="mb-2 block text-sm font-medium text-foreground">
                                        3. Default cluster
                                    </label>
                                    <Select
                                        value={defaultCluster}
                                        onChange={(e) => {
                                            setDefaultCluster(e.target.value);
                                            setPreviewResult(null);
                                        }}
                                    >
                                        {clusterOptions(clusters).map(c => (
                                            <option key={c} value={c}>{c}</option>
                                        ))}
                                    </Select>
                                </div>

                                <div>
                                    <label className="mb-2 block text-sm font-medium text-foreground">
                                        4. Preview
                                    </label>
                                    <Textarea
                                        className="font-mono"
                                        rows={5}
                                        value={previewDocument}
                                        onChange={(e) => {
                                            setPreviewDocument(e.target.value);
                                            setPreviewResult(null);
                                        }}
                                    />
                                    <Button
                                        type="button"
                                        variant="outline"
                                        size="sm"
                                        leftIcon={<Play size={16} />}
                                        loading={isPreviewing}
                                        onClick={handlePreview}
                                        className="mt-3"
                                    >
                                        Preview Route
                                    </Button>
                                    {previewResult && (
                                        <div className="mt-3 rounded-md border border-border bg-muted/30 p-3 text-sm">
                                            <div className="flex flex-wrap items-center gap-2">
                                                <span className="font-medium text-foreground">
                                                    {previewResult.used_default ? 'Default route' : 'Matched rule'}
                                                </span>
                                                {previewResult.routed_to?.map((clusterName) => (
                                                    <span
                                                        key={clusterName}
                                                        className="rounded border border-primary/30 bg-primary/10 px-2 py-1 text-xs font-semibold text-primary"
                                                    >
                                                        {clusterName}
                                                    </span>
                                                ))}
                                            </div>
                                            {previewResult.matched_rule && (
                                                <pre className="mt-3 max-h-40 overflow-auto rounded-md bg-background p-3 text-xs text-muted-foreground">
                                                    {JSON.stringify(previewResult.matched_rule, null, 2)}
                                                </pre>
                                            )}
                                        </div>
                                    )}
                                </div>

                                <Button
                                    type="submit"
                                    variant="purple"
                                    fullWidth
                                    leftIcon={<GitBranch size={18} />}
                                    loading={isSubmitting}
                                >
                                    Save Routing Rules
                                </Button>
                            </>
                        )}
                    </form>
                </Card>

                <Card title="Current Routing Configuration" noPadding>
                    <div className="divide-y divide-border">
                        {configuredRules.length > 0 ? (
                            configuredRules.map(([collection, ruleConfig]) => (
                                <div key={collection} className="relative p-4 transition-colors hover:bg-muted/50">
                                    <p className="mb-3 font-bold text-foreground">{collection}</p>
                                    <ul className="space-y-2 text-sm">
                                        {ruleConfig.rules.map((rule, index) => (
                                            <li key={index} className="flex flex-wrap items-center gap-2 pr-10">
                                                <span className="rounded border border-border bg-muted px-2 py-1 font-mono text-xs">
                                                    {formatRuleCondition(rule)}
                                                </span>
                                                {rule.priority !== undefined && (
                                                    <span className="rounded border border-border px-2 py-1 text-xs text-muted-foreground">
                                                        p{rule.priority}
                                                    </span>
                                                )}
                                                <span className="text-muted-foreground">→</span>
                                                {normalizeRuleTargets(rule).map((clusterName) => (
                                                    <span
                                                        key={clusterName}
                                                        className="rounded border border-primary/30 bg-primary/10 px-2 py-1 text-xs font-semibold text-primary"
                                                    >
                                                        {clusterName}
                                                    </span>
                                                ))}
                                            </li>
                                        ))}
                                        <li className="flex items-center gap-2 border-t border-border pt-2">
                                            <span className="text-muted-foreground">Default</span>
                                            <span className="text-muted-foreground">→</span>
                                            <span className="font-semibold text-primary">{ruleConfig.default_cluster}</span>
                                        </li>
                                    </ul>
                                    <IconButton
                                        variant="danger"
                                        className="absolute top-3 right-3"
                                        onClick={() => setRuleToDelete(collection)}
                                        title={`Delete routing rules for ${collection}`}
                                    >
                                        <Trash2 size={16} />
                                    </IconButton>
                                </div>
                            ))
                        ) : (
                            <EmptyState
                                icon={<GitBranch size={48} />}
                                title="No routing rules configured"
                                description="Select a collection and define routing rules to start sharding."
                            />
                        )}
                    </div>
                </Card>
            </div>
        </div>
    );
}
