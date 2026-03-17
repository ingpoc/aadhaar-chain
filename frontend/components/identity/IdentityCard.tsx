'use client';

import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { useWallet } from '@solana/wallet-adapter-react';
import { useEffect, useState } from 'react';
import axios from 'axios';
import { identityApi } from '@/lib/api';
import type { Identity, TrustReadSurface } from '@/lib/types';

export function IdentityCard() {
  const { publicKey, connected } = useWallet();
  const [identity, setIdentity] = useState<Identity | null>(null);
  const [trustSurface, setTrustSurface] = useState<TrustReadSurface | null>(null);
  const walletAddress = publicKey?.toBase58();

  useEffect(() => {
    if (!connected || !walletAddress) {
      return;
    }

    let cancelled = false;

    identityApi.getIdentity(walletAddress)
      .then(async (result) => {
        if (!cancelled) {
          setIdentity(result);
        }

        try {
          const trust = await identityApi.getTrustSurface(walletAddress);
          if (!cancelled) {
            setTrustSurface(trust);
          }
        } catch (error: unknown) {
          if (!cancelled) {
            console.error('Failed to load trust surface', error);
            setTrustSurface(null);
          }
        }
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          if (axios.isAxiosError(error) && error.response?.status === 404) {
            setIdentity(null);
            setTrustSurface(null);
          } else {
            console.error('Failed to load identity', error);
          }
        }
      });

    return () => {
      cancelled = true;
    };
  }, [walletAddress, connected]);

  const activeIdentity =
    connected && identity?.owner === walletAddress ? identity : null;
  const activeTrustSurface =
    connected && trustSurface?.walletAddress === walletAddress ? trustSurface : null;

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

        {activeTrustSurface && activeTrustSurface.verifications.length > 0 && (
          <div className="card-section space-y-3">
            <p className="data-label">Trust Artifacts</p>
            {activeTrustSurface.verifications.map((verification) => (
              <div key={verification.verificationId} className="rounded-md border border-border p-3 space-y-2">
                <div className="data-row">
                  <span className="data-label">{verification.documentType.toUpperCase()}</span>
                  <span className="data-value">
                    {verification.review.status === 'manual_review_required'
                      ? 'Manual Review'
                      : formatLabel(verification.workflowStatus)}
                  </span>
                </div>
                <div className="data-row">
                  <span className="data-label">Evidence</span>
                  <span className="data-value">
                    {verification.evidenceStatus ? formatLabel(verification.evidenceStatus) : 'Pending'}
                  </span>
                </div>
                <div className="data-row">
                  <span className="data-label">Consent</span>
                  <span className="data-value">{formatLabel(verification.consent.status)}</span>
                </div>
                <div className="data-row">
                  <span className="data-label">Audit Ref</span>
                  <span className="data-value">{verification.auditReceipts[0]?.reference ?? 'Pending'}</span>
                </div>
              </div>
            ))}
          </div>
        )}
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

function formatLabel(value: string): string {
  return value.replace(/_/g, ' ').replace(/\b\w/g, (char) => char.toUpperCase());
}
