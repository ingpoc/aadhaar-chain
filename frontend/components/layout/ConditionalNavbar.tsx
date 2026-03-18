'use client';

import type { ReactNode } from 'react';
import { usePathname } from 'next/navigation';

import { Navbar } from './Navbar';

export const ConditionalNavbar = () => {
  const pathname = usePathname();

  if (pathname === '/') {
    return null;
  }

  return <Navbar />;
};

export const ConditionalMainWrapper = ({
  children,
}: {
  children: ReactNode;
}) => {
  const pathname = usePathname();

  if (pathname === '/') {
    return <>{children}</>;
  }

  return (
    <main className="app-main">
      <div className="app-shell">{children}</div>
    </main>
  );
};
