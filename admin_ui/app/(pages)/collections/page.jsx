'use client';
import { useState, useEffect } from 'react';
import { Plus, X, Trash2 } from 'lucide-react';

const API_BASE = '/api';

const ConfirmationModal = ({ onConfirm, onCancel, resourceName, resourceType }) => {
    return (
        <div className="fixed inset-0 bg-black bg-opacity-70 flex justify-center items-center z-50">
            <div className="bg-gray-800 rounded-lg p-6 shadow-xl border border-gray-700 w-full max-w-md mx-4">
                <h2 className="text-xl font-bold text-white mb-4">Confirm Deletion</h2>
                <p className="text-gray-300 mb-6">
                    Are you sure you want to delete the {resourceType} <span className="font-bold text-red-400">{resourceName}</span>? This action cannot be undone.
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
                        Delete {resourceType}
                    </button>
                </div>
            </div>
        </div>
    );
};

export default function CollectionsPage() {
    const [routingMap, setRoutingMap] = useState({ clusters: [], collections: {} });
    const [newCollection, setNewCollection] = useState({ name: '', fields: [{ name: '', type: 'string', facet: false }]});
    const [error, setError] = useState('');
    const [success, setSuccess] = useState('');
    const [collectionToDelete, setCollectionToDelete] = useState(null);

    const fetchRoutingMap = async () => {
        try {
            const res = await fetch(`${API_BASE}/admin/routing-map`);
            if (!res.ok) throw new Error('Failed to fetch routing map');
            const data = await res.json();
            setRoutingMap(data);
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
            if (!schema.name) { setError('Collection Name is required.'); return; }
            if (schema.fields.length === 0) { setError('At least one field is required.'); return; }

            const res = await fetch(`${API_BASE}/admin/collections`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(schema),
            });
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || 'Failed to create collection');
            setSuccess(`Collection '${schema.name}' created successfully on all clusters!`);
            fetchRoutingMap();
        } catch (err) { setError(err.message); }
    };

    const handleDeleteConfirm = async () => {
        if (!collectionToDelete) return;
        setError(''); setSuccess('');
        try {
            const res = await fetch(`${API_BASE}/admin/collections/${collectionToDelete}`, {
                method: 'DELETE',
            });
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || 'Failed to delete collection');
            setSuccess(`Collection '${collectionToDelete}' deleted successfully!`);
            fetchRoutingMap();
        } catch (err) {
            setError(err.message);
        } finally {
            setCollectionToDelete(null);
        }
    };

    return (
        <div>
            {collectionToDelete && (
                <ConfirmationModal
                    resourceName={collectionToDelete}
                    resourceType="collection"
                    onConfirm={handleDeleteConfirm}
                    onCancel={() => setCollectionToDelete(null)}
                />
            )}
            <h1 className='text-3xl font-bold mb-4'>Collection Management</h1>
            <p className="text-gray-400 mb-6 max-w-2xl">Create or delete collection schemas. Actions performed here will affect all clusters.</p>
            <div className='grid grid-cols-1 lg:grid-cols-2 gap-8'>
                <div>
                    <h2 className='text-xl font-semibold mb-3'>Existing Collections</h2>
                    <div className='bg-gray-800/50 border border-gray-700 rounded-lg p-4'>
                        {Object.keys(routingMap.collections).length > 0 ? (
                            <ul className='divide-y divide-gray-700'>{Object.entries(routingMap.collections).map(([col, rule]) => (
                                <li key={col} className='p-3 flex justify-between items-center'>
                                  <div className="flex flex-col">
                                    <span className="text-white font-semibold">{col}</span>
                                    <span className={`mt-1 text-xs px-2 py-0.5 rounded-full w-fit ${rule.rules && rule.rules.length > 0 ? 'bg-purple-600/50 text-purple-300' : 'bg-gray-600 text-gray-300'}`}>
                                        {rule.rules && rule.rules.length > 0 ? `Sharded` : 'Not Sharded'}
                                    </span>
                                  </div>
                                  <button
                                      onClick={() => setCollectionToDelete(col)}
                                      className='p-2 text-gray-400 hover:text-red-500 hover:bg-red-900/50 rounded-full transition-colors'
                                      title={`Delete collection ${col}`}
                                  >
                                      <Trash2 size={16}/>
                                  </button>
                                </li>
                            ))}</ul>
                        ) : (<p className='text-gray-400 p-3'>No collections created yet.</p>)}
                    </div>
                </div>
                <div>
                    <h2 className='text-xl font-semibold mb-3'>Create New Collection</h2>
                    <form onSubmit={handleCreate} className='bg-gray-800/50 border border-gray-700 rounded-lg p-6 space-y-4'>
                        <input type='text' placeholder='Collection Name' value={newCollection.name} onChange={(e) => setNewCollection({...newCollection, name: e.target.value})} className='w-full bg-gray-900 border-gray-600 rounded-md' required />
                        <div>
                            <label className='text-sm text-gray-400 mb-2 block'>Schema Fields</label>
                            {newCollection.fields.map((field, index) => (
                                <div key={index} className='flex items-center space-x-2 mt-2'>
                                    <input type='text' name='name' placeholder='Field Name' value={field.name} onChange={e => handleFieldChange(index, e)} className='flex-1 bg-gray-900 border-gray-600 rounded-md text-sm' />
                                    <select name='type' value={field.type} onChange={e => handleFieldChange(index, e)} className='bg-gray-900 border-gray-600 rounded-md text-sm'>
                                        <option value='string'>string</option><option value='int32'>int32</option><option value='float'>float</option><option value='bool'>bool</option><option value='string[]'>string[]</option>
                                    </select>
                                    <label className='flex items-center text-sm text-gray-300'><input type='checkbox' name='facet' checked={field.facet} onChange={e => handleFieldChange(index, e)} className='mr-1 rounded bg-gray-700 border-gray-500 text-blue-400 focus:ring-blue-500' />Facet</label>
                                    <button type='button' onClick={() => handleRemoveField(index)} className='p-1 text-gray-400 hover:text-red-400'><X size={16}/></button>
                                </div>
                            ))}
                            <button type='button' onClick={handleAddField} className='mt-3 flex items-center text-sm text-blue-400 hover:text-blue-300'><Plus size={16} className='mr-1'/> Add Field</button>
                        </div>
                        <button type='submit' className='w-full bg-green-600 hover:bg-green-700 rounded-md py-2 font-semibold transition-colors'>Create Collection</button>
                        {success && <p className='text-green-400 text-sm mt-2'>{success}</p>}
                        {error && <p className='text-red-400 text-sm mt-2'>{error}</p>}
                    </form>
                </div>
            </div>
        </div>
    );
}
