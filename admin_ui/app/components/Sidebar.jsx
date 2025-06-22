'use client';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { LayoutDashboard, Server, Database, GitBranch } from 'lucide-react';

const navItems = [
  { href: '/dashboard', label: 'Dashboard', icon: LayoutDashboard },
  { href: '/clusters', label: 'Clusters', icon: Server },
  { href: '/collections', label: 'Collections', icon: Database },
  { href: '/routing', label: 'Routing', icon: GitBranch },
];

export default function Sidebar() {
  const pathname = usePathname();
  return (
    <aside className='w-64 bg-gray-900/80 backdrop-blur-sm border-r border-gray-700 p-4 flex flex-col'>
      <div className='mb-8'>
        <h1 className='text-2xl font-bold text-white'>IMPOSBRO</h1>
        <p className='text-sm text-gray-400'>Admin Panel</p>
      </div>
      <nav className='flex-grow'>
        <ul>
          {navItems.map((item) => (
            <li key={item.label} className='mb-2'>
              <Link href={item.href} className={`flex items-center p-3 rounded-lg text-gray-300 transition-colors ${pathname.startsWith(item.href) ? 'bg-blue-600 text-white' : 'hover:bg-gray-700/50'}`}>
                <item.icon className='w-5 h-5 mr-3' />
                {item.label}
              </Link>
            </li>
          ))}
        </ul>
      </nav>
      <div className='text-xs text-gray-500'>v2.1.0 (Federated)</div>
    </aside>
  );
}
