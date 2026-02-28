import { Inter } from 'next/font/google';
import './globals.css';
import Sidebar from './components/Sidebar';
const inter = Inter({ subsets: ['latin'] });
export const metadata = {
  title: 'IMPOSBRO Search Admin',
  description: 'Admin UI for managing the IMPOSBRO Search Engine',
};
export default function RootLayout({ children }) {
  return (
    <html lang='en'>
      <body className={inter.className}>
        <div className='flex min-h-screen'>
          <Sidebar />
          <main className='flex-1 overflow-auto bg-background p-4 sm:p-6 md:p-8'>{children}</main>
        </div>
      </body>
    </html>
  );
}
