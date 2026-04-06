import type { Metadata } from 'next';
import './globals.css';

export const metadata: Metadata = {
  title: 'Kalishi Edge',
  description: 'Personal AI Sports Betting Intelligence System',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen antialiased">
        {children}
      </body>
    </html>
  );
}
