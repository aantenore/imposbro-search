'use client';

/**
 * PageHeader Component
 * 
 * Consistent page header with title and optional description.
 * 
 * @param {Object} props
 * @param {string} props.title - Page title
 * @param {string} [props.description] - Optional description text
 * @param {React.ReactNode} [props.action] - Optional action button/element
 */
export default function PageHeader({ title, description, action }) {
    return (
        <div className="mb-8">
            <div className="flex justify-between items-start">
                <div>
                    <h1 className="text-3xl font-bold text-white tracking-tight">
                        {title}
                    </h1>
                    {description && (
                        <p className="mt-2 text-gray-400 max-w-2xl">
                            {description}
                        </p>
                    )}
                </div>
                {action && <div>{action}</div>}
            </div>
        </div>
    );
}
