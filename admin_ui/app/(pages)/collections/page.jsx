'use client';
import { useState, useEffect } from 'react';
import { Plus, X } from 'lucide-react';
const API_BASE = '/api';

export default function CollectionsPage() {
    const [routingMap, setRoutingMap] = useState({ clusters: [], collections: {} });
    const [newCollection, setNewCollection] = useState({ name: '', cluster_name: 'default', fields: [{ name: '', type: 'string', facet: false }]});
    const [error, setError] = useState('');
    const [success, setSuccess] = useState('');

    const fetchRoutingMap = async () => {
        try {
            const res = await fetch(`${API_BASE}/admin/routing-map`);
            if (!res.ok) throw new Error('Failed to fetch routing map');
            const data = await res.json();
            setRoutingMap(data);
            if (data.clusters && data.clusters.length > 0 && !newCollection.cluster_name) {
                setNewCollection(prev => ({...prev, cluster_name: data.clusters[0]}));
            }
        } catch (err) { setError(err.message); }
    };

    useEffect(() => { fetchRoutingMap(); }, []);

    const handleFieldChange = (index, event) => {
        const values = [...newCollection.fields];
        values[index][event.target.name] = event.target.type === 'checkbox' ? event.target.checked : event.target.value;
        setNewCollection({...newCollection, fields: values});
    };

    const handleAddField = () => setNewCollection({...newCollection, fields: [...newCollection.fields, { name: '', type: 'string', facet: false }]});
    const handleRemoveField = (index) => {
        const values = [...newCollection.fields];
        values.splice(index, 1);
        setNewCollection({...newCollection, fields: values});
    };

    const handleCreate = async (e) => {
        e.preventDefault();
        setError(''); setSuccess('');
        try {
            const schema = { ...newCollection, fields: newCollection.fields.filter(f => f.name) };
            if (!schema.name || !schema.cluster_name) { setError('Collection Name and Cluster are required.'); return; }
            if (schema.fields.length === 0) { setError('At least one field is required.'); return; }
            
            const res = await fetch(`${API_BASE}/admin/collections`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(schema),
            });
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || 'Failed to create collection');
            setSuccess(`Collection '${schema.name}' created successfully!`);
            fetchRoutingMap();
        } catch (err) { setError(err.message); }
    };

    return (
        <div>
            <h1 className='text-3xl font-bold mb-4'>Collection Management</h1>
            <p className="text-gray-400 mb-6 max-w-2xl">Create collection schemas. A collection will be created on ALL clusters to ensure routing and search can function correctly, but you only need to specify a primary cluster here.</p>
            <div className='grid grid-cols-1 lg:grid-cols-2 gap-8'>
                <div>
                    <h2 className='text-xl font-semibold mb-3'>Existing Collections</h2>
                    <div className='bg-gray-800/50 border border-gray-700 rounded-lg p-4'>
                        {Object.keys(routingMap.collections).length > 0 ? (
                            <ul className='divide-y divide-gray-700'>{Object.entries(routingMap.collections).map(([col, rule]) => (
                                <li key={col} className='p-2 flex justify-between items-center'>
                                  <span>{col}</span>
                                  <span className='text-xs text-gray-400 bg-gray-700 px-2 py-1 rounded-full'>
                                    {rule.field ? `Sharded by "${rule.field}"` : 'Not Sharded'}
                                  </span>
                                </li>
                            ))}</ul>
                        ) : (<p className='text-gray-400'>No collections created yet.</p>)}
                    </div>
                </div>
                <div>
                    <h2 className='text-xl font-semibold mb-3'>Create New Collection</h2>
                    <form onSubmit={handleCreate} className='bg-gray-800/50 border border-gray-700 rounded-lg p-6 space-y-4'>
                        <input type='text' placeholder='Collection Name' value={newCollection.name} onChange={(e) => setNewCollection({...newCollection, name: e.target.value})} className='w-full bg-gray-900 border-gray-600 rounded-md' required />
                        <div>
                            <label className='text-sm text-gray-400'>Schema Fields</label>
                            {newCollection.fields.map((field, index) => (
                                <div key={index} className='flex items-center space-x-2 mt-2'>
                                    <input type='text' name='name' placeholder='Field Name' value={field.name} onChange={e => handleFieldChange(index, e)} className='flex-1 bg-gray-900 border-gray-600 rounded-md' />
                                    <select name='type' value={field.type} onChange={e => handleFieldChange(index, e)} className='bg-gray-900 border-gray-600 rounded-md'>
                                        <option value='string'>string</option><option value='int32'>int32</option><option value='float'>float</option><option value='bool'>bool</option><option value='string[]'>string[]</option>
                                    </select>
                                    <label className='flex items-center text-sm'><input type='checkbox' name='facet' checked={field.facet} onChange={e => handleFieldChange(index, e)} className='mr-1 rounded bg-gray-700 border-gray-500' />Facet</label>
                                    <button type='button' onClick={() => handleRemoveField(index)} className='p-1 text-red-400 hover:text-red-300'><X size={16}/></button>
                                </div>
                            ))}
                            <button type='button' onClick={handleAddField} className='mt-2 flex items-center text-sm text-blue-400 hover:text-blue-300'><Plus size={16} className='mr-1'/> Add Field</button>
                        </div>
                        <button type='submit' className='w-full bg-green-600 hover:bg-green-700 rounded-md py-2 font-semibold'>Create Collection</button>
                        {success && <p className='text-green-400'>{success}</p>}
                        {error && <p className='text-red-400'>{error}</p>}
                    </form>
                </div>
            </div>
        </div>
    );
}
