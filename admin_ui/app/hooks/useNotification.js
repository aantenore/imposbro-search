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
 * Notification Component – shadcn-style alert.
 */
export function Notification({ type, message, onClose }) {
  const styles = {
    success: 'border-emerald-500/50 bg-emerald-500/10 text-emerald-400',
    error: 'border-destructive/50 bg-destructive/10 text-destructive',
    info: 'border-primary/50 bg-primary/10 text-primary',
  };

  return (
    <div
      className={`flex items-center justify-between rounded-lg border p-3 text-sm ${styles[type] || styles.info}`}
    >
      <span>{message}</span>
      {onClose && (
        <button
          type="button"
          onClick={onClose}
          className="ml-4 opacity-70 hover:opacity-100 focus:outline-none focus:ring-2 focus:ring-ring rounded"
        >
          ×
        </button>
      )}
    </div>
  );
}

export default useNotification;
