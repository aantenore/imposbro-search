'use client';

import { useState, useEffect, useCallback } from 'react';
import { Trash2, Server } from 'lucide-react';
import { api } from '../../lib/api';
import { createEmptyClusterForm, formatClusterEndpoint } from '../../lib/clusterConfig';
import { useNotification, Notification } from '../../hooks/useNotification';
import { ConfirmationModal } from '../../components/ui';
import Card from '../../components/ui/Card';
import Button, { IconButton } from '../../components/ui/Button';
import Input, { Select } from '../../components/ui/Input';
import PageHeader from '../../components/ui/PageHeader';
import EmptyState from '../../components/ui/EmptyState';
import StatusBadge from '../../components/ui/StatusBadge';

/**
 * Clusters Page
 * 
 * Manages federated Typesense cluster registration and configuration.
 * Allows users to register new clusters and view/delete existing ones.
 */
export default function ClustersPage() {
    const [clusters, setClusters] = useState([]);
    const [newCluster, setNewCluster] = useState(createEmptyClusterForm);
    const [clusterToDelete, setClusterToDelete] = useState(null);
    const [isLoading, setIsLoading] = useState(true);
    const [isSubmitting, setIsSubmitting] = useState(false);
    const [health, setHealth] = useState(null);
    const { notification, showSuccess, showError } = useNotification();

    const fetchClusters = useCallback(async () => {
        try {
            setIsLoading(true);
            const [data, healthData] = await Promise.all([
                api.clusters.list(),
                api.health.get().catch(() => null),
            ]);
            setClusters(Object.values(data) || []);
            setHealth(healthData);
        } catch (err) {
            showError(err.message);
        } finally {
            setIsLoading(false);
        }
    }, [showError]);

    // Fetch clusters on mount
    useEffect(() => {
        fetchClusters();
    }, [fetchClusters]);

    const handleRegister = async (e) => {
        e.preventDefault();
        setIsSubmitting(true);

        try {
            await api.clusters.create(newCluster);
            showSuccess(`Cluster '${newCluster.name}' registered successfully!`);
            setNewCluster(createEmptyClusterForm());
            fetchClusters();
        } catch (err) {
            showError(err.message);
        } finally {
            setIsSubmitting(false);
        }
    };

    const handleDeleteConfirm = async () => {
        if (!clusterToDelete) return;

        try {
            await api.clusters.delete(clusterToDelete.name);
            showSuccess(`Cluster '${clusterToDelete.name}' deleted successfully!`);
            fetchClusters();
        } catch (err) {
            showError(err.message);
        } finally {
            setClusterToDelete(null);
        }
    };

    const handleInputChange = (field) => (e) => {
        const value = field === 'port' ? parseInt(e.target.value) : e.target.value;
        setNewCluster(prev => ({ ...prev, [field]: value }));
    };

    return (
        <div>
            {/* Delete Confirmation Modal */}
            {clusterToDelete && (
                <ConfirmationModal
                    resourceName={clusterToDelete.name}
                    resourceType="cluster"
                    onConfirm={handleDeleteConfirm}
                    onCancel={() => setClusterToDelete(null)}
                />
            )}

            <PageHeader
                title="Cluster Management"
                description="Register Typesense clusters that will store your data. You need at least one cluster before creating collections or routing rules."
            />

            {/* Notifications */}
            {notification && (
                <div className="mb-6">
                    <Notification {...notification} />
                </div>
            )}

            <div className="grid grid-cols-1 lg:grid-cols-2 gap-8">
                {/* Registered Clusters List */}
                <Card title="Registered Clusters" noPadding>
                    <div className="divide-y divide-border">
                        {isLoading ? (
                            <div className="p-6 text-center text-muted-foreground">Loading clusters...</div>
                        ) : clusters.length > 0 ? (
                            clusters.map((cluster) => (
                                <div
                                    key={cluster.name}
                                    className="flex items-center justify-between p-4 transition-colors hover:bg-muted/50"
                                >
                                    <div className="min-w-0">
                                        <p className="font-semibold text-foreground">{cluster.name}</p>
                                        <p className="text-xs font-mono text-muted-foreground">
                                            {formatClusterEndpoint(cluster)}
                                        </p>
                                        {cluster.name !== 'default' && (
                                            <div className="mt-2 flex flex-wrap items-center gap-2">
                                                <StatusBadge
                                                    variant={health?.data_clusters?.[cluster.name] === 'ok' ? 'success' : 'warning'}
                                                >
                                                    {health?.data_clusters?.[cluster.name] || 'unknown'}
                                                </StatusBadge>
                                                <span className="text-xs text-muted-foreground">
                                                    {(health?.data_cluster_nodes?.[cluster.name] || []).length} node(s)
                                                </span>
                                            </div>
                                        )}
                                    </div>
                                    {cluster.name !== 'default' && (
                                        <IconButton
                                            variant="danger"
                                            onClick={() => setClusterToDelete(cluster)}
                                            title={`Delete cluster ${cluster.name}`}
                                        >
                                            <Trash2 size={16} />
                                        </IconButton>
                                    )}
                                </div>
                            ))
                        ) : (
                            <EmptyState
                                icon={<Server size={48} />}
                                title="No clusters registered"
                                description="Register your first Typesense cluster to get started."
                            />
                        )}
                    </div>
                </Card>

                {/* Register New Cluster Form */}
                <Card title="Register New Cluster">
                    <p className="mb-4 text-sm text-muted-foreground">
                        Add a Typesense node or cluster. The name is used in routing rules (e.g. default-data-cluster).
                    </p>
                    <form onSubmit={handleRegister} className="space-y-4">
                        <div>
                            <label className="mb-1 block text-xs font-medium text-muted-foreground">Name</label>
                            <Input
                                placeholder="e.g. cluster-us"
                                value={newCluster.name}
                                onChange={handleInputChange('name')}
                                required
                            />
                        </div>
                        <div>
                            <label className="mb-1 block text-xs font-medium text-muted-foreground">Host</label>
                            <Input
                                placeholder="e.g. typesense.example.com"
                                value={newCluster.host}
                                onChange={handleInputChange('host')}
                                required
                            />
                        </div>
                        <div>
                            <label className="mb-1 block text-xs font-medium text-muted-foreground">Protocol</label>
                            <Select
                                value={newCluster.protocol}
                                onChange={handleInputChange('protocol')}
                                required
                            >
                                <option value="http">HTTP</option>
                                <option value="https">HTTPS</option>
                            </Select>
                        </div>
                        <div>
                            <label className="mb-1 block text-xs font-medium text-muted-foreground">Port</label>
                            <Input
                                type="number"
                                placeholder="8108"
                                value={newCluster.port}
                                onChange={handleInputChange('port')}
                                required
                            />
                        </div>
                        <div>
                            <label className="mb-1 block text-xs font-medium text-muted-foreground">API Key</label>
                            <Input
                                type="password"
                                placeholder="Typesense API key"
                                value={newCluster.api_key}
                                onChange={handleInputChange('api_key')}
                                required
                            />
                        </div>
                        <Button
                            type="submit"
                            variant="primary"
                            fullWidth
                            loading={isSubmitting}
                        >
                            Register Cluster
                        </Button>
                    </form>
                </Card>
            </div>
        </div>
    );
}
