'use client';

/**
 * EmptyState Component
 * 
 * Placeholder content for empty lists or initial states.
 * 
 * @param {Object} props
 * @param {React.ReactNode} [props.icon] - Optional icon
 * @param {string} props.title - Empty state title
 * @param {string} [props.description] - Optional description
 * @param {React.ReactNode} [props.action] - Optional action button
 */
export default function EmptyState({
    icon,
    title,
    description,
    action
}) {
    return (
        <div className="text-center py-12">
            {icon && (
                <div className="mb-4 flex justify-center text-gray-500">
                    {icon}
                </div>
            )}
            <h3 className="text-lg font-medium text-gray-300 mb-2">
                {title}
            </h3>
            {description && (
                <p className="text-gray-500 mb-6 max-w-sm mx-auto">
                    {description}
                </p>
            )}
            {action}
        </div>
    );
}
