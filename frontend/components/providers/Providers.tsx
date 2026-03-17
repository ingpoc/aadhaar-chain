'use client';

import type { ReactNode } from 'react';
import { WalletProvider } from '@/lib/wallet';
import { WalletRouter } from './WalletRouter';

export function Providers({ children }: { children: ReactNode }) {
  return (
    <WalletProvider>
      <WalletRouter />
      {children}
    </WalletProvider>
  );
}
