'use client';

import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { useWallet } from '@solana/wallet-adapter-react';
import { useEffect, useState } from 'react';
import axios from 'axios';
import { identityApi } from '@/lib/api';
import type { Identity } from '@/lib/types';

export function IdentityCard() {
  const { publicKey, connected } = useWallet();
  const [identity, setIdentity] = useState<Identity | null>(null);

  useEffect(() => {
    if (!connected || !publicKey) {
      return;
    }

    let cancelled = false;
    const walletAddress = publicKey.toBase58();

    identityApi.getIdentity(walletAddress)
      .then((result) => {
        if (!cancelled) {
          setIdentity(result);
        }
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          if (axios.isAxiosError(error) && error.response?.status === 404) {
            setIdentity(null);
          } else {
            console.error('Failed to load identity', error);
          }
        }
      });

    return () => {
      cancelled = true;
    };
  }, [publicKey, connected]);

  const activeIdentity = connected ? identity : null;

  if (!activeIdentity) {
    return (
      <Card className="metric-card">
        <CardHeader>
          <CardTitle className="text-lg">Identity Status</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex items-center gap-2 text-muted-foreground">
            <span className="status-dot-muted" />
            <span className="text-sm">{connected ? 'No identity found' : 'Wallet not connected'}</span>
          </div>
          <p className="text-sm text-muted-foreground">
            {connected
              ? 'Create your decentralized identity to get started.'
              : 'Connect your wallet to load or create an identity anchor.'}
          </p>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card className="metric-card">
      <CardHeader>
        <CardTitle className="text-lg">Identity Status</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="data-row">
          <span className="data-label">DID</span>
          <span className="data-value">{activeIdentity.did}</span>
        </div>

        <div className="data-row">
          <span className="data-label">Owner</span>
          <span className="data-value">{activeIdentity.owner}</span>
        </div>

        <div className="data-row">
          <span className="data-label">Commitment</span>
          <span className="data-value">
            {truncate(activeIdentity.commitment)}
          </span>
        </div>

        <div className="data-row">
          <span className="data-label">Verification Bitmap</span>
          <span className="metric-value text-xl">{activeIdentity.verificationBitmap}</span>
        </div>
      </CardContent>
    </Card>
  );
}

function truncate(value: string, size = 10): string {
  if (value.length <= size * 2) {
    return value;
  }

  return `${value.slice(0, size)}...${value.slice(-size)}`;
}
