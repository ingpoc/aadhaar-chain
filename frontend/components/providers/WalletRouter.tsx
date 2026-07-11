'use client';

import { useEffect } from 'react';
import { useWallet } from '@solana/wallet-adapter-react';
import { useRouter, usePathname } from 'next/navigation';

// Public routes where a connected wallet should leave for /home
const PUBLIC_ROUTES = [
  '/',
];

export function WalletRouter() {
  const { connected } = useWallet();
  const router = useRouter();
  const pathname = usePathname();

  useEffect(() => {
    const isPublicRoute = PUBLIC_ROUTES.includes(pathname);

    if (connected && isPublicRoute) {
      // Auto-connected wallets briefly report disconnected during hydration.
      // Redirecting protected routes during that window traps the user in
      // a "/" -> "/home" loop, so only redirect the landing page.
      router.replace('/home');
    }
  }, [connected, pathname, router]);

  return null;
}
