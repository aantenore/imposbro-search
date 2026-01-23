'use client';

import { forwardRef } from 'react';

/**
 * Input Component
 * 
 * A styled text input with consistent appearance.
 * 
 * @param {Object} props
 * @param {string} [props.label] - Optional label text
 * @param {string} [props.error] - Error message to display
 * @param {string} [props.className] - Additional CSS classes
 */
const Input = forwardRef(function Input({
    label,
    error,
    className = '',
    ...props
}, ref) {
    return (
        <div className="w-full">
            {label && (
                <label className="block text-sm font-medium text-gray-300 mb-1.5">
                    {label}
                </label>
            )}
            <input
                ref={ref}
                className={`
          w-full
          px-3 py-2
          bg-gray-900
          border border-gray-600
          rounded-lg
          text-white
          placeholder-gray-500
          transition-colors
          focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent
          disabled:opacity-50 disabled:cursor-not-allowed
          ${error ? 'border-red-500 focus:ring-red-500' : ''}
          ${className}
        `}
                {...props}
            />
            {error && (
                <p className="mt-1 text-sm text-red-400">{error}</p>
            )}
        </div>
    );
});

export default Input;

/**
 * Select Component
 * 
 * A styled select dropdown with consistent appearance.
 */
export const Select = forwardRef(function Select({
    label,
    error,
    children,
    className = '',
    ...props
}, ref) {
    return (
        <div className="w-full">
            {label && (
                <label className="block text-sm font-medium text-gray-300 mb-1.5">
                    {label}
                </label>
            )}
            <select
                ref={ref}
                className={`
          w-full
          px-3 py-2
          bg-gray-900
          border border-gray-600
          rounded-lg
          text-white
          transition-colors
          focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent
          disabled:opacity-50 disabled:cursor-not-allowed
          ${error ? 'border-red-500 focus:ring-red-500' : ''}
          ${className}
        `}
                {...props}
            >
                {children}
            </select>
            {error && (
                <p className="mt-1 text-sm text-red-400">{error}</p>
            )}
        </div>
    );
});

/**
 * Checkbox Component
 * 
 * A styled checkbox with label.
 */
export const Checkbox = forwardRef(function Checkbox({
    label,
    className = '',
    ...props
}, ref) {
    return (
        <label className={`inline-flex items-center cursor-pointer ${className}`}>
            <input
                ref={ref}
                type="checkbox"
                className="
          w-4 h-4
          rounded
          bg-gray-700
          border-gray-500
          text-blue-500
          focus:ring-blue-500 focus:ring-offset-gray-900
          transition-colors
        "
                {...props}
            />
            {label && (
                <span className="ml-2 text-sm text-gray-300">{label}</span>
            )}
        </label>
    );
});
