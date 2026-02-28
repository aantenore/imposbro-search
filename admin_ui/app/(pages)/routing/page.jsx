'use client';

import { useState, useEffect } from 'react';
import { Plus, X, GitBranch, Trash2 } from 'lucide-react';
import { api } from '../../lib/api';
import { useNotification, Notification } from '../../hooks/useNotification';
import { ConfirmationModal } from '../../components/ui';
import Card from '../../components/ui/Card';
import Button, { IconButton } from '../../components/ui/Button';
import Input, { Select } from '../../components/ui/Input';
import PageHeader from '../../components/ui/PageHeader';
import EmptyState from '../../components/ui/EmptyState';

/**
 * Routing Page
 * 
 * Manages document-level routing rules for collections.
 * Allows defining rules that determine which cluster stores each document.
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
    const { notification, showSuccess, showError } = useNotification();

    useEffect(() => {
        fetchData();
    }, []);

    useEffect(() => {
        if (selectedCollection) {
            fetchSchemaAndSetRules();
        } else {
            setAvailableFields([]);
            setRules([]);
        }
    }, [selectedCollection, clusters, currentRules]);

    const fetchData = async () => {
        try {
            const data = await api.routing.getMap();
            setCollections(Object.keys(data.collections || {}));
            setClusters(data.clusters || []);
            setCurrentRules(data.collections || {});
            if (data.clusters?.length > 0) {
                setDefaultCluster(data.clusters[0]);
            }
        } catch (err) {
            showError(err.message);
        }
    };

    const fetchSchemaAndSetRules = async () => {
        try {
            const schemaData = await api.collections.get(selectedCollection);
            setAvailableFields(schemaData.fields || []);

            const existingRuleSet = currentRules[selectedCollection];
            if (existingRuleSet?.rules) {
                setRules(existingRuleSet.rules);
                setDefaultCluster(existingRuleSet.default_cluster);
            } else {
                setRules([]);
                setDefaultCluster(clusters[0] || '');
            }
        } catch (err) {
            showError(`Could not fetch schema for ${selectedCollection}.`);
            setAvailableFields([]);
            setRules([]);
        }
    };

    const handleAddRule = () => {
        if (availableFields.length === 0) return;
        setRules(prev => [
            ...prev,
            { field: availableFields[0].name, value: '', cluster: clusters[0] || '' }
        ]);
    };

    const handleRemoveRule = (index) => {
        setRules(prev => prev.filter((_, i) => i !== index));
    };

    const handleRuleChange = (index, event) => {
        const { name, value } = event.target;
        setRules(prev => {
            const newRules = [...prev];
            newRules[index][name] = value;
            return newRules;
        });
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
                rules: rules.filter(r => r.field && r.value && r.cluster),
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

    const configuredRules = Object.entries(currentRules)
        .filter(([_, rule]) => rule.rules?.length > 0);

    return (
        <div>
            {/* Delete Confirmation Modal */}
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
                description="Define rules to shard a collection's documents across multiple clusters. The first matching rule determines the target cluster."
            />

            {/* Notifications */}
            {notification && (
                <div className="mb-6">
                    <Notification {...notification} />
                </div>
            )}

            <div className="grid grid-cols-1 lg:grid-cols-2 gap-8">
                {/* Set/Update Rules Form */}
                <Card title="Set / Update Rules">
                    <form onSubmit={handleSubmit} className="space-y-6">
                        {/* Step 1: Select Collection */}
                        <div>
                            <label className="mb-2 block text-sm font-medium text-foreground">
                                1. Select Collection
                            </label>
                            <Select
                                value={selectedCollection}
                                onChange={(e) => setSelectedCollection(e.target.value)}
                            >
                                <option value="">-- Select a Collection --</option>
                                {collections.map(c => (
                                    <option key={c} value={c}>{c}</option>
                                ))}
                            </Select>
                        </div>

                        {selectedCollection && (
                            <>
                                {/* Step 2: Define Rules */}
                                <div>
                                    <label className="block text-sm font-medium text-gray-300 mb-2">
                                        2. Define Specific Rules
                                    </label>

                                    {rules.map((rule, index) => (
                                        <div
                                            key={index}
                                            className="grid grid-cols-12 gap-2 items-center mt-2 p-3 bg-gray-900/50 rounded-lg"
                                        >
                                            <Select
                                                className="col-span-4"
                                                name="field"
                                                value={rule.field}
                                                onChange={(e) => handleRuleChange(index, e)}
                                                disabled={availableFields.length === 0}
                                            >
                                                <option value="">-- Field --</option>
                                                {availableFields.map(f => (
                                                    <option key={f.name} value={f.name}>{f.name}</option>
                                                ))}
                                            </Select>
                                            <Input
                                                className="col-span-4"
                                                name="value"
                                                placeholder="Value"
                                                value={rule.value}
                                                onChange={(e) => handleRuleChange(index, e)}
                                            />
                                            <Select
                                                className="col-span-3"
                                                name="cluster"
                                                value={rule.cluster}
                                                onChange={(e) => handleRuleChange(index, e)}
                                            >
                                                {clusters.map(c => (
                                                    <option key={c} value={c}>{c}</option>
                                                ))}
                                            </Select>
                                            <IconButton
                                                variant="danger"
                                                onClick={() => handleRemoveRule(index)}
                                            >
                                                <X size={16} />
                                            </IconButton>
                                        </div>
                                    ))}

                                    <Button
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

                                {/* Step 3: Default Cluster */}
                                <div>
                                    <label className="mb-2 block text-sm font-medium text-foreground">
                                        3. Set Default Cluster
                                    </label>
                                    <Select
                                        value={defaultCluster}
                                        onChange={(e) => setDefaultCluster(e.target.value)}
                                    >
                                        {clusters.map(c => (
                                            <option key={c} value={c}>{c}</option>
                                        ))}
                                    </Select>
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

                {/* Current Routing Configuration */}
                <Card title="Current Routing Configuration" noPadding>
                    <div className="divide-y divide-border">
                        {configuredRules.length > 0 ? (
                            configuredRules.map(([collection, ruleConfig]) => (
                                <div key={collection} className="relative p-4 transition-colors hover:bg-muted/50">
                                    <p className="mb-3 font-bold text-foreground">{collection}</p>
                                    <ul className="space-y-2 text-sm">
                                        {ruleConfig.rules.map((r, i) => (
                                            <li key={i} className="flex items-center gap-2">
                                                <span className="rounded border border-border bg-muted px-2 py-1 font-mono text-xs">
                                                    {r.field}: {r.value}
                                                </span>
                                                <span className="text-muted-foreground">→</span>
                                                <span className="font-semibold text-primary">{r.cluster}</span>
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
