'use client';

import { useCallback, useEffect, useRef, useState } from 'react';

/**
 * Small notification state helper with deterministic timer cleanup.
 */
export function useNotification(autoHideDelay = 5000) {
  const [notification, setNotification] = useState(null);
  const timerRef = useRef(null);

  const cancelTimer = useCallback(() => {
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  const clearNotification = useCallback(() => {
    cancelTimer();
    setNotification(null);
  }, [cancelTimer]);

  const showNotification = useCallback((type, message) => {
    cancelTimer();
    setNotification({ type, message });

    if (autoHideDelay > 0) {
      timerRef.current = window.setTimeout(() => {
        timerRef.current = null;
        setNotification(null);
      }, autoHideDelay);
    }
  }, [autoHideDelay, cancelTimer]);

  useEffect(() => cancelTimer, [cancelTimer]);

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
 * Notification status region. Errors are assertive; routine updates stay polite.
 */
export function Notification({ type, message, onClose }) {
  const isError = type === 'error';
  const styles = {
    success: 'border-emerald-500/50 bg-emerald-500/10 text-emerald-400',
    error: 'border-destructive/50 bg-destructive/10 text-destructive',
    info: 'border-primary/50 bg-primary/10 text-primary',
  };

  return (
    <div
      className={`flex items-center justify-between rounded-lg border p-3 text-sm ${styles[type] || styles.info}`}
      role={isError ? 'alert' : 'status'}
      aria-live={isError ? 'assertive' : 'polite'}
      aria-atomic="true"
    >
      <span className="min-w-0 break-words">{message}</span>
      {onClose && (
        <button
          type="button"
          onClick={onClose}
          aria-label="Dismiss notification"
          className="ml-4 rounded opacity-70 hover:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <span aria-hidden="true">×</span>
        </button>
      )}
    </div>
  );
}

export default useNotification;
