'use client';

import { useState, useEffect } from 'react';
import { Plus, X, Trash2, Database } from 'lucide-react';
import { api } from '../../lib/api';
import { useNotification, Notification } from '../../hooks/useNotification';
import { ConfirmationModal } from '../../components/ui';
import Card from '../../components/ui/Card';
import Button, { IconButton } from '../../components/ui/Button';
import Input, { Select, Checkbox } from '../../components/ui/Input';
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
    { value: 'bool', label: 'Boolean' },
    { value: 'string[]', label: 'String Array' },
    { value: 'int32[]', label: 'Integer Array' },
];

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
    const { notification, showSuccess, showError } = useNotification();

    useEffect(() => {
        fetchRoutingMap();
    }, []);

    const fetchRoutingMap = async () => {
        try {
            setIsLoading(true);
            const data = await api.routing.getMap();
            setRoutingMap(data);
        } catch (err) {
            showError(err.message);
        } finally {
            setIsLoading(false);
        }
    };

    const handleFieldChange = (index, event) => {
        const values = [...newCollection.fields];
        const { name, type, checked, value } = event.target;
        values[index][name] = type === 'checkbox' ? checked : value;
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

        const schema = {
            ...newCollection,
            fields: newCollection.fields.filter(f => f.name.trim())
        };

        if (!schema.name.trim()) {
            showError('Collection Name is required.');
            return;
        }
        if (schema.fields.length === 0) {
            showError('At least one field is required.');
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
                description="Create and manage collection schemas. Collections are created across all federated clusters."
            />

            {/* Notifications */}
            {notification && (
                <div className="mb-6">
                    <Notification {...notification} />
                </div>
            )}

            <div className="grid grid-cols-1 lg:grid-cols-2 gap-8">
                {/* Existing Collections List */}
                <Card title="Existing Collections" noPadding>
                    <div className="divide-y divide-gray-700">
                        {isLoading ? (
                            <div className="p-6 text-center text-gray-400">Loading collections...</div>
                        ) : collections.length > 0 ? (
                            collections.map(([name, rule]) => (
                                <div
                                    key={name}
                                    className="p-4 flex justify-between items-center hover:bg-gray-700/30 transition-colors"
                                >
                                    <div className="flex flex-col gap-1">
                                        <span className="text-white font-semibold">{name}</span>
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
                </Card>

                {/* Create New Collection Form */}
                <Card title="Create New Collection">
                    <form onSubmit={handleCreate} className="space-y-4">
                        <Input
                            placeholder="Collection Name"
                            value={newCollection.name}
                            onChange={(e) => setNewCollection(prev => ({ ...prev, name: e.target.value }))}
                            required
                        />

                        <div>
                            <label className="block text-sm font-medium text-gray-300 mb-2">
                                Schema Fields
                            </label>

                            {newCollection.fields.map((field, index) => (
                                <div key={index} className="flex items-center gap-2 mt-2 p-3 bg-gray-900/50 rounded-lg">
                                    <Input
                                        className="flex-1"
                                        placeholder="Field Name"
                                        name="name"
                                        value={field.name}
                                        onChange={(e) => handleFieldChange(index, e)}
                                    />
                                    <Select
                                        className="w-36"
                                        name="type"
                                        value={field.type}
                                        onChange={(e) => handleFieldChange(index, e)}
                                    >
                                        {FIELD_TYPES.map(t => (
                                            <option key={t.value} value={t.value}>{t.label}</option>
                                        ))}
                                    </Select>
                                    <Checkbox
                                        name="facet"
                                        checked={field.facet}
                                        onChange={(e) => handleFieldChange(index, e)}
                                        label="Facet"
                                    />
                                    <IconButton
                                        variant="danger"
                                        onClick={() => handleRemoveField(index)}
                                    >
                                        <X size={16} />
                                    </IconButton>
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
