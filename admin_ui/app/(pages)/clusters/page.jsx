'use client';
import { useState, useEffect } from 'react';
import { Trash2 } from 'lucide-react'; // Import delete icon

const API_BASE = '/api';

// Confirmation Modal Component
const ConfirmationModal = ({ onConfirm, onCancel, clusterName }) => {
    return (
        <div className="fixed inset-0 bg-black bg-opacity-70 flex justify-center items-center z-50">
            <div className="bg-gray-800 rounded-lg p-6 shadow-xl border border-gray-700 w-full max-w-md mx-4">
                <h2 className="text-xl font-bold text-white mb-4">Confirm Deletion</h2>
                <p className="text-gray-300 mb-6">
                    Are you sure you want to delete the cluster <span className="font-bold text-red-400">{clusterName}</span>? This action cannot be undone.
                </p>
                <div className="flex justify-end space-x-4">
                    <button
                        onClick={onCancel}
                        className="px-4 py-2 rounded-md bg-gray-600 hover:bg-gray-500 text-white font-semibold transition-colors"
                    >
                        Cancel
                    </button>
                    <button
                        onClick={onConfirm}
                        className="px-4 py-2 rounded-md bg-red-600 hover:bg-red-700 text-white font-bold transition-colors"
                    >
                        Delete Cluster
                    </button>
                </div>
            </div>
        </div>
    );
};


export default function ClustersPage() {
    const [clusters, setClusters] = useState([]);
    const [newCluster, setNewCluster] = useState({ name: '', host: '', port: 8108, api_key: '' });
    const [error, setError] = useState('');
    const [success, setSuccess] = useState('');
    const [clusterToDelete, setClusterToDelete] = useState(null); // State to manage which cluster is targeted for deletion

    const fetchClusters = async () => {
        try {
            const res = await fetch(`${API_BASE}/admin/federation/clusters`);
            if (!res.ok) throw new Error('Failed to fetch clusters');
            const data = await res.json();
            setClusters(Object.values(data) || []);
        } catch (err) { setError(err.message); }
    };

    useEffect(() => { fetchClusters(); }, []);

    const handleRegister = async (e) => {
        e.preventDefault();
        setError(''); setSuccess('');
        try {
            const res = await fetch(`${API_BASE}/admin/federation/clusters`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(newCluster),
            });
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || 'Failed to register cluster');
            setSuccess(`Cluster '${newCluster.name}' registered successfully!`);
            setNewCluster({ name: '', host: '', port: 8108, api_key: '' });
            fetchClusters();
        } catch (err) { setError(err.message); }
    };

    // --- NEW: Function to handle the confirmed deletion ---
    const handleDeleteConfirm = async () => {
        if (!clusterToDelete) return;
        setError(''); setSuccess('');

        try {
            const res = await fetch(`${API_BASE}/admin/federation/clusters/${clusterToDelete.name}`, {
                method: 'DELETE',
            });
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || 'Failed to delete cluster');

            setSuccess(`Cluster '${clusterToDelete.name}' deleted successfully!`);
            fetchClusters(); // Refresh the list of clusters
        } catch (err) {
            setError(err.message);
        } finally {
            setClusterToDelete(null); // Close the modal regardless of outcome
        }
    };

    return (
        <div>
            {/* --- NEW: Render the confirmation modal conditionally --- */}
            {clusterToDelete && (
                <ConfirmationModal
                    clusterName={clusterToDelete.name}
                    onConfirm={handleDeleteConfirm}
                    onCancel={() => setClusterToDelete(null)}
                />
            )}

            <h1 className='text-3xl font-bold mb-4'>Cluster Management</h1>
            <div className='grid grid-cols-1 lg:grid-cols-2 gap-8'>
                <div>
                    <h2 className='text-xl font-semibold mb-3'>Registered Clusters</h2>
                    <div className='bg-gray-800/50 border border-gray-700 rounded-lg p-4 space-y-1'>
                        {clusters.length > 0 ? (
                            clusters.map(c =>
                                // --- UPDATED: Cluster list item with delete button ---
                                <div key={c.name} className='p-3 flex justify-between items-center border-b border-gray-700 last:border-b-0'>
                                    <div>
                                        <p className='font-semibold text-white'>{c.name}</p>
                                        <p className='text-xs text-gray-400 font-mono'>{c.host}:{c.port}</p>
                                    </div>
                                    {c.name !== 'default' && ( // Prevent showing delete button for the default cluster
                                        <button
                                            onClick={() => setClusterToDelete(c)} // Open confirmation modal
                                            className='p-2 text-gray-400 hover:text-red-500 hover:bg-red-900/50 rounded-full transition-colors'
                                            title={`Delete cluster ${c.name}`}
                                        >
                                            <Trash2 size={16}/>
                                        </button>
                                    )}
                                </div>
                            )
                        ) : (<p className='text-gray-400 p-3'>No clusters registered yet.</p>)}
                    </div>
                </div>
                <div>
                    <h2 className='text-xl font-semibold mb-3'>Register New Cluster</h2>
                    <form onSubmit={handleRegister} className='bg-gray-800/50 border border-gray-700 rounded-lg p-6 space-y-4'>
                        <input type='text' placeholder='Cluster Name (e.g., cluster-us)' value={newCluster.name} onChange={(e) => setNewCluster({...newCluster, name: e.target.value})} className='w-full bg-gray-900 border-gray-600 rounded-md' required />
                        <input type='text' placeholder='Host (e.g., typesense-replica)' value={newCluster.host} onChange={(e) => setNewCluster({...newCluster, host: e.target.value})} className='w-full bg-gray-900 border-gray-600 rounded-md' required />
                        <input type='number' placeholder='Port' value={newCluster.port} onChange={(e) => setNewCluster({...newCluster, port: parseInt(e.target.value)})} className='w-full bg-gray-900 border-gray-600 rounded-md' required />
                        <input type='text' placeholder='API Key' value={newCluster.api_key} onChange={(e) => setNewCluster({...newCluster, api_key: e.target.value})} className='w-full bg-gray-900 border-gray-600 rounded-md' required />
                        <button type='submit' className='w-full bg-blue-600 hover:bg-blue-700 rounded-md py-2 font-semibold transition-colors'>Register Cluster</button>
                        {success && <p className='text-green-400 text-sm mt-2'>{success}</p>}
                        {error && <p className='text-red-400 text-sm mt-2'>{error}</p>}
                    </form>
                </div>
            </div>
        </div>
    );
}
