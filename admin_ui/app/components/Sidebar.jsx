'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { LayoutDashboard, Server, Database, GitBranch, Search, Workflow } from 'lucide-react';
import { cn } from '../lib/utils';

const navItems = [
  { href: '/dashboard', label: 'Dashboard', icon: LayoutDashboard },
  { href: '/clusters', label: 'Clusters', icon: Server },
  { href: '/collections', label: 'Collections', icon: Database },
  { href: '/routing', label: 'Routing', icon: GitBranch },
  { href: '/workspace', label: 'Workspace', icon: Workflow },
];

export default function Sidebar() {
  const pathname = usePathname();

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
      <div className="hidden border-t border-sidebar-border px-4 py-3 text-xs text-muted-foreground lg:block">
        Federated · v4
      </div>
    </aside>
  );
}
