'use client';

/**
 * ConfirmationModal Component
 * 
 * A reusable modal dialog for confirming destructive actions like deletions.
 * Uses a dark theme consistent with the application design.
 * 
 * @param {Object} props
 * @param {Function} props.onConfirm - Called when user confirms the action
 * @param {Function} props.onCancel - Called when user cancels
 * @param {string} props.resourceName - Name of the resource being affected
 * @param {string} props.resourceType - Type of resource (e.g., 'cluster', 'collection')
 * @param {string} [props.title] - Optional custom title
 * @param {string} [props.message] - Optional custom message
 * @param {string} [props.confirmText] - Optional custom confirm button text
 */
export default function ConfirmationModal({
    onConfirm,
    onCancel,
    resourceName,
    resourceType = 'item',
    title = 'Confirm Deletion',
    message,
    confirmText,
}) {
    const defaultMessage = `Are you sure you want to delete the ${resourceType} "${resourceName}"? This action cannot be undone.`;

    return (
        <div
            className="fixed inset-0 bg-black/70 backdrop-blur-sm flex justify-center items-center z-50"
            onClick={onCancel}
            role="dialog"
            aria-modal="true"
            aria-labelledby="modal-title"
        >
            <div
                className="bg-gray-800 rounded-xl p-6 shadow-2xl border border-gray-700 w-full max-w-md mx-4 transform transition-all"
                onClick={e => e.stopPropagation()}
            >
                <h2 id="modal-title" className="text-xl font-bold text-white mb-4">
                    {title}
                </h2>
                <p className="text-gray-300 mb-6">
                    {message || defaultMessage.split(`"${resourceName}"`)[0]}
                    {!message && (
                        <span className="font-bold text-red-400">{resourceName}</span>
                    )}
                    {!message && defaultMessage.split(`"${resourceName}"`)[1]}
                </p>
                <div className="flex justify-end gap-3">
                    <button
                        onClick={onCancel}
                        className="px-4 py-2 rounded-lg bg-gray-700 hover:bg-gray-600 text-white font-medium transition-colors focus:outline-none focus:ring-2 focus:ring-gray-500"
                    >
                        Cancel
                    </button>
                    <button
                        onClick={onConfirm}
                        className="px-4 py-2 rounded-lg bg-red-600 hover:bg-red-500 text-white font-bold transition-colors focus:outline-none focus:ring-2 focus:ring-red-500"
                    >
                        {confirmText || `Delete ${resourceType}`}
                    </button>
                </div>
            </div>
        </div>
    );
}
