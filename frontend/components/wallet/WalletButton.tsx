'use client';

import { useEffect } from 'react';
import { useWallet } from '@solana/wallet-adapter-react';
import { WalletMultiButton } from '@solana/wallet-adapter-react-ui';
import { Connection } from '@solana/web3.js';

import { SOLANA_RPC_URL } from '@/lib/wallet';
import { useWalletStore } from '@/stores/wallet';

export function WalletConnectionButton() {
  const { publicKey, connected } = useWallet();
  const { setAddress, setConnected, fetchBalance } = useWalletStore();
  const balance = useWalletStore((state) => state.balance);

  useEffect(() => {
    setConnected(connected);
    setAddress(publicKey?.toBase58() ?? null);
  }, [connected, publicKey, setConnected, setAddress]);

  useEffect(() => {
    if (!publicKey) {
      return;
    }

    const connection = new Connection(SOLANA_RPC_URL, 'confirmed');

    fetchBalance(connection, publicKey);
    const interval = window.setInterval(() => {
      fetchBalance(connection, publicKey);
    }, 10000);

    return () => window.clearInterval(interval);
  }, [publicKey, fetchBalance]);

  return (
    <div className="wallet-connector">
      {connected ? (
        <span className="hidden font-mono text-sm tabular-nums text-muted-foreground sm:block">
          {balance.toFixed(4)} SOL
        </span>
      ) : null}
      <WalletMultiButton />
    </div>
  );
}
