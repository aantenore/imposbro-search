'use client';

import { forwardRef, useId } from 'react';
import { cn } from '../../lib/utils';

function normalizedControlId(prefix, suppliedId, generatedId) {
  return suppliedId || `${prefix}-${generatedId.replace(/:/g, '')}`;
}

function fallbackAccessibleName(label, ariaLabel, name, placeholder) {
  if (label || ariaLabel) return ariaLabel;
  if (name) {
    return String(name)
      .replace(/[_-]+/g, ' ')
      .replace(/\b\w/g, (character) => character.toUpperCase());
  }
  return placeholder ? String(placeholder) : undefined;
}

function describedByValue(existingValue, errorId) {
  return [existingValue, errorId].filter(Boolean).join(' ') || undefined;
}

function FieldError({ id, children }) {
  if (!children) return null;
  return (
    <p id={id} className="mt-1.5 text-sm text-destructive" role="alert">
      {children}
    </p>
  );
}

const Input = forwardRef(function Input(
  {
    className,
    type,
    label,
    error,
    id: suppliedId,
    name,
    placeholder,
    'aria-label': ariaLabel,
    'aria-describedby': ariaDescribedBy,
    'aria-invalid': ariaInvalid,
    ...props
  },
  ref
) {
  const generatedId = useId();
  const controlId = normalizedControlId('input', suppliedId, generatedId);
  const errorId = error ? `${controlId}-error` : undefined;
  const accessibleName = fallbackAccessibleName(label, ariaLabel, name, placeholder);

  return (
    <div className="w-full">
      {label && (
        <label htmlFor={controlId} className="mb-1.5 block text-sm font-medium text-foreground">
          {label}
        </label>
      )}
      <input
        id={controlId}
        name={name}
        type={type}
        placeholder={placeholder}
        aria-label={accessibleName}
        aria-describedby={describedByValue(ariaDescribedBy, errorId)}
        aria-invalid={error ? true : ariaInvalid}
        className={cn(
          'flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background file:border-0 file:bg-transparent file:text-sm file:font-medium placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50',
          error && 'border-destructive focus-visible:ring-destructive',
          className
        )}
        ref={ref}
        {...props}
      />
      <FieldError id={errorId}>{error}</FieldError>
    </div>
  );
});

export default Input;

export const Select = forwardRef(function Select(
  {
    className,
    label,
    error,
    children,
    id: suppliedId,
    name,
    'aria-label': ariaLabel,
    'aria-describedby': ariaDescribedBy,
    'aria-invalid': ariaInvalid,
    ...props
  },
  ref
) {
  const generatedId = useId();
  const controlId = normalizedControlId('select', suppliedId, generatedId);
  const errorId = error ? `${controlId}-error` : undefined;
  const accessibleName = fallbackAccessibleName(label, ariaLabel, name);

  return (
    <div className="w-full">
      {label && (
        <label htmlFor={controlId} className="mb-1.5 block text-sm font-medium text-foreground">
          {label}
        </label>
      )}
      <select
        id={controlId}
        name={name}
        aria-label={accessibleName}
        aria-describedby={describedByValue(ariaDescribedBy, errorId)}
        aria-invalid={error ? true : ariaInvalid}
        className={cn(
          'flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50',
          error && 'border-destructive focus-visible:ring-destructive',
          className
        )}
        ref={ref}
        {...props}
      >
        {children}
      </select>
      <FieldError id={errorId}>{error}</FieldError>
    </div>
  );
});

export const Checkbox = forwardRef(function Checkbox(
  { className, label, 'aria-label': ariaLabel, ...props },
  ref
) {
  return (
    <label
      className={cn(
        'inline-flex cursor-pointer items-center gap-2 text-sm font-medium leading-none peer-disabled:cursor-not-allowed peer-disabled:opacity-70',
        className
      )}
    >
      <input
        ref={ref}
        type="checkbox"
        aria-label={label ? ariaLabel : ariaLabel || 'Toggle option'}
        className="h-4 w-4 rounded border-input bg-background text-primary focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
        {...props}
      />
      {label && <span className="text-muted-foreground">{label}</span>}
    </label>
  );
});

export const Textarea = forwardRef(function Textarea(
  {
    className,
    label,
    error,
    id: suppliedId,
    name,
    placeholder,
    'aria-label': ariaLabel,
    'aria-describedby': ariaDescribedBy,
    'aria-invalid': ariaInvalid,
    ...props
  },
  ref
) {
  const generatedId = useId();
  const controlId = normalizedControlId('textarea', suppliedId, generatedId);
  const errorId = error ? `${controlId}-error` : undefined;
  const accessibleName = fallbackAccessibleName(label, ariaLabel, name, placeholder);

  return (
    <div className="w-full">
      {label && (
        <label htmlFor={controlId} className="mb-1.5 block text-sm font-medium text-foreground">
          {label}
        </label>
      )}
      <textarea
        id={controlId}
        name={name}
        placeholder={placeholder}
        aria-label={accessibleName}
        aria-describedby={describedByValue(ariaDescribedBy, errorId)}
        aria-invalid={error ? true : ariaInvalid}
        className={cn(
          'flex min-h-32 w-full rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50',
          error && 'border-destructive focus-visible:ring-destructive',
          className
        )}
        ref={ref}
        {...props}
      />
      <FieldError id={errorId}>{error}</FieldError>
    </div>
  );
});
