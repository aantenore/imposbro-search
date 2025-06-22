'use client';
import { useState, useEffect } from 'react';
import { Plus, X, GitBranch, Trash2 } from 'lucide-react';

const API_BASE = '/api';

const ConfirmationModal = ({ onConfirm, onCancel, resourceName, resourceType }) => {
    return (
        <div className="fixed inset-0 bg-black bg-opacity-70 flex justify-center items-center z-50">
            <div className="bg-gray-800 rounded-lg p-6 shadow-xl border border-gray-700 w-full max-w-md mx-4">
                <h2 className="text-xl font-bold text-white mb-4">Confirm Deletion</h2>
                <p className="text-gray-300 mb-6">
                    Are you sure you want to delete the {resourceType} for <span className="font-bold text-red-400">{resourceName}</span>? The collection itself will not be deleted.
                </p>
                <div className="flex justify-end space-x-4">
                    <button onClick={onCancel} className="px-4 py-2 rounded-md bg-gray-600 hover:bg-gray-500 text-white font-semibold">Cancel</button>
                    <button onClick={onConfirm} className="px-4 py-2 rounded-md bg-red-600 hover:bg-red-700 text-white font-bold">Delete Rule</button>
                </div>
            </div>
        </div>
    );
};

export default function RoutingPage() {
    const [collections, setCollections] = useState([]);
    const [clusters, setClusters] = useState([]);
    const [selectedCollection, setSelectedCollection] = useState('');
    const [availableFields, setAvailableFields] = useState([]);

    const [rules, setRules] = useState([]);
    const [defaultCluster, setDefaultCluster] = useState('');

    const [error, setError] = useState('');
    const [success, setSuccess] = useState('');
    const [currentRules, setCurrentRules] = useState({});
    const [ruleToDelete, setRuleToDelete] = useState(null);

    const fetchData = async () => {
        try {
            const res = await fetch(`${API_BASE}/admin/routing-map`);
            if (!res.ok) throw new Error('Failed to fetch data');
            const data = await res.json();
            setCollections(Object.keys(data.collections || {}));
            setClusters(data.clusters || []);
            setCurrentRules(data.collections || {});
            if (data.clusters && data.clusters.length > 0) {
                setDefaultCluster(data.clusters[0]);
            }
        } catch (err) { setError(err.message); }
    };

    useEffect(() => { fetchData(); }, []);

    useEffect(() => {
        if (!selectedCollection) {
            setAvailableFields([]);
            return;
        }
        const fetchSchema = async () => {
            try {
                const res = await fetch(`${API_BASE}/admin/collections/${selectedCollection}`);
                if (!res.ok) {
                    setAvailableFields([]);
                    throw new Error('Failed to fetch collection schema');
                }
                const schemaData = await res.json();
                setAvailableFields(schemaData.fields || []);
            } catch (err) {
                setError(err.message);
            }
        };
        fetchSchema();

        const existingRuleSet = currentRules[selectedCollection];
        if (existingRuleSet && existingRuleSet.rules) {
            setRules(existingRuleSet.rules);
            setDefaultCluster(existingRuleSet.default_cluster);
        } else {
            setRules([]);
            setDefaultCluster(clusters[0] || '');
        }
    }, [selectedCollection, clusters, currentRules]);

    const handleAddRule = () => {
        if (availableFields.length === 0) return;
        setRules([...rules, { field: availableFields[0].name, value: '', cluster: clusters[0] || '' }]);
    };

    const handleRemoveRule = (index) => setRules(rules.filter((_, i) => i !== index));

    const handleRuleChange = (index, event) => {
        const newRules = [...rules];
        newRules[index][event.target.name] = event.target.value;
        setRules(newRules);
    };

    const handleSubmit = async (e) => {
        e.preventDefault();
        setError(''); setSuccess('');
        if (!selectedCollection || !defaultCluster) {
            setError('Please select a collection and a default cluster.');
            return;
        }

        const payload = {
            collection: selectedCollection,
            rules: rules.filter(r => r.field && r.value && r.cluster),
            default_cluster: defaultCluster,
        };

        try {
            const res = await fetch(`${API_BASE}/admin/routing-rules`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || 'Failed to set routing rules');
            setSuccess(`Routing rules for '${selectedCollection}' set successfully!`);
            fetchData();
        } catch (err) { setError(err.message); }
    };

    const handleDeleteConfirm = async () => {
        if (!ruleToDelete) return;
        setError(''); setSuccess('');
        try {
            const res = await fetch(`${API_BASE}/admin/routing-rules/${ruleToDelete}`, {
                method: 'DELETE',
            });
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || 'Failed to delete routing rule');
            setSuccess(`Routing rule for '${ruleToDelete}' deleted successfully!`);
            fetchData();
        } catch (err) {
            setError(err.message);
        } finally {
            setRuleToDelete(null);
        }
    };

    return (
        <div>
            {ruleToDelete && ( <ConfirmationModal
                resourceName={ruleToDelete}
                resourceType="routing rule"
                onConfirm={handleDeleteConfirm}
                onCancel={() => setRuleToDelete(null)}
            /> )}
            <h1 className='text-3xl font-bold mb-4'>Document Routing Rules</h1>
            <p className="text-gray-400 mb-6 max-w-2xl">Define a set of rules to shard a collection. The first rule that matches a document determines its destination.</p>

            <div className="grid grid-cols-1 lg:grid-cols-2 gap-8">
                <div>
                    <h2 className='text-xl font-semibold mb-3'>Set / Update Rules</h2>
                    <form onSubmit={handleSubmit} className='bg-gray-800/50 border border-gray-700 rounded-lg p-6 space-y-4'>
                        <div>
                            <label className="block text-sm font-medium text-gray-300 mb-1">1. Select Collection</label>
                            <select value={selectedCollection} onChange={(e) => setSelectedCollection(e.target.value)} className='w-full bg-gray-900 border-gray-600 rounded-md'>
                                <option value="">-- Select a Collection --</option>
                                {collections.map(c => <option key={c} value={c}>{c}</option>)}
                            </select>
                        </div>

                        {selectedCollection && (
                            <>
                                <div>
                                   <label className="block text-sm font-medium text-gray-300 mb-1">2. Define Specific Rules</label>
                                   {rules.map((rule, index) => (
                                       <div key={index} className='grid grid-cols-12 gap-2 items-center mt-2 p-2 bg-gray-900/50 rounded-md'>
                                            <select name='field' value={rule.field} onChange={e => handleRuleChange(index, e)} className='col-span-4 bg-gray-800 border-gray-600 rounded-md text-sm'>
                                                <option value="">-- Field --</option>
                                                {availableFields.map(f => <option key={f.name} value={f.name}>{f.name}</option>)}
                                            </select>
                                            <input type='text' name='value' placeholder='Value (e.g., IT)' value={rule.value} onChange={e => handleRuleChange(index, e)} className='col-span-4 bg-gray-800 border-gray-600 rounded-md text-sm' />
                                            <select name='cluster' value={rule.cluster} onChange={e => handleRuleChange(index, e)} className='col-span-3 bg-gray-800 border-gray-600 rounded-md text-sm'>
                                                {clusters.map(c => <option key={c} value={c}>{c}</option>)}
                                            </select>
                                           <button type='button' onClick={() => handleRemoveRule(index)} className='col-span-1 p-1 text-gray-400 hover:text-red-400 justify-self-center'><X size={16}/></button>
                                       </div>
                                   ))}
                                   <button type='button' onClick={handleAddRule} className='mt-3 flex items-center text-sm text-blue-400 hover:text-blue-300'><Plus size={16} className='mr-1'/> Add Rule</button>
                                </div>
                                <div>
                                    <label className="block text-sm font-medium text-gray-300 mb-1">3. Set Default Cluster</label>
                                     <select value={defaultCluster} onChange={(e) => setDefaultCluster(e.target.value)} className='w-full bg-gray-900 border-gray-600 rounded-md'>
                                        {clusters.map(c => <option key={c} value={c}>{c}</option>)}
                                    </select>
                                </div>
                                <button type='submit' className='w-full bg-purple-600 hover:bg-purple-700 rounded-md py-2 font-semibold flex items-center justify-center transition-colors'><GitBranch size={18} className="mr-2"/> Save Routing Rules</button>
                            </>
                        )}

                        {success && <p className='text-green-400 text-sm mt-2'>{success}</p>}
                        {error && <p className='text-red-400 text-sm mt-2'>{error}</p>}
                    </form>
                </div>
                <div>
                     <h2 className='text-xl font-semibold mb-3'>Current Routing Configuration</h2>
                     <div className='bg-gray-800/50 border border-gray-700 rounded-lg p-4 space-y-4'>
                        {Object.values(currentRules).filter(rule => rule.rules && rule.rules.length > 0).length > 0 ? Object.values(currentRules).filter(rule => rule.rules && rule.rules.length > 0).map((rule) => (
                            <div key={rule.collection} className="p-3 bg-gray-900/50 rounded-lg relative">
                               <p className="font-bold text-white">{rule.collection}</p>
                               <ul className="mt-2 text-sm space-y-1">
                                   {rule.rules.map((r, i) => <li key={i} className="flex items-center"><span className="font-mono bg-gray-700 px-1.5 py-0.5 rounded w-1/3 truncate">{r.field}: {r.value}</span> <span className="text-gray-500 mx-2">→</span> <span className="text-blue-400 font-semibold">{r.cluster}</span></li>)}
                                   <li className="mt-2 pt-2 border-t border-gray-700 flex items-center"><span className="text-gray-300 w-1/3">Default</span> <span className="text-gray-500 mx-2">→</span> <span className="text-blue-400 font-semibold">{rule.default_cluster}</span></li>
                               </ul>
                               <button
                                   onClick={() => setRuleToDelete(rule.collection)}
                                   className='absolute top-2 right-2 p-2 text-gray-400 hover:text-red-500 hover:bg-red-900/50 rounded-full transition-colors'
                                   title={`Delete routing rule for ${rule.collection}`}
                               >
                                   <Trash2 size={16}/>
                               </button>
                            </div>
                        )) : (<p className='text-gray-400 p-3'>No routing rules configured yet.</p>)}
                     </div>
                </div>
            </div>
        </div>
    );
}
