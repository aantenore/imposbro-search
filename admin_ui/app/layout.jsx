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

export const viewport = { width: 'device-width', initialScale: 1 };

const criticalStyles = `
  :root {
    --background: oklch(0.12 0.01 265);
    --foreground: oklch(0.97 0 0);
    --card: oklch(0.18 0.015 265);
    --card-foreground: oklch(0.97 0 0);
    --primary: oklch(0.55 0.22 265);
    --primary-foreground: oklch(0.99 0 0);
    --muted: oklch(0.28 0.02 265);
    --muted-foreground: oklch(0.65 0.02 265);
    --border: oklch(0.32 0.02 265);
    --sidebar: oklch(0.14 0.015 265);
    --sidebar-foreground: oklch(0.97 0 0);
    --sidebar-border: oklch(0.28 0.02 265);
    --radius: 0.5rem;
  }
  body { background: var(--background); color: var(--foreground); -webkit-font-smoothing: antialiased; }
`;

export default function RootLayout({ children }) {
  return (
    <html lang="en" className={plusJakarta.variable}>
      <body className="font-sans min-h-screen">
        <style dangerouslySetInnerHTML={{ __html: criticalStyles }} />
        <div className="flex min-h-screen">
          <Sidebar />
          <main className="flex-1 overflow-auto bg-background p-6 sm:p-8 md:p-10">{children}</main>
        </div>
      </body>
    </html>
  );
}
