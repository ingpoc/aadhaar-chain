'use client';

import { useWallet } from '@solana/wallet-adapter-react';

import { EmptyState } from '@/components/ui/empty-state';
import { KeyValueList } from '@/components/ui/key-value-list';
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { StatusBadge } from '@/components/ui/status-badge';
import { useWalletStore } from '@/stores/wallet';

export function WalletInfo() {
  const { publicKey, connected } = useWallet();
  const balance = useWalletStore((state) => state.balance);

  if (!connected) {
    return (
      <EmptyState
        title="Wallet connection required"
        description="Connect your Solana wallet to inspect balances, verification state, and credentials."
      />
    );
  }

  const address = publicKey?.toBase58() ?? '';
  const shortAddress = `${address.slice(0, 6)}...${address.slice(-6)}`;

  return (
    <Card>
      <CardHeader>
        <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
          <div className="space-y-2">
            <CardTitle>Wallet snapshot</CardTitle>
            <p className="text-sm text-muted-foreground">
              Live connection state and on-chain balance for the active wallet.
            </p>
          </div>
          <StatusBadge status="connected" />
        </div>
      </CardHeader>
      <CardContent className="space-y-5">
        <KeyValueList
          items={[
            {
              label: 'Address',
              value: shortAddress,
              valueClassName: 'font-mono tabular-nums',
            },
            {
              label: 'Balance',
              value: `${balance.toFixed(4)} SOL`,
              valueClassName: 'font-mono tabular-nums text-base',
            },
            {
              label: 'Network',
              value: 'Solana Devnet',
            },
          ]}
        />

        <div className="list-panel">
          <p className="page-eyebrow">Operational note</p>
          <p className="mt-2 text-sm text-muted-foreground">
            AadhaarChain binds identity proofs to wallet ownership. Keep the
            current wallet connected while creating or reviewing trust state.
          </p>
        </div>
      </CardContent>
    </Card>
  );
}
