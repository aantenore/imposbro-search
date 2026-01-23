'use client';

import { forwardRef } from 'react';

/**
 * Button Component
 * 
 * A flexible button component with multiple variants for different use cases.
 * 
 * Variants:
 * - primary: Blue, for main actions
 * - secondary: Gray, for secondary actions
 * - success: Green, for positive actions like create
 * - danger: Red, for destructive actions
 * - ghost: Transparent, for subtle actions
 * 
 * @param {Object} props
 * @param {React.ReactNode} props.children - Button content
 * @param {string} [props.variant='primary'] - Button style variant
 * @param {string} [props.size='md'] - Button size (sm, md, lg)
 * @param {boolean} [props.fullWidth] - Make button full width
 * @param {boolean} [props.disabled] - Disable the button
 * @param {boolean} [props.loading] - Show loading state
 * @param {React.ReactNode} [props.leftIcon] - Icon to show on the left
 * @param {string} [props.className] - Additional CSS classes
 */
const Button = forwardRef(function Button({
    children,
    variant = 'primary',
    size = 'md',
    fullWidth = false,
    disabled = false,
    loading = false,
    leftIcon,
    className = '',
    type = 'button',
    ...props
}, ref) {
    const baseStyles = `
    inline-flex items-center justify-center
    font-semibold
    rounded-lg
    transition-all duration-200
    focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-offset-gray-900
    disabled:opacity-50 disabled:cursor-not-allowed
  `;

    const variants = {
        primary: 'bg-blue-600 hover:bg-blue-500 text-white focus:ring-blue-500',
        secondary: 'bg-gray-700 hover:bg-gray-600 text-white focus:ring-gray-500',
        success: 'bg-green-600 hover:bg-green-500 text-white focus:ring-green-500',
        danger: 'bg-red-600 hover:bg-red-500 text-white focus:ring-red-500',
        ghost: 'bg-transparent hover:bg-gray-700/50 text-gray-300 focus:ring-gray-500',
        purple: 'bg-purple-600 hover:bg-purple-500 text-white focus:ring-purple-500',
    };

    const sizes = {
        sm: 'px-3 py-1.5 text-sm gap-1.5',
        md: 'px-4 py-2 text-sm gap-2',
        lg: 'px-6 py-3 text-base gap-2',
    };

    return (
        <button
            ref={ref}
            type={type}
            disabled={disabled || loading}
            className={`
        ${baseStyles}
        ${variants[variant]}
        ${sizes[size]}
        ${fullWidth ? 'w-full' : ''}
        ${className}
      `}
            {...props}
        >
            {loading ? (
                <svg className="animate-spin h-4 w-4" viewBox="0 0 24 24">
                    <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" fill="none" />
                    <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                </svg>
            ) : leftIcon}
            {children}
        </button>
    );
});

export default Button;

/**
 * IconButton Component
 * 
 * A circular button for icon-only actions.
 */
export function IconButton({
    children,
    variant = 'ghost',
    size = 'md',
    className = '',
    ...props
}) {
    const sizes = {
        sm: 'p-1.5',
        md: 'p-2',
        lg: 'p-3',
    };

    const variants = {
        ghost: 'text-gray-400 hover:text-white hover:bg-gray-700/50',
        danger: 'text-gray-400 hover:text-red-500 hover:bg-red-900/30',
    };

    return (
        <button
            type="button"
            className={`
        rounded-full
        transition-colors
        focus:outline-none focus:ring-2 focus:ring-gray-500
        ${sizes[size]}
        ${variants[variant]}
        ${className}
      `}
            {...props}
        >
            {children}
        </button>
    );
}
