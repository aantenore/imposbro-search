'use client';

import { cva } from 'class-variance-authority';
import { cn } from '../../lib/utils';

const badgeVariants = cva(
  'inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-semibold transition-colors focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2',
  {
    variants: {
      variant: {
        default: 'border-transparent bg-primary text-primary-foreground',
        secondary: 'border-transparent bg-secondary text-secondary-foreground',
        destructive: 'border-transparent bg-destructive text-destructive-foreground',
        outline: 'text-foreground',
        success:
          'border-transparent bg-emerald-500/15 text-emerald-400 dark:bg-emerald-500/20',
        warning:
          'border-transparent bg-amber-500/15 text-amber-400 dark:bg-amber-500/20',
        error:
          'border-transparent bg-destructive/15 text-destructive dark:bg-destructive/20',
        info: 'border-transparent bg-primary/15 text-primary dark:bg-primary/20',
        purple:
          'border-transparent bg-violet-500/15 text-violet-400 dark:bg-violet-500/20',
      },
    },
    defaultVariants: {
      variant: 'default',
    },
  }
);

/**
 * StatusBadge – backward compat: "default" maps to secondary style.
 */
export default function StatusBadge({ children, variant = 'default', className = '' }) {
  const mapped = variant === 'default' ? 'secondary' : variant;
  return (
    <span className={cn(badgeVariants({ variant: mapped }), className)}>
      {children}
    </span>
  );
}
