'use client';
import { useState, useEffect } from 'react';
import { Plus, X, GitBranch, Trash2 } from 'lucide-react';

const API_BASE = '/api';

// Re-using a generic Confirmation Modal
const ConfirmationModal = ({ onConfirm, onCancel, resourceName, resourceType }) => {
    return (
        <div className="fixed inset-0 bg-black bg-opacity-70 flex justify-center items-center z-50">
            <div className="bg-gray-800 rounded-lg p-6 shadow-xl border border-gray-700 w-full max-w-md mx-4">
                <h2 className="text-xl font-bold text-white mb-4">Confirm Deletion</h2>
                <p className="text-gray-300 mb-6">
                    Are you sure you want to delete the {resourceType} for <span className="font-bold text-red-400">{resourceName}</span>? The collection itself will not be deleted.
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
                        Delete Rule
                    </button>
                </div>
            </div>
        </div>
    );
};

export default function RoutingPage() {
    const [collections, setCollections] = useState([]);
    const [clusters, setClusters] = useState([]);
    const [selectedCollection, setSelectedCollection] = useState('');
    const [routingField, setRoutingField] = useState('');
    const [rules, setRules] = useState([{ value: '', cluster: '' }]);
    const [defaultCluster, setDefaultCluster] = useState('');
    const [error, setError] = useState('');
    const [success, setSuccess] = useState('');
    const [currentRules, setCurrentRules] = useState({});
    const [ruleToDelete, setRuleToDelete] = useState(null); // State for deletion modal

    const fetchData = async () => {
        try {
            const res = await fetch(`${API_BASE}/admin/routing-map`);
            if (!res.ok) throw new Error('Failed to fetch data');
            const data = await res.json();
            const collectionsMap = data.collections || {};
            setCollections(Object.keys(collectionsMap));
            setClusters(data.clusters || []);

            const formattedRules = {};
            for (const [col, ruleData] of Object.entries(collectionsMap)) {
                if (ruleData.field) { // Check if a rule is defined
                   formattedRules[col] = ruleData;
                }
            }
            setCurrentRules(formattedRules);

            if (data.clusters.length > 0) {
                if (!defaultCluster) setDefaultCluster(data.clusters[0]);
                if (rules.length === 1 && !rules[0].cluster) setRules([{ value: '', cluster: data.clusters[0] }]);
            }

        } catch (err) { setError(err.message); }
    };

    useEffect(() => { fetchData(); }, []);

    useEffect(() => {
        const existingRule = currentRules[selectedCollection];
        if (existingRule) {
            setRoutingField(existingRule.field);
            setDefaultCluster(existingRule.default_cluster);
            setRules(existingRule.rules.length > 0 ? existingRule.rules : [{ value: '', cluster: clusters[0] || '' }]);
        } else {
            setRoutingField('');
            setDefaultCluster(clusters[0] || '');
            setRules([{ value: '', cluster: clusters[0] || '' }]);
        }
    }, [selectedCollection, currentRules, clusters]);

    const handleAddRule = () => setRules([...rules, { value: '', cluster: clusters[0] || '' }]);
    const handleRemoveRule = (index) => setRules(rules.filter((_, i) => i !== index));

    const handleRuleChange = (index, event) => {
        const newRules = [...rules];
        newRules[index][event.target.name] = event.target.value;
        setRules(newRules);
    };

    const handleSubmit = async (e) => {
        e.preventDefault();
        setError(''); setSuccess('');
        if (!selectedCollection || !routingField || !defaultCluster) {
            setError('Please fill all required fields.');
            return;
        }

        const payload = {
            collection: selectedCollection,
            field: routingField,
            rules: rules.filter(r => r.value && r.cluster),
            default_cluster: defaultCluster,
        };

        try {
            const res = await fetch(`${API_BASE}/admin/routing-rules`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || 'Failed to set routing rule');
            setSuccess(`Routing rule for '${selectedCollection}' set successfully!`);
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
            fetchData(); // Refresh the rules list
        } catch (err) {
            setError(err.message);
        } finally {
            setRuleToDelete(null); // Close the modal
        }
    };

    return (
        <div>
            {ruleToDelete && (
                <ConfirmationModal
                    resourceName={ruleToDelete}
                    resourceType="routing rule"
                    onConfirm={handleDeleteConfirm}
                    onCancel={() => setRuleToDelete(null)}
                />
            )}
            <h1 className='text-3xl font-bold mb-4'>Document Routing Rules</h1>
            <p className="text-gray-400 mb-6 max-w-2xl">Define rules to automatically shard a collection across multiple clusters based on a document field.</p>

            <div className="grid grid-cols-1 lg:grid-cols-2 gap-8">
                <div>
                    <h2 className='text-xl font-semibold mb-3'>Set / Update Rule</h2>
                    <form onSubmit={handleSubmit} className='bg-gray-800/50 border border-gray-700 rounded-lg p-6 space-y-4'>
                        <div>
                            <label className="block text-sm font-medium text-gray-300 mb-1">Collection</label>
                            <select value={selectedCollection} onChange={(e) => setSelectedCollection(e.target.value)} className='w-full bg-gray-900 border-gray-600 rounded-md'>
                                <option value="">Select a Collection</option>
                                {collections.map(c => <option key={c} value={c}>{c}</option>)}
                            </select>
                        </div>
                        <div>
                           <label className="block text-sm font-medium text-gray-300 mb-1">Routing Field</label>
                           <input type='text' placeholder='e.g., country, tenant_id' value={routingField} onChange={(e) => setRoutingField(e.target.value)} className='w-full bg-gray-900 border-gray-600 rounded-md' required />
                        </div>
                        <div>
                           <label className="block text-sm font-medium text-gray-300 mb-1">Specific Rules</label>
                           {rules.map((rule, index) => (
                               <div key={index} className='flex items-center space-x-2 mt-2'>
                                   <input type='text' name='value' placeholder='Field Value (e.g., IT)' value={rule.value} onChange={e => handleRuleChange(index, e)} className='flex-1 bg-gray-900 border-gray-600 rounded-md' />
                                   <span className="text-gray-400">→</span>
                                   <select name='cluster' value={rule.cluster} onChange={e => handleRuleChange(index, e)} className='flex-1 bg-gray-900 border-gray-600 rounded-md'>
                                       {clusters.map(c => <option key={c} value={c}>{c}</option>)}
                                   </select>
                                   <button type='button' onClick={() => handleRemoveRule(index)} className='p-1 text-gray-400 hover:text-red-400'><X size={16}/></button>
                               </div>
                           ))}
                           <button type='button' onClick={handleAddRule} className='mt-3 flex items-center text-sm text-blue-400 hover:text-blue-300'><Plus size={16} className='mr-1'/> Add Rule</button>
                        </div>
                        <div>
                            <label className="block text-sm font-medium text-gray-300 mb-1">Default Cluster (for unmatched values)</label>
                             <select value={defaultCluster} onChange={(e) => setDefaultCluster(e.target.value)} className='w-full bg-gray-900 border-gray-600 rounded-md'>
                                {clusters.map(c => <option key={c} value={c}>{c}</option>)}
                            </select>
                        </div>
                        <button type='submit' className='w-full bg-purple-600 hover:bg-purple-700 rounded-md py-2 font-semibold flex items-center justify-center transition-colors'><GitBranch size={18} className="mr-2"/> Set Routing Rule</button>
                        {success && <p className='text-green-400 text-sm mt-2'>{success}</p>}
                        {error && <p className='text-red-400 text-sm mt-2'>{error}</p>}
                    </form>
                </div>
                <div>
                     <h2 className='text-xl font-semibold mb-3'>Current Routing Configuration</h2>
                     <div className='bg-gray-800/50 border border-gray-700 rounded-lg p-4 space-y-4'>
                        {Object.keys(currentRules).length > 0 ? Object.entries(currentRules).map(([col, rule]) => (
                            <div key={col} className="p-3 bg-gray-900/50 rounded-lg relative">
                               <p className="font-bold text-white">{col}</p>
                               <p className="text-sm text-gray-400 mt-1">Routes on field: <span className="font-mono bg-gray-700 px-1.5 py-0.5 rounded">{rule.field}</span></p>
                               <ul className="mt-2 text-sm space-y-1">
                                   {rule.rules.map((r, i) => <li key={i} className="flex items-center"><span className="text-gray-300 w-1/3 truncate">{r.value}</span> <span className="text-gray-500 mx-2">→</span> <span className="text-blue-400 font-semibold">{r.cluster}</span></li>)}
                                   <li className="mt-2 pt-2 border-t border-gray-700 flex items-center"><span className="text-gray-300 w-1/3">Default</span> <span className="text-gray-500 mx-2">→</span> <span className="text-blue-400 font-semibold">{rule.default_cluster}</span></li>
                               </ul>
                               <button
                                   onClick={() => setRuleToDelete(col)}
                                   className='absolute top-2 right-2 p-2 text-gray-400 hover:text-red-500 hover:bg-red-900/50 rounded-full transition-colors'
                                   title={`Delete routing rule for ${col}`}
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
