import { Plus_Jakarta_Sans } from 'next/font/google';
import './globals.css';
import Sidebar from './components/Sidebar';

const plusJakarta = Plus_Jakarta_Sans({
  subsets: ['latin'],
  variable: '--font-sans',
  display: 'swap',
});

export const metadata = {
  title: 'IMPOSBRO Search Admin',
  description: 'Admin UI for managing the IMPOSBRO Search Engine',
};

export default function RootLayout({ children }) {
  return (
    <html lang="en" className={plusJakarta.variable}>
      <body className="font-sans min-h-screen">
        <div className="flex min-h-screen">
          <Sidebar />
          <main className="flex-1 overflow-auto bg-background p-6 sm:p-8 md:p-10">{children}</main>
        </div>
      </body>
    </html>
  );
}
