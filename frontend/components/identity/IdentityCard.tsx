'use client';

import { useEffect, useState } from 'react';
import { useWallet } from '@solana/wallet-adapter-react';

import { identityApi } from '@/lib/api';
import type { Identity, TrustReadSurface } from '@/lib/types';
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { EmptyState } from '@/components/ui/empty-state';
import { KeyValueList } from '@/components/ui/key-value-list';
import { StatusBadge, formatStatusLabel } from '@/components/ui/status-badge';

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

    identityApi
      .getIdentity(walletAddress)
      .then(async (result) => {
        if (!result) {
          if (!cancelled) {
            setIdentity(null);
            setTrustSurface(null);
          }
          return;
        }

        if (!cancelled) {
          setIdentity(result);
        }

        try {
          const trust = await identityApi.getTrustSurface(walletAddress);
          if (!cancelled) {
            setTrustSurface(trust);
          }
        } catch (error) {
          if (!cancelled) {
            console.error('Failed to load trust surface', error);
            setTrustSurface(null);
          }
        }
      })
      .catch((error: unknown) => {
        if (cancelled) {
          return;
        }

        console.error('Failed to load identity', error);
      });

    return () => {
      cancelled = true;
    };
  }, [connected, walletAddress]);

  const activeIdentity =
    connected && identity?.owner === walletAddress ? identity : null;
  const activeTrustSurface =
    connected && trustSurface?.walletAddress === walletAddress ? trustSurface : null;

  if (!activeIdentity) {
    return (
      <EmptyState
        title="No identity anchor yet"
        description="Create your wallet-bound identity to start collecting verification artifacts and downstream credentials."
      />
    );
  }

  const latestVerification = activeTrustSurface?.verifications[0];

  return (
    <Card>
      <CardHeader>
        <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
          <div className="space-y-2">
            <CardTitle>Identity anchor</CardTitle>
            <p className="text-sm text-muted-foreground">
              Canonical identity record and the trust artifacts attached to it.
            </p>
          </div>
          <StatusBadge
            status={latestVerification?.workflowStatus ?? 'pending'}
            label={
              latestVerification
                ? formatStatusLabel(latestVerification.workflowStatus)
                : 'Awaiting verification'
            }
          />
        </div>
      </CardHeader>
      <CardContent className="space-y-6">
        <KeyValueList
          items={[
            {
              label: 'DID',
              value: activeIdentity.did,
              valueClassName: 'font-mono text-xs md:text-sm',
            },
            {
              label: 'Owner',
              value: activeIdentity.owner,
              valueClassName: 'font-mono text-xs md:text-sm',
            },
            {
              label: 'Commitment',
              value: truncate(activeIdentity.commitment),
              valueClassName: 'font-mono text-xs md:text-sm',
            },
            {
              label: 'Verification bitmap',
              value: String(activeIdentity.verificationBitmap),
              valueClassName: 'font-mono tabular-nums text-base',
            },
          ]}
        />

        {activeTrustSurface?.verifications.length ? (
          <div className="space-y-3">
            <p className="page-eyebrow">Trust artifacts</p>
            <div className="space-y-3">
              {activeTrustSurface.verifications.map((verification) => (
                <div
                  key={verification.verificationId}
                  className="list-panel space-y-4"
                >
                  <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                    <div>
                      <p className="text-sm font-semibold tracking-tight text-foreground">
                        {verification.documentType.toUpperCase()} verification
                      </p>
                      <p className="mt-1 text-sm text-muted-foreground">
                        {verification.reason ?? 'Verification artifact recorded on the trust surface.'}
                      </p>
                    </div>
                    <StatusBadge
                      status={
                        verification.review.status === 'manual_review_required'
                          ? verification.review.status
                          : verification.workflowStatus
                      }
                    />
                  </div>

                  <KeyValueList
                    items={[
                      {
                        label: 'Evidence',
                        value: verification.evidenceStatus
                          ? formatStatusLabel(verification.evidenceStatus)
                          : 'Pending',
                      },
                      {
                        label: 'Consent',
                        value: formatStatusLabel(verification.consent.status),
                      },
                      {
                        label: 'Attestation',
                        value: formatStatusLabel(verification.attestation.status),
                      },
                      {
                        label: 'Audit reference',
                        value: verification.auditReceipts[0]?.reference ?? 'Pending',
                        valueClassName: 'font-mono text-xs',
                      },
                    ]}
                  />
                </div>
              ))}
            </div>
          </div>
        ) : null}
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
