'use client';

import { useState, useCallback } from 'react';

/**
 * useNotification Hook
 * 
 * Provides a simple notification/toast system for user feedback.
 * Returns both the notification state and methods to trigger notifications.
 * 
 * Usage:
 * const { notification, showSuccess, showError, clearNotification } = useNotification();
 * 
 * // In response to an action
 * showSuccess('Item created successfully!');
 * showError('Failed to create item');
 * 
 * // In render
 * {notification && <Notification {...notification} />}
 */
export function useNotification(autoHideDelay = 5000) {
    const [notification, setNotification] = useState(null);

    const clearNotification = useCallback(() => {
        setNotification(null);
    }, []);

    const showNotification = useCallback((type, message) => {
        setNotification({ type, message });

        if (autoHideDelay > 0) {
            setTimeout(clearNotification, autoHideDelay);
        }
    }, [autoHideDelay, clearNotification]);

    const showSuccess = useCallback((message) => {
        showNotification('success', message);
    }, [showNotification]);

    const showError = useCallback((message) => {
        showNotification('error', message);
    }, [showNotification]);

    const showInfo = useCallback((message) => {
        showNotification('info', message);
    }, [showNotification]);

    return {
        notification,
        showSuccess,
        showError,
        showInfo,
        clearNotification,
    };
}

/**
 * Notification Component
 * 
 * Renders a notification message with appropriate styling.
 */
export function Notification({ type, message, onClose }) {
    const styles = {
        success: 'bg-green-900/50 border-green-700 text-green-300',
        error: 'bg-red-900/50 border-red-700 text-red-300',
        info: 'bg-blue-900/50 border-blue-700 text-blue-300',
    };

    return (
        <div className={`
      p-3 rounded-lg border
      flex items-center justify-between
      ${styles[type]}
    `}>
            <span className="text-sm">{message}</span>
            {onClose && (
                <button
                    onClick={onClose}
                    className="ml-4 text-current opacity-70 hover:opacity-100"
                >
                    ×
                </button>
            )}
        </div>
    );
}

export default useNotification;
