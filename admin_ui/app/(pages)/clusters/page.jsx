'use client';

import { useState, useEffect } from 'react';
import { Trash2, Server } from 'lucide-react';
import { api } from '../../lib/api';
import { useNotification, Notification } from '../../hooks/useNotification';
import { ConfirmationModal } from '../../components/ui';
import Card from '../../components/ui/Card';
import Button, { IconButton } from '../../components/ui/Button';
import Input from '../../components/ui/Input';
import PageHeader from '../../components/ui/PageHeader';
import EmptyState from '../../components/ui/EmptyState';

/**
 * Clusters Page
 * 
 * Manages federated Typesense cluster registration and configuration.
 * Allows users to register new clusters and view/delete existing ones.
 */
export default function ClustersPage() {
    const [clusters, setClusters] = useState([]);
    const [newCluster, setNewCluster] = useState({
        name: '',
        host: '',
        port: 8108,
        api_key: ''
    });
    const [clusterToDelete, setClusterToDelete] = useState(null);
    const [isLoading, setIsLoading] = useState(true);
    const [isSubmitting, setIsSubmitting] = useState(false);
    const { notification, showSuccess, showError } = useNotification();

    // Fetch clusters on mount
    useEffect(() => {
        fetchClusters();
    }, []);

    const fetchClusters = async () => {
        try {
            setIsLoading(true);
            const data = await api.clusters.list();
            setClusters(Object.values(data) || []);
        } catch (err) {
            showError(err.message);
        } finally {
            setIsLoading(false);
        }
    };

    const handleRegister = async (e) => {
        e.preventDefault();
        setIsSubmitting(true);

        try {
            await api.clusters.create(newCluster);
            showSuccess(`Cluster '${newCluster.name}' registered successfully!`);
            setNewCluster({ name: '', host: '', port: 8108, api_key: '' });
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
                description="Register and manage your federated Typesense clusters. Each cluster can store sharded document data."
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
                    <div className="divide-y divide-gray-700">
                        {isLoading ? (
                            <div className="p-6 text-center text-gray-400">Loading clusters...</div>
                        ) : clusters.length > 0 ? (
                            clusters.map((cluster) => (
                                <div
                                    key={cluster.name}
                                    className="p-4 flex justify-between items-center hover:bg-gray-700/30 transition-colors"
                                >
                                    <div>
                                        <p className="font-semibold text-white">{cluster.name}</p>
                                        <p className="text-xs text-gray-400 font-mono">
                                            {cluster.host}:{cluster.port}
                                        </p>
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
                    <form onSubmit={handleRegister} className="space-y-4">
                        <Input
                            placeholder="Cluster Name (e.g., cluster-us)"
                            value={newCluster.name}
                            onChange={handleInputChange('name')}
                            required
                        />
                        <Input
                            placeholder="Host (e.g., typesense-replica)"
                            value={newCluster.host}
                            onChange={handleInputChange('host')}
                            required
                        />
                        <Input
                            type="number"
                            placeholder="Port"
                            value={newCluster.port}
                            onChange={handleInputChange('port')}
                            required
                        />
                        <Input
                            type="password"
                            placeholder="API Key"
                            value={newCluster.api_key}
                            onChange={handleInputChange('api_key')}
                            required
                        />
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
