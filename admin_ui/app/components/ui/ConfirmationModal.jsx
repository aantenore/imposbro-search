'use client';

import { useEffect, useId, useRef } from 'react';
import { cn } from '../../lib/utils';
import Button from './Button';

const FOCUSABLE_ELEMENTS = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',');

export default function ConfirmationModal({
  onConfirm,
  onCancel,
  resourceName,
  resourceType = 'item',
  title = 'Confirm Deletion',
  message,
  confirmText,
}) {
  const generatedId = useId().replace(/:/g, '');
  const titleId = `confirmation-title-${generatedId}`;
  const descriptionId = `confirmation-description-${generatedId}`;
  const dialogRef = useRef(null);
  const cancelButtonRef = useRef(null);
  const onCancelRef = useRef(onCancel);

  useEffect(() => {
    onCancelRef.current = onCancel;
  }, [onCancel]);

  useEffect(() => {
    const previouslyFocused = document.activeElement;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';

    const focusFrame = window.requestAnimationFrame(() => {
      cancelButtonRef.current?.focus();
    });

    return () => {
      window.cancelAnimationFrame(focusFrame);
      document.body.style.overflow = previousOverflow;
      if (previouslyFocused instanceof HTMLElement && previouslyFocused.isConnected) {
        previouslyFocused.focus();
      }
    };
  }, []);

  const defaultMessage = `Are you sure you want to delete the ${resourceType} "${resourceName}"? This action cannot be undone.`;
  const displayMessage = message || defaultMessage;

  const handleDialogKeyDown = (event) => {
    if (event.key === 'Escape') {
      event.preventDefault();
      onCancelRef.current?.();
      return;
    }

    if (event.key !== 'Tab') return;
    const focusableElements = Array.from(
      dialogRef.current?.querySelectorAll(FOCUSABLE_ELEMENTS) || []
    ).filter((element) => element instanceof HTMLElement && !element.hidden);

    if (focusableElements.length === 0) {
      event.preventDefault();
      dialogRef.current?.focus();
      return;
    }

    const firstElement = focusableElements[0];
    const lastElement = focusableElements[focusableElements.length - 1];
    if (event.shiftKey && document.activeElement === firstElement) {
      event.preventDefault();
      lastElement.focus();
    } else if (!event.shiftKey && document.activeElement === lastElement) {
      event.preventDefault();
      firstElement.focus();
    }
  };

  const handleBackdropClick = (event) => {
    if (event.target === event.currentTarget) onCancelRef.current?.();
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center overflow-y-auto overscroll-contain bg-black/80 p-4 backdrop-blur-sm"
      onClick={handleBackdropClick}
    >
      <div
        ref={dialogRef}
        className={cn(
          'relative w-full max-w-lg rounded-lg border border-border bg-card p-6 text-card-foreground shadow-lg',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background'
        )}
        role="alertdialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={descriptionId}
        tabIndex={-1}
        onKeyDown={handleDialogKeyDown}
      >
        <h2 id={titleId} className="text-lg font-semibold leading-none tracking-tight text-balance">
          {title}
        </h2>
        <p id={descriptionId} className="mt-4 text-sm text-muted-foreground">
          {displayMessage}
        </p>
        <div className="mt-6 flex justify-end gap-3">
          <Button ref={cancelButtonRef} type="button" variant="outline" onClick={onCancel}>
            Cancel
          </Button>
          <Button type="button" variant="destructive" onClick={onConfirm}>
            {confirmText || `Delete ${resourceType}`}
          </Button>
        </div>
      </div>
    </div>
  );
}
