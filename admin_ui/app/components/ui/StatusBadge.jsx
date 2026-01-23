'use client';

/**
 * StatusBadge Component
 * 
 * Visual indicator for status with different color variants.
 * 
 * @param {Object} props
 * @param {string} props.children - Badge text
 * @param {string} [props.variant='default'] - Color variant
 */
export default function StatusBadge({
    children,
    variant = 'default',
    className = ''
}) {
    const variants = {
        default: 'bg-gray-600 text-gray-200',
        success: 'bg-green-600/50 text-green-300',
        warning: 'bg-yellow-600/50 text-yellow-300',
        error: 'bg-red-600/50 text-red-300',
        info: 'bg-blue-600/50 text-blue-300',
        purple: 'bg-purple-600/50 text-purple-300',
    };

    return (
        <span className={`
      inline-flex items-center
      px-2.5 py-0.5
      text-xs font-medium
      rounded-full
      ${variants[variant]}
      ${className}
    `}>
            {children}
        </span>
    );
}
