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

export default function CredentialsPage() {
  const { connected, publicKey } = useWallet();
  const [trustSurface, setTrustSurface] = useState<TrustReadSurface | null>(null);
  const [loadedWalletAddress, setLoadedWalletAddress] = useState<string | null>(null);
  const [error, setError] = useState('');

  const walletAddress = publicKey?.toBase58() ?? null;

  useEffect(() => {
    if (!connected || !walletAddress) {
      return;
    }

    let cancelled = false;
    const loadTrustSurface = async () => {
      try {
        const surface = await identityApi.getTrustSurface(walletAddress);
        if (!cancelled) {
          setTrustSurface(surface);
          setLoadedWalletAddress(walletAddress);
          setError('');
        }
      } catch (loadError) {
        if (!cancelled) {
          console.error('Failed to load credential trust surface', loadError);
          setTrustSurface(null);
          setLoadedWalletAddress(walletAddress);
          setError('Unable to load the trust surface for this wallet.');
        }
      }
    };

    void loadTrustSurface();

    return () => {
      cancelled = true;
    };
  }, [connected, walletAddress]);

  const isLoadingTrustSurface =
    Boolean(connected && walletAddress) && loadedWalletAddress !== walletAddress && !error;
  const verifications = trustSurface?.verifications ?? [];
  const issuedCredentials = verifications.filter(
    (verification) => verification.attestation.status === 'issued'
  );
  const pendingCredentialReason = verifications[0]?.reason;

  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="Credential registry"
        title="Issued credentials"
        description="Inspect wallet-bound credentials that were actually issued from the trust surface rather than placeholder demo data."
        actions={
          connected ? (
            <Button variant="outline" disabled>
              Issuance follows approved verification
            </Button>
          ) : undefined
        }
      />

      {!connected ? (
        <EmptyState
          title="Wallet connection required"
          description="Credential views stay bound to the active wallet so that identity ownership and issuance history remain aligned."
          icon={CreditCard}
        />
      ) : isLoadingTrustSurface ? (
        <Notice tone="neutral" title="Loading credentials">
          Fetching the current wallet-bound trust surface.
        </Notice>
      ) : error ? (
        <Notice tone="destructive" title="Credential registry unavailable">
          {error}
        </Notice>
      ) : issuedCredentials.length === 0 ? (
        <>
          <EmptyState
            title="No credentials issued yet"
            description="Approved verification must issue an attestation before anything appears in the credential registry."
            icon={CreditCard}
          />
          {verifications.length ? (
            <Notice tone="warning" title="Verification artifacts exist, but issuance is still blocked">
              {pendingCredentialReason ??
                'The current verification state has not produced an issued credential yet.'}
            </Notice>
          ) : null}
        </>
      ) : (
        <div className="grid gap-4 lg:grid-cols-2">
          {issuedCredentials.map((credential) => (
            <Card key={credential.verificationId}>
              <CardHeader>
                <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
                  <div className="space-y-2">
                    <CardTitle>{credential.documentType.toUpperCase()} credential</CardTitle>
                    <CardDescription>
                      Issued by {issuerByDocumentType[credential.documentType]}
                    </CardDescription>
                  </div>
                  <StatusBadge status={credential.attestation.status} />
                </div>
              </CardHeader>
              <CardContent>
                <KeyValueList
                  items={[
                    {
                      label: 'Verification workflow',
                      value: credential.workflowStatus,
                    },
                    {
                      label: 'Credential reference',
                      value: credential.attestation.reference ?? 'Pending reference',
                      valueClassName: 'font-mono text-xs',
                    },
                    {
                      label: 'Audit reference',
                      value: credential.auditReceipts[0]?.reference ?? 'Pending audit reference',
                      valueClassName: 'font-mono text-xs',
                    },
                  ]}
                />
              </CardContent>
              <CardFooter className="gap-3">
                <Button variant="outline" size="sm" className="flex-1" disabled>
                  View
                </Button>
                <Button variant="outline" size="sm" className="flex-1" disabled>
                  Share
                </Button>
              </CardFooter>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
