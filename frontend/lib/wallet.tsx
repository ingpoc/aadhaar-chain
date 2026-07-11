'use client';

import '@solana/wallet-adapter-react-ui/styles.css';

import { useMemo } from 'react';
import type { WalletAdapter } from '@solana/wallet-adapter-base';
import {
  ConnectionProvider,
  WalletProvider as SolanaWalletProvider,
} from '@solana/wallet-adapter-react';
import { WalletModalProvider } from '@solana/wallet-adapter-react-ui';
import { PhantomWalletAdapter, SolflareWalletAdapter } from '@solana/wallet-adapter-wallets';
import { DevBurnerWalletAdapter } from '@/lib/devBurnerWallet';

/** Prefer local validator in dev; override with NEXT_PUBLIC_SOLANA_RPC_URL. */
export const SOLANA_RPC_URL =
  process.env.NEXT_PUBLIC_SOLANA_RPC_URL || 'http://127.0.0.1:8899';

const DEV_BURNER_WALLET_ENABLED = process.env.NEXT_PUBLIC_DEV_BURNER_WALLET === 'true';

export function resolveSolanaNetworkLabel(rpcUrl: string = SOLANA_RPC_URL): string {
  if (
    rpcUrl.includes('8899') ||
    rpcUrl.includes('localhost') ||
    rpcUrl.includes('127.0.0.1')
  ) {
    return 'Localnet';
  }
  if (rpcUrl.includes('devnet')) {
    return 'Devnet';
  }
  if (rpcUrl.includes('mainnet')) {
    return 'Mainnet Beta';
  }
  return 'Custom';
}

export function WalletProvider({ children }: { children: React.ReactNode }) {
  const wallets = useMemo(() => {
    const adapters: WalletAdapter[] = [
      new PhantomWalletAdapter(),
      new SolflareWalletAdapter(),
    ];
    if (DEV_BURNER_WALLET_ENABLED) {
      adapters.unshift(new DevBurnerWalletAdapter());
    }
    return adapters;
  }, []);

  return (
    <ConnectionProvider endpoint={SOLANA_RPC_URL}>
      <SolanaWalletProvider wallets={wallets} autoConnect>
        <WalletModalProvider>
          {children}
        </WalletModalProvider>
      </SolanaWalletProvider>
    </ConnectionProvider>
  );
}
