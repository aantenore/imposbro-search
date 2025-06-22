import { LayoutDashboard, Server, Database, GitBranch } from 'lucide-react';
import Link from 'next/link';

export default function DashboardPage() {
  return (
    <div>
      <h1 className='text-3xl font-bold mb-4 text-white'>Dashboard</h1>
      <p className='text-gray-400 max-w-2xl'>
        Welcome to the IMPOSBRO Search Engine Admin Panel. This is your central hub for managing a distributed, sharded search infrastructure.
      </p>
        <div className='mt-8 grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6'>
            <Link href='/clusters' className='bg-gray-800/50 border border-gray-700 p-6 rounded-lg hover:bg-gray-700/50 transition-colors'>
                <Server className='w-8 h-8 mb-3 text-blue-400' />
                <h2 className='text-xl font-semibold mb-2 text-white'>Manage Clusters</h2>
                <p className='text-gray-400'>Register and view your federated Typesense clusters.</p>
            </Link>
            <Link href='/collections' className='bg-gray-800/50 border border-gray-700 p-6 rounded-lg hover:bg-gray-700/50 transition-colors'>
                <Database className='w-8 h-8 mb-3 text-green-400' />
                <h2 className='text-xl font-semibold mb-2 text-white'>Manage Collections</h2>
                <p className='text-gray-400'>Create new search indexes (collections).</p>
            </Link>
             <Link href='/routing' className='bg-gray-800/50 border border-gray-700 p-6 rounded-lg hover:bg-gray-700/50 transition-colors'>
                <GitBranch className='w-8 h-8 mb-3 text-purple-400' />
                <h2 className='text-xl font-semibold mb-2 text-white'>Configure Routing</h2>
                <p className='text-gray-400'>Define rules to shard a collection's documents across multiple clusters.</p>
            </Link>
             <a href='http://localhost:3000' target='_blank' rel='noopener noreferrer' className='bg-gray-800/50 border border-gray-700 p-6 rounded-lg hover:bg-gray-700/50 transition-colors'>
                <LayoutDashboard className='w-8 h-8 mb-3 text-yellow-400' />
                <h2 className='text-xl font-semibold mb-2 text-white'>View Monitoring</h2>
                <p className='text-gray-400'>Open Grafana to monitor system health and performance.</p>
            </a>
        </div>
    </div>
  );
}
