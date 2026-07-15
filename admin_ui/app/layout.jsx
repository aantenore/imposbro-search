import './globals.css';
import Sidebar from './components/Sidebar';

export const metadata = {
  title: 'IMPOSBRO Search Admin',
  description: 'Admin UI for managing the IMPOSBRO Search Engine',
};

export const viewport = {
  width: 'device-width',
  initialScale: 1,
  colorScheme: 'dark',
  themeColor: '#11121a',
};

const criticalStyles = `
  :root {
    --background: oklch(0.12 0.01 265);
    --foreground: oklch(0.97 0 0);
    --card: oklch(0.18 0.015 265);
    --card-foreground: oklch(0.97 0 0);
    --primary: oklch(0.72 0.16 265);
    --primary-foreground: oklch(0.12 0.01 265);
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
    <html lang="en">
      <body className="font-sans min-h-screen">
        <style dangerouslySetInnerHTML={{ __html: criticalStyles }} />
        <a
          href="#main-content"
          className="sr-only z-[100] rounded-md bg-background px-4 py-2 font-medium text-foreground shadow-lg focus:not-sr-only focus:fixed focus:left-4 focus:top-4 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          Skip to main content
        </a>
        <div className="flex min-h-screen flex-col lg:flex-row">
          <Sidebar />
          <main
            id="main-content"
            tabIndex={-1}
            className="min-w-0 flex-1 overflow-auto bg-background p-4 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring sm:p-8 md:p-10"
          >
            {children}
          </main>
        </div>
      </body>
    </html>
  );
}
