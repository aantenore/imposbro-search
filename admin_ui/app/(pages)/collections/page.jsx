'use client';

import { useState, useEffect, useCallback } from 'react';
import { Plus, X, Trash2, Database, ServerCog, Link2, RefreshCw, Tags } from 'lucide-react';
import { api } from '../../lib/api';
import { useNotification, Notification } from '../../hooks/useNotification';
import { ConfirmationModal } from '../../components/ui';
import Card from '../../components/ui/Card';
import Button, { IconButton } from '../../components/ui/Button';
import Input, { Select, Checkbox, Textarea } from '../../components/ui/Input';
import PageHeader from '../../components/ui/PageHeader';
import StatusBadge from '../../components/ui/StatusBadge';
import EmptyState from '../../components/ui/EmptyState';

/**
 * Typesense field types available for collection schemas
 */
const FIELD_TYPES = [
    { value: 'string', label: 'String' },
    { value: 'int32', label: 'Integer (32-bit)' },
    { value: 'int64', label: 'Integer (64-bit)' },
    { value: 'float', label: 'Float' },
    { value: 'float[]', label: 'Float Array / Vector' },
    { value: 'bool', label: 'Boolean' },
    { value: 'string[]', label: 'String Array' },
    { value: 'int32[]', label: 'Integer Array' },
];

function reconcileTotals(result) {
    const clusters = Object.values(result?.clusters || {});
    return clusters.reduce(
        (totals, report) => ({
            created: totals.created + (report.created?.length || 0),
            existing: totals.existing + (report.existing?.length || 0),
            clusters: totals.clusters + 1,
        }),
        { created: 0, existing: 0, clusters: 0 }
    );
}

function ReconcileReport({ result }) {
    if (!result) return null;

    const totals = reconcileTotals(result);
    const clusters = Object.entries(result.clusters || {});

    return (
        <div className="border-t border-border p-4">
            <div className="mb-3 flex flex-wrap items-center gap-2">
                <StatusBadge variant="info">{result.collections_desired ?? 0} desired</StatusBadge>
                <StatusBadge variant={totals.created > 0 ? 'success' : 'default'}>
                    {totals.created} created
                </StatusBadge>
                <StatusBadge variant="default">{totals.existing} existing</StatusBadge>
                <StatusBadge variant="default">{totals.clusters} clusters checked</StatusBadge>
            </div>
            <div className="space-y-2">
                {clusters.map(([cluster, report]) => (
                    <div
                        key={cluster}
                        className="rounded-md border border-border bg-muted/20 px-3 py-2"
                    >
                        <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                            <span className="font-mono text-sm font-semibold text-foreground">
                                {cluster}
                            </span>
                            <StatusBadge variant={report.created?.length ? 'success' : 'default'}>
                                {report.created?.length || 0} created
                            </StatusBadge>
                        </div>
                        <p className="text-xs text-muted-foreground">
                            Created: {(report.created || []).join(', ') || 'none'}
                        </p>
                        <p className="mt-1 text-xs text-muted-foreground">
                            Existing: {(report.existing || []).join(', ') || 'none'}
                        </p>
                    </div>
                ))}
            </div>
        </div>
    );
}

function normalizeAliases(payload) {
    const aliases = payload?.aliases?.aliases || payload?.aliases || [];
    return Array.isArray(aliases) ? aliases : [];
}

function getAliasName(alias) {
    return alias.name || alias.alias_name || alias.id || '';
}

/**
 * Collections Page
 * 
 * Manages Typesense collection schemas across all federated clusters.
 * Allows creating new collections with custom field definitions.
 */
export default function CollectionsPage() {
    const [routingMap, setRoutingMap] = useState({ clusters: [], collections: {} });
    const [newCollection, setNewCollection] = useState({
        name: '',
        default_sorting_field: '',
        fields: [{ name: '', type: 'string', facet: false }]
    });
    const [collectionToDelete, setCollectionToDelete] = useState(null);
    const [isLoading, setIsLoading] = useState(true);
    const [isSubmitting, setIsSubmitting] = useState(false);
    const [isReconciling, setIsReconciling] = useState(false);
    const [reconcileResult, setReconcileResult] = useState(null);
    const [selectedAliasCluster, setSelectedAliasCluster] = useState('');
    const [aliases, setAliases] = useState([]);
    const [aliasName, setAliasName] = useState('');
    const [aliasTargetCollection, setAliasTargetCollection] = useState('');
    const [aliasToDelete, setAliasToDelete] = useState(null);
    const [isLoadingAliases, setIsLoadingAliases] = useState(false);
    const [isSavingAlias, setIsSavingAlias] = useState(false);
    const { notification, showSuccess, showError } = useNotification();

    const fetchRoutingMap = useCallback(async () => {
        try {
            setIsLoading(true);
            const data = await api.routing.getMap();
            setRoutingMap(data);
            const clusters = data.clusters || [];
            setSelectedAliasCluster((previous) => (
                previous && clusters.includes(previous) ? previous : clusters[0] || ''
            ));
        } catch (err) {
            showError(err.message);
        } finally {
            setIsLoading(false);
        }
    }, [showError]);

    useEffect(() => {
        fetchRoutingMap();
    }, [fetchRoutingMap]);

    const fetchAliases = useCallback(async (clusterName = selectedAliasCluster) => {
        if (!clusterName) {
            setAliases([]);
            return;
        }
        setIsLoadingAliases(true);
        try {
            const data = await api.aliases.list({ clusterName });
            setAliases(normalizeAliases(data));
        } catch (err) {
            setAliases([]);
            showError(err.message);
        } finally {
            setIsLoadingAliases(false);
        }
    }, [selectedAliasCluster, showError]);

    useEffect(() => {
        fetchAliases();
    }, [fetchAliases]);

    const handleFieldChange = (index, event) => {
        const values = [...newCollection.fields];
        const { name, type, checked, value } = event.target;
        values[index][name] = type === 'checkbox' ? checked : value;
        if (name === 'type') {
            if (value === 'float[]') {
                values[index].facet = false;
            } else {
                delete values[index].num_dim;
                delete values[index].embed_json;
            }
        }
        setNewCollection(prev => ({ ...prev, fields: values }));
    };

    const handleAddField = () => {
        setNewCollection(prev => ({
            ...prev,
            fields: [...prev.fields, { name: '', type: 'string', facet: false }]
        }));
    };

    const handleRemoveField = (index) => {
        setNewCollection(prev => ({
            ...prev,
            fields: prev.fields.filter((_, i) => i !== index)
        }));
    };

    const handleCreate = async (e) => {
        e.preventDefault();

        let fields;
        try {
            fields = newCollection.fields
                .filter(f => f.name.trim())
                .map(({ embed_json, num_dim, ...field }) => {
                    const nextField = { ...field };
                    if (field.type === 'float[]') {
                        if (num_dim) nextField.num_dim = Number(num_dim);
                        if (embed_json?.trim()) {
                            nextField.embed = JSON.parse(embed_json);
                        }
                    }
                    return nextField;
                });
        } catch (err) {
            showError(`Invalid embed JSON: ${err.message}`);
            return;
        }

        const schema = {
            ...newCollection,
            fields,
        };
        if (!schema.default_sorting_field) {
            delete schema.default_sorting_field;
        }

        if (!schema.name.trim()) {
            showError('Collection Name is required.');
            return;
        }
        if (schema.fields.length === 0) {
            showError('At least one field is required.');
            return;
        }
        const invalidVectorField = schema.fields.find(
            (field) => field.type === 'float[]' && !field.num_dim
        );
        if (invalidVectorField) {
            showError(`Vector field '${invalidVectorField.name}' requires num_dim.`);
            return;
        }
        if (
            schema.default_sorting_field &&
            !schema.fields.some((field) => field.name === schema.default_sorting_field)
        ) {
            showError('Default sorting field must be one of the schema fields.');
            return;
        }

        setIsSubmitting(true);
        try {
            await api.collections.create(schema);
            showSuccess(`Collection '${schema.name}' created successfully on all clusters!`);
            setNewCollection({
                name: '',
                default_sorting_field: '',
                fields: [{ name: '', type: 'string', facet: false }]
            });
            fetchRoutingMap();
        } catch (err) {
            showError(err.message);
        } finally {
            setIsSubmitting(false);
        }
    };

    const handleDeleteConfirm = async () => {
        if (!collectionToDelete) return;

        try {
            await api.collections.delete(collectionToDelete);
            showSuccess(`Collection '${collectionToDelete}' deleted successfully!`);
            fetchRoutingMap();
        } catch (err) {
            showError(err.message);
        } finally {
            setCollectionToDelete(null);
        }
    };

    const handleReconcile = async () => {
        setIsReconciling(true);
        try {
            const result = await api.collections.reconcile();
            const totals = reconcileTotals(result);
            setReconcileResult(result);
            showSuccess(
                totals.created > 0
                    ? `Reconciled ${totals.created} missing collection schema(s).`
                    : 'All desired collection schemas are already present.'
            );
            fetchRoutingMap();
        } catch (err) {
            showError(err.message);
        } finally {
            setIsReconciling(false);
        }
    };

    const handleUpsertAlias = async (event) => {
        event.preventDefault();
        if (!selectedAliasCluster || !aliasName.trim() || !aliasTargetCollection) {
            showError('Select a cluster, alias name, and target collection.');
            return;
        }

        setIsSavingAlias(true);
        try {
            await api.aliases.upsert({
                aliasName: aliasName.trim(),
                collectionName: aliasTargetCollection,
                clusterName: selectedAliasCluster,
            });
            showSuccess(`Alias '${aliasName.trim()}' now points to '${aliasTargetCollection}'.`);
            setAliasName('');
            fetchAliases(selectedAliasCluster);
        } catch (err) {
            showError(err.message);
        } finally {
            setIsSavingAlias(false);
        }
    };

    const handleDeleteAliasConfirm = async () => {
        if (!aliasToDelete) return;

        try {
            await api.aliases.delete({
                aliasName: aliasToDelete.name,
                clusterName: aliasToDelete.cluster,
            });
            showSuccess(`Alias '${aliasToDelete.name}' deleted.`);
            fetchAliases(aliasToDelete.cluster);
        } catch (err) {
            showError(err.message);
        } finally {
            setAliasToDelete(null);
        }
    };

    const collections = Object.entries(routingMap.collections);
    const collectionNames = collections.map(([name]) => name);
    const defaultSortCandidates = newCollection.fields.filter(
        (field) => field.name.trim() && ['int32', 'int64', 'float'].includes(field.type)
    );

    return (
        <div>
            {/* Delete Confirmation Modal */}
            {collectionToDelete && (
                <ConfirmationModal
                    resourceName={collectionToDelete}
                    resourceType="collection"
                    onConfirm={handleDeleteConfirm}
                    onCancel={() => setCollectionToDelete(null)}
                />
            )}
            {aliasToDelete && (
                <ConfirmationModal
                    resourceName={aliasToDelete.name}
                    resourceType="alias"
                    message={`Delete alias "${aliasToDelete.name}" from "${aliasToDelete.cluster}"?`}
                    confirmText="Delete Alias"
                    onConfirm={handleDeleteAliasConfirm}
                    onCancel={() => setAliasToDelete(null)}
                />
            )}

            <PageHeader
                title="Collection Management"
                description="Create search indexes (collections) with a schema. Each collection is created on all registered clusters; use Routing to decide which cluster stores which documents."
            />

            {/* Notifications */}
            {notification && (
                <div className="mb-6">
                    <Notification {...notification} />
                </div>
            )}

            <div className="grid grid-cols-1 lg:grid-cols-2 gap-8">
                {/* Existing Collections List */}
                <Card
                    title="Existing Collections"
                    action={
                        <Button
                            type="button"
                            variant="outline"
                            size="sm"
                            leftIcon={<ServerCog size={16} />}
                            loading={isReconciling}
                            onClick={handleReconcile}
                        >
                            Reconcile
                        </Button>
                    }
                    noPadding
                >
                    <div className="divide-y divide-border">
                        {isLoading ? (
                            <div className="p-6 text-center text-muted-foreground">Loading collections...</div>
                        ) : collections.length > 0 ? (
                            collections.map(([name, rule]) => (
                                <div
                                    key={name}
                                    className="flex items-center justify-between p-4 transition-colors hover:bg-muted/50"
                                >
                                    <div className="flex flex-col gap-1">
                                        <span className="font-semibold text-foreground">{name}</span>
                                        <StatusBadge
                                            variant={rule.rules?.length > 0 ? 'purple' : 'default'}
                                        >
                                            {rule.rules?.length > 0 ? 'Sharded' : 'Not Sharded'}
                                        </StatusBadge>
                                    </div>
                                    <IconButton
                                        variant="danger"
                                        onClick={() => setCollectionToDelete(name)}
                                        title={`Delete collection ${name}`}
                                    >
                                        <Trash2 size={16} />
                                    </IconButton>
                                </div>
                            ))
                        ) : (
                            <EmptyState
                                icon={<Database size={48} />}
                                title="No collections created"
                                description="Create your first collection to start indexing documents."
                            />
                        )}
                    </div>
                    <ReconcileReport result={reconcileResult} />
                </Card>

                {/* Create New Collection Form */}
                <Card title="Create New Collection">
                    <p className="mb-4 text-sm text-muted-foreground">
                        Define a name and at least one field. The collection will be created on every registered cluster.
                    </p>
                    <form onSubmit={handleCreate} className="space-y-4">
                        <Input
                            label="Collection name"
                            name="collection_name"
                            placeholder="e.g. products"
                            value={newCollection.name}
                            onChange={(e) => setNewCollection(prev => ({ ...prev, name: e.target.value }))}
                            required
                        />
                        <Select
                            label="Default sorting field"
                            value={newCollection.default_sorting_field}
                            onChange={(e) => setNewCollection(prev => ({
                                ...prev,
                                default_sorting_field: e.target.value,
                            }))}
                        >
                            <option value="">None</option>
                            {defaultSortCandidates.map((field) => (
                                <option key={`${field.name}-${field.type}`} value={field.name}>
                                    {field.name} ({field.type})
                                </option>
                            ))}
                        </Select>

                        <div>
                            <label className="mb-2 block text-sm font-medium text-foreground">
                                Schema fields
                            </label>

                            {newCollection.fields.map((field, index) => (
                                <div key={index} className="mt-2 rounded-lg border border-border bg-muted/30 p-3">
                                    <div className="flex flex-col gap-2 xl:flex-row xl:items-center">
                                        <Input
                                            className="flex-1"
                                            aria-label={`Field ${index + 1} name`}
                                            placeholder="Field Name"
                                            name="name"
                                            value={field.name}
                                            onChange={(e) => handleFieldChange(index, e)}
                                        />
                                        <Select
                                            className="xl:w-44"
                                            aria-label={`Field ${index + 1} type`}
                                            name="type"
                                            value={field.type}
                                            onChange={(e) => handleFieldChange(index, e)}
                                        >
                                            {FIELD_TYPES.map(t => (
                                                <option key={t.value} value={t.value}>{t.label}</option>
                                            ))}
                                        </Select>
                                        {field.type === 'float[]' && (
                                            <Input
                                                className="xl:w-28"
                                                aria-label={`Field ${index + 1} vector dimensions`}
                                                placeholder="num_dim"
                                                name="num_dim"
                                                type="number"
                                                min="1"
                                                value={field.num_dim || ''}
                                                onChange={(e) => handleFieldChange(index, e)}
                                            />
                                        )}
                                        <Checkbox
                                            name="facet"
                                            checked={field.facet}
                                            onChange={(e) => handleFieldChange(index, e)}
                                            label="Facet"
                                            disabled={field.type === 'float[]'}
                                        />
                                        <IconButton
                                            type="button"
                                            variant="danger"
                                            aria-label={`Remove field ${index + 1}`}
                                            onClick={() => handleRemoveField(index)}
                                        >
                                            <X size={16} />
                                        </IconButton>
                                    </div>
                                    {field.type === 'float[]' && (
                                        <Textarea
                                            className="mt-3 min-h-24 font-mono text-xs"
                                            aria-label={`Field ${index + 1} embedding configuration`}
                                            placeholder='{"from":["title"],"model_config":{"model_name":"ts/all-MiniLM-L12-v2"}}'
                                            name="embed_json"
                                            value={field.embed_json || ''}
                                            onChange={(e) => handleFieldChange(index, e)}
                                            spellCheck={false}
                                        />
                                    )}
                                </div>
                            ))}

                            <Button
                                type="button"
                                variant="ghost"
                                size="sm"
                                leftIcon={<Plus size={16} />}
                                onClick={handleAddField}
                                className="mt-3"
                            >
                                Add Field
                            </Button>
                        </div>

                        <Button
                            type="submit"
                            variant="success"
                            fullWidth
                            loading={isSubmitting}
                        >
                            Create Collection
                        </Button>
                    </form>
                </Card>
            </div>

            <div className="mt-8">
                <Card
                    title="Collection Aliases"
                    action={
                        <Button
                            type="button"
                            variant="outline"
                            size="sm"
                            leftIcon={<RefreshCw size={16} />}
                            loading={isLoadingAliases}
                            disabled={!selectedAliasCluster}
                            onClick={() => fetchAliases(selectedAliasCluster)}
                        >
                            Refresh
                        </Button>
                    }
                >
                    <div className="grid gap-6 lg:grid-cols-[minmax(280px,420px),1fr]">
                        <form onSubmit={handleUpsertAlias} className="space-y-4">
                            <Select
                                label="Cluster"
                                value={selectedAliasCluster}
                                onChange={(event) => setSelectedAliasCluster(event.target.value)}
                            >
                                {routingMap.clusters.map((cluster) => (
                                    <option key={cluster} value={cluster}>{cluster}</option>
                                ))}
                            </Select>
                            <Input
                                label="Alias"
                                placeholder="products_live"
                                value={aliasName}
                                onChange={(event) => setAliasName(event.target.value)}
                            />
                            <Select
                                label="Target collection"
                                value={aliasTargetCollection}
                                onChange={(event) => setAliasTargetCollection(event.target.value)}
                            >
                                <option value="">Select collection</option>
                                {collectionNames.map((name) => (
                                    <option key={name} value={name}>{name}</option>
                                ))}
                            </Select>
                            <Button
                                type="submit"
                                leftIcon={<Link2 size={16} />}
                                loading={isSavingAlias}
                                disabled={!selectedAliasCluster || collectionNames.length === 0}
                            >
                                Save alias
                            </Button>
                        </form>

                        <div className="rounded-lg border border-border">
                            {isLoadingAliases ? (
                                <div className="p-6 text-center text-muted-foreground">Loading aliases...</div>
                            ) : aliases.length > 0 ? (
                                <div className="divide-y divide-border">
                                    {aliases.map((alias, index) => {
                                        const name = getAliasName(alias) || `alias-${index}`;
                                        return (
                                            <div
                                                key={`${name}-${alias.collection_name || index}`}
                                                className="flex items-center justify-between gap-4 p-4"
                                            >
                                                <div className="min-w-0">
                                                    <div className="flex flex-wrap items-center gap-2">
                                                        <Tags className="h-4 w-4 text-primary" />
                                                        <span className="font-mono text-sm font-semibold text-foreground">
                                                            {name}
                                                        </span>
                                                    </div>
                                                    <p className="mt-1 truncate text-sm text-muted-foreground">
                                                        {alias.collection_name || 'unknown collection'}
                                                    </p>
                                                </div>
                                                <IconButton
                                                    type="button"
                                                    variant="danger"
                                                    onClick={() => setAliasToDelete({
                                                        name,
                                                        cluster: selectedAliasCluster,
                                                    })}
                                                    title={`Delete alias ${name}`}
                                                >
                                                    <Trash2 size={16} />
                                                </IconButton>
                                            </div>
                                        );
                                    })}
                                </div>
                            ) : (
                                <EmptyState
                                    icon={<Tags size={44} />}
                                    title="No aliases on this cluster"
                                    description="Create an alias to switch search traffic between versioned collections."
                                />
                            )}
                        </div>
                    </div>
                </Card>
            </div>
        </div>
    );
}
