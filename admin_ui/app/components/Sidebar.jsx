'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import {
  ArchiveRestore,
  Database,
  GitBranch,
  LayoutDashboard,
  LogIn,
  LogOut,
  Search,
  Server,
  UserCircle,
  Workflow,
} from 'lucide-react';
import { api } from '../lib/api';
import { cn } from '../lib/utils';

const navItems = [
  { href: '/dashboard', label: 'Dashboard', icon: LayoutDashboard },
  { href: '/clusters', label: 'Clusters', icon: Server },
  { href: '/collections', label: 'Collections', icon: Database },
  { href: '/routing', label: 'Routing', icon: GitBranch },
  { href: '/workspace', label: 'Workspace', icon: Workflow },
  { href: '/operations', label: 'Operations', icon: ArchiveRestore },
];

export default function Sidebar() {
  const pathname = usePathname();
  const [session, setSession] = useState({ enabled: false, authenticated: false });

  useEffect(() => {
    let cancelled = false;
    api.auth.session()
      .then((nextSession) => {
        if (!cancelled) setSession(nextSession);
      })
      .catch(() => {
        if (!cancelled) setSession({ enabled: false, authenticated: false });
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const returnTo = encodeURIComponent(pathname || '/dashboard');
  const loginHref = `/api/auth/login?return_to=${returnTo}`;
  const logoutHref = `/api/auth/logout?return_to=${returnTo}`;
  const expiresAt = session.expires_at ? new Date(session.expires_at) : null;
  const expiryLabel = expiresAt && !Number.isNaN(expiresAt.getTime())
    ? expiresAt.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    : null;

  return (
    <aside className="flex w-full shrink-0 flex-col border-b border-sidebar-border bg-sidebar text-sidebar-foreground shadow-card lg:min-h-screen lg:w-64 lg:border-b-0 lg:border-r">
      <div className="flex h-16 items-center gap-2 border-b border-sidebar-border px-4 sm:px-6">
        <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-primary/15 text-primary">
          <Search className="h-5 w-5" />
        </div>
        <div className="min-w-0">
          <span className="truncate text-base font-semibold tracking-tight">IMPOSBRO</span>
          <span className="block truncate text-xs text-muted-foreground">Search Admin</span>
        </div>
      </div>
      <nav className="flex gap-1 overflow-x-auto p-3 lg:flex-1 lg:flex-col lg:space-y-0.5 lg:overflow-visible">
        {navItems.map((item) => {
          const isActive = pathname?.startsWith(item.href);
          return (
            <Link
              key={item.label}
              href={item.href}
              className={cn(
                'flex shrink-0 items-center gap-2 rounded-lg px-3 py-2.5 text-sm font-medium transition-colors lg:gap-3',
                isActive
                  ? 'bg-sidebar-accent text-sidebar-accent-foreground shadow-sm'
                  : 'text-muted-foreground hover:bg-sidebar-accent/80 hover:text-sidebar-foreground'
              )}
            >
              <item.icon className="h-5 w-5 shrink-0 opacity-90" />
              {item.label}
            </Link>
          );
        })}
      </nav>
      <div className="border-t border-sidebar-border px-4 py-3 text-xs text-muted-foreground">
        {session.enabled ? (
          <div className="space-y-3">
            <div className="flex items-center gap-2">
              <UserCircle className="h-4 w-4 shrink-0 text-primary" />
              <div className="min-w-0">
                <p className="truncate font-medium text-sidebar-foreground">
                  {session.authenticated ? 'Signed in' : 'Not signed in'}
                </p>
                {session.authenticated && expiryLabel && (
                  <p className="truncate">Session until {expiryLabel}</p>
                )}
              </div>
            </div>
            <a
              href={session.authenticated ? logoutHref : loginHref}
              className="inline-flex w-full items-center justify-center gap-2 rounded-md border border-sidebar-border px-3 py-2 font-medium text-sidebar-foreground transition-colors hover:bg-sidebar-accent"
            >
              {session.authenticated ? <LogOut className="h-4 w-4" /> : <LogIn className="h-4 w-4" />}
              {session.authenticated ? 'Sign out' : 'Sign in'}
            </a>
          </div>
        ) : (
          <span className="hidden lg:block">Federated · v4</span>
        )}
      </div>
    </aside>
  );
}
