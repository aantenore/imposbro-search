'use client';
import { useState, useEffect } from 'react';
const API_BASE = '/api';

export default function ClustersPage() {
    const [clusters, setClusters] = useState([]);
    const [newCluster, setNewCluster] = useState({ name: '', host: '', port: 8108, api_key: '' });
    const [error, setError] = useState('');
    const [success, setSuccess] = useState('');

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

    return (
        <div>
            <h1 className='text-3xl font-bold mb-4'>Cluster Management</h1>
            <div className='grid grid-cols-1 lg:grid-cols-2 gap-8'>
                <div>
                    <h2 className='text-xl font-semibold mb-3'>Registered Clusters</h2>
                    <div className='bg-gray-800/50 border border-gray-700 rounded-lg p-4 space-y-2'>
                        {clusters.length > 0 ? (
                            clusters.map(c => 
                                <div key={c.name} className='p-2 border-b border-gray-700'>
                                    <p className='font-semibold'>{c.name}</p>
                                    <p className='text-xs text-gray-400'>{c.host}:{c.port}</p>
                                </div>
                            )
                        ) : (<p className='text-gray-400'>No clusters registered yet.</p>)}
                    </div>
                </div>
                <div>
                    <h2 className='text-xl font-semibold mb-3'>Register New Cluster</h2>
                    <form onSubmit={handleRegister} className='bg-gray-800/50 border border-gray-700 rounded-lg p-6 space-y-4'>
                        <input type='text' placeholder='Cluster Name (e.g., cluster-us)' value={newCluster.name} onChange={(e) => setNewCluster({...newCluster, name: e.target.value})} className='w-full bg-gray-900 border-gray-600 rounded-md' required />
                        <input type='text' placeholder='Host (e.g., typesense-replica)' value={newCluster.host} onChange={(e) => setNewCluster({...newCluster, host: e.target.value})} className='w-full bg-gray-900 border-gray-600 rounded-md' required />
                        <input type='number' placeholder='Port' value={newCluster.port} onChange={(e) => setNewCluster({...newCluster, port: parseInt(e.target.value)})} className='w-full bg-gray-900 border-gray-600 rounded-md' required />
                        <input type='text' placeholder='API Key' value={newCluster.api_key} onChange={(e) => setNewCluster({...newCluster, api_key: e.target.value})} className='w-full bg-gray-900 border-gray-600 rounded-md' required />
                        <button type='submit' className='w-full bg-blue-600 hover:bg-blue-700 rounded-md py-2 font-semibold'>Register</button>
                        {success && <p className='text-green-400'>{success}</p>}
                        {error && <p className='text-red-400'>{error}</p>}
                    </form>
                </div>
            </div>
        </div>
    );
}
