'use client';

import { useEffect, useState } from 'react';
import { useWallet } from '@solana/wallet-adapter-react';
import { CreditCard } from 'lucide-react';

import { identityApi } from '@/lib/api';
import type { TrustReadSurface, TrustVerificationSummary } from '@/lib/types';
import { PageHeader } from '@/components/layout/page-header';
import { Notice } from '@/components/ui/notice';
import { Button } from '@/components/ui/button';
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { EmptyState } from '@/components/ui/empty-state';
import { KeyValueList } from '@/components/ui/key-value-list';
import { StatusBadge } from '@/components/ui/status-badge';

const issuerByDocumentType: Record<TrustVerificationSummary['documentType'], string> = {
  aadhaar: 'UIDAI',
  pan: 'Income Tax Department',
};

export default function ActivityPage() {
  const { connected, publicKey } = useWallet();
  const [trustSurface, setTrustSurface] = useState<TrustReadSurface | null>(null);
  const [loadedWalletAddress, setLoadedWalletAddress] = useState<string | null>(null);
  const [error, setError] = useState('');

  const walletAddress = publicKey?.toBase58() ?? null;

  useEffect(() => {
    if (!connected || !walletAddress) return;

    let cancelled = false;
    const loadTrustSurface = async () => {
      try {
        const surface = await identityApi.getTrustSurface(walletAddress);
        if (!cancelled) {
          setTrustSurface(surface);
          setLoadedWalletAddress(walletAddress);
          setError('');
        }
      } catch {
        if (!cancelled) {
          setTrustSurface(null);
          setLoadedWalletAddress(walletAddress);
          setError('Unable to load activity for this wallet.');
        }
      }
    };

    void loadTrustSurface();
    return () => {
      cancelled = true;
    };
  }, [connected, walletAddress]);

  const isLoading =
    Boolean(connected && walletAddress) && loadedWalletAddress !== walletAddress && !error;
  const verifications = trustSurface?.verifications ?? [];
  const issuedCredentials = verifications.filter(
    (v) => v.attestation.status === 'issued',
  );

  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="Activity"
        title="Verification history"
        description="Track verification steps and issued credentials for your connected wallet."
      />

      {!connected ? (
        <EmptyState
          title="Wallet connection required"
          description="Activity stays bound to your active wallet."
          icon={CreditCard}
        />
      ) : isLoading ? (
        <Notice tone="neutral" title="Loading activity">
          Fetching trust surface for this wallet.
        </Notice>
      ) : error ? (
        <Notice tone="destructive" title="Activity unavailable">
          {error}
        </Notice>
      ) : (
        <>
          {verifications.length > 0 ? (
            <Card>
              <CardHeader>
                <CardTitle>Timeline</CardTitle>
              </CardHeader>
              <CardContent className="space-y-3">
                {verifications.map((v) => (
                  <div
                    key={v.verificationId}
                    className="flex items-center justify-between rounded-xl border border-border/80 px-4 py-3 text-sm"
                  >
                    <span className="capitalize">{v.documentType} verification</span>
                    <StatusBadge status={v.workflowStatus} />
                  </div>
                ))}
              </CardContent>
            </Card>
          ) : null}

          {issuedCredentials.length === 0 ? (
            <EmptyState
              title="No credentials issued yet"
              description="Approved verification issues an attestation before it appears here."
              icon={CreditCard}
            />
          ) : (
            <div className="grid gap-4 lg:grid-cols-2">
              {issuedCredentials.map((credential) => (
                <Card key={credential.verificationId}>
                  <CardHeader>
                    <CardTitle>{credential.documentType.toUpperCase()} credential</CardTitle>
                    <CardDescription>
                      Issued by {issuerByDocumentType[credential.documentType]}
                    </CardDescription>
                  </CardHeader>
                  <CardContent>
                    <KeyValueList
                      items={[
                        { label: 'Workflow', value: credential.workflowStatus },
                        {
                          label: 'Reference',
                          value: credential.attestation.reference ?? 'Pending',
                          valueClassName: 'font-mono text-xs',
                        },
                      ]}
                    />
                  </CardContent>
                  <CardFooter className="gap-3">
                    <Button variant="outline" size="sm" className="flex-1" disabled>
                      View
                    </Button>
                  </CardFooter>
                </Card>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}
