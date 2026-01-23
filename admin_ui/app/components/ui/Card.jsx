'use client';

/**
 * Card Component
 * 
 * A reusable container with consistent styling for content sections.
 * Supports different variants and optional header/footer slots.
 * 
 * @param {Object} props
 * @param {React.ReactNode} props.children - Card content
 * @param {string} [props.className] - Additional CSS classes
 * @param {string} [props.title] - Optional card title
 * @param {React.ReactNode} [props.action] - Optional action element in header
 * @param {boolean} [props.noPadding] - Remove default padding
 */
export default function Card({
    children,
    className = '',
    title,
    action,
    noPadding = false
}) {
    return (
        <div className={`
      bg-gray-800/50 
      border border-gray-700 
      rounded-xl 
      backdrop-blur-sm
      ${noPadding ? '' : 'p-6'}
      ${className}
    `}>
            {(title || action) && (
                <div className={`flex justify-between items-center mb-4 ${noPadding ? 'px-6 pt-6' : ''}`}>
                    {title && <h3 className="text-lg font-semibold text-white">{title}</h3>}
                    {action}
                </div>
            )}
            {children}
        </div>
    );
}

/**
 * CardItem Component
 * 
 * A list item within a card with consistent styling.
 */
export function CardItem({ children, className = '' }) {
    return (
        <div className={`
      p-4 
      border-b border-gray-700 
      last:border-b-0 
      hover:bg-gray-700/30 
      transition-colors
      ${className}
    `}>
            {children}
        </div>
    );
}
