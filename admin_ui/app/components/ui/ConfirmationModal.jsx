'use client';

import { cn } from '../../lib/utils';
import Button from './Button';

export default function ConfirmationModal({
  onConfirm,
  onCancel,
  resourceName,
  resourceType = 'item',
  title = 'Confirm Deletion',
  message,
  confirmText,
}) {
  const defaultMessage = `Are you sure you want to delete the ${resourceType} "${resourceName}"? This action cannot be undone.`;
  const displayMessage = message || defaultMessage;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 backdrop-blur-sm"
      onClick={onCancel}
      role="dialog"
      aria-modal="true"
      aria-labelledby="modal-title"
    >
      <div
        className={cn(
          'relative mx-4 w-full max-w-lg rounded-lg border border-border bg-card p-6 text-card-foreground shadow-lg',
          'focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2 focus:ring-offset-background'
        )}
        onClick={(e) => e.stopPropagation()}
      >
        <h2
          id="modal-title"
          className="text-lg font-semibold leading-none tracking-tight"
        >
          {title}
        </h2>
        <p className="mt-4 text-sm text-muted-foreground">
          {message ? (
            message
          ) : (
            <>
              {defaultMessage.split(`"${resourceName}"`)[0]}
              <span className="font-medium text-destructive">{resourceName}</span>
              {defaultMessage.split(`"${resourceName}"`)[1]}
            </>
          )}
        </p>
        <div className="mt-6 flex justify-end gap-3">
          <Button variant="outline" onClick={onCancel}>
            Cancel
          </Button>
          <Button
            variant="destructive"
            onClick={onConfirm}
          >
            {confirmText || `Delete ${resourceType}`}
          </Button>
        </div>
      </div>
    </div>
  );
}
