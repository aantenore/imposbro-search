'use client';

import { useState, useEffect, useCallback } from 'react';
import { Plus, X, Trash2, Database, ServerCog } from 'lucide-react';
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
        fields: [{ name: '', type: 'string', facet: false }]
    });
    const [collectionToDelete, setCollectionToDelete] = useState(null);
    const [isLoading, setIsLoading] = useState(true);
    const [isSubmitting, setIsSubmitting] = useState(false);
    const [isReconciling, setIsReconciling] = useState(false);
    const [reconcileResult, setReconcileResult] = useState(null);
    const { notification, showSuccess, showError } = useNotification();

    const fetchRoutingMap = useCallback(async () => {
        try {
            setIsLoading(true);
            const data = await api.routing.getMap();
            setRoutingMap(data);
        } catch (err) {
            showError(err.message);
        } finally {
            setIsLoading(false);
        }
    }, [showError]);

    useEffect(() => {
        fetchRoutingMap();
    }, [fetchRoutingMap]);

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

        setIsSubmitting(true);
        try {
            await api.collections.create(schema);
            showSuccess(`Collection '${schema.name}' created successfully on all clusters!`);
            setNewCollection({ name: '', fields: [{ name: '', type: 'string', facet: false }] });
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

    const collections = Object.entries(routingMap.collections);

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
                        <div>
                            <label className="mb-1 block text-xs font-medium text-muted-foreground">Collection name</label>
                            <Input
                                placeholder="e.g. products"
                                value={newCollection.name}
                                onChange={(e) => setNewCollection(prev => ({ ...prev, name: e.target.value }))}
                                required
                            />
                        </div>

                        <div>
                            <label className="mb-2 block text-sm font-medium text-foreground">
                                Schema fields
                            </label>

                            {newCollection.fields.map((field, index) => (
                                <div key={index} className="mt-2 rounded-lg border border-border bg-muted/30 p-3">
                                    <div className="flex flex-col gap-2 xl:flex-row xl:items-center">
                                        <Input
                                            className="flex-1"
                                            placeholder="Field Name"
                                            name="name"
                                            value={field.name}
                                            onChange={(e) => handleFieldChange(index, e)}
                                        />
                                        <Select
                                            className="xl:w-44"
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
                                            variant="danger"
                                            onClick={() => handleRemoveField(index)}
                                        >
                                            <X size={16} />
                                        </IconButton>
                                    </div>
                                    {field.type === 'float[]' && (
                                        <Textarea
                                            className="mt-3 min-h-24 font-mono text-xs"
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
        </div>
    );
}
