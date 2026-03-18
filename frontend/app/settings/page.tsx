'use client';

import { useMemo, useRef, useState } from 'react';
import { useWallet } from '@solana/wallet-adapter-react';

import { PageHeader } from '@/components/layout/page-header';
import { Button } from '@/components/ui/button';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { EmptyState } from '@/components/ui/empty-state';
import { Input } from '@/components/ui/input';
import { KeyValueList } from '@/components/ui/key-value-list';
import { Label } from '@/components/ui/label';
import { Notice } from '@/components/ui/notice';

export default function SettingsPage() {
  const { connected, publicKey } = useWallet();
  const walletAddress = publicKey?.toBase58() ?? null;
  const recoveryEmailRef = useRef<HTMLInputElement | null>(null);
  const [recoveryNotice, setRecoveryNotice] = useState<{
    tone: 'success' | 'destructive';
    message: string;
  } | null>(null);
  const storedRecoveryEmail = useMemo(() => {
    if (!walletAddress || typeof window === 'undefined') {
      return '';
    }

    return (
      window.localStorage.getItem(`aadhaar-chain:recovery-email:${walletAddress}`) ?? ''
    );
  }, [walletAddress]);

  const handleSaveRecoverySettings = () => {
    if (!walletAddress) {
      setRecoveryNotice({
        tone: 'destructive',
        message: 'Connect a wallet before saving recovery settings.',
      });
      return;
    }

    const nextEmail = recoveryEmailRef.current?.value.trim() ?? '';
    if (!nextEmail) {
      window.localStorage.removeItem(`aadhaar-chain:recovery-email:${walletAddress}`);
      setRecoveryNotice({
        tone: 'success',
        message: 'Recovery email cleared for this local wallet session.',
      });
      return;
    }

    window.localStorage.setItem(
      `aadhaar-chain:recovery-email:${walletAddress}`,
      nextEmail
    );
    setRecoveryNotice({
      tone: 'success',
      message: 'Recovery email saved locally for this wallet.',
    });
  };

  const handleExportData = () => {
    window.alert('Data export feature coming soon');
  };

  const handleDeleteIdentity = () => {
    if (
      window.confirm(
        'Are you sure you want to delete your identity? This action cannot be undone.'
      )
    ) {
      window.alert('Identity deletion feature coming soon');
    }
  };

  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="System settings"
        title="Wallet and privacy settings"
        description="Review the connected wallet, configure recovery details, and inspect the operational environment used by the authenticated app."
      />

      {!connected ? (
        <EmptyState
          title="Connect a wallet to access settings"
          description="Settings are scoped to the current wallet and only become meaningful once the ownership surface is active."
        />
      ) : (
        <>
          <div className="section-grid">
            <Card>
              <CardHeader>
                <CardTitle>Wallet information</CardTitle>
              </CardHeader>
              <CardContent>
                <KeyValueList
                  items={[
                    {
                      label: 'Address',
                      value: publicKey?.toBase58() ?? 'Unknown',
                      valueClassName: 'font-mono text-xs md:text-sm',
                    },
                    {
                      label: 'Network',
                      value: 'Solana Devnet',
                    },
                  ]}
                />
              </CardContent>
            </Card>

            <Card>
              <CardHeader>
                <CardTitle>Recovery settings</CardTitle>
                <CardDescription>
                  Configure a recovery contact for the wallet-bound identity surface.
                </CardDescription>
              </CardHeader>
              <CardContent className="space-y-4">
                <div className="field-stack">
                  <Label htmlFor="recovery-email">Recovery email</Label>
                  <Input
                    key={walletAddress ?? 'no-wallet'}
                    id="recovery-email"
                    name="recovery-email"
                    type="email"
                    autoComplete="email"
                    placeholder="recovery@example.com"
                    defaultValue={storedRecoveryEmail}
                    ref={recoveryEmailRef}
                  />
                </div>
                {recoveryNotice ? (
                  <Notice tone={recoveryNotice.tone} title="Recovery settings">
                    {recoveryNotice.message}
                  </Notice>
                ) : null}
                <Button onClick={handleSaveRecoverySettings}>Save recovery settings</Button>
              </CardContent>
            </Card>
          </div>

          <Card>
            <CardHeader>
              <CardTitle>Data and privacy</CardTitle>
              <CardDescription>
                Manage exports and high-risk lifecycle actions for the identity record.
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-6">
              <div className="flex flex-col gap-4 rounded-[1.5rem] border border-border/80 p-5 md:flex-row md:items-center md:justify-between">
                <div className="space-y-1">
                  <p className="text-sm font-semibold tracking-tight text-foreground">
                    Export data
                  </p>
                  <p className="text-sm text-muted-foreground">
                    Download your current wallet-linked data for portability and audit.
                  </p>
                </div>
                <Button variant="outline" onClick={handleExportData}>
                  Export data
                </Button>
              </div>

              <Notice tone="warning" title="Danger zone">
                Identity deletion is irreversible. This action should stay behind a dedicated
                confirmation flow before shipping to production.
              </Notice>

              <div className="flex flex-col gap-4 rounded-[1.5rem] border border-destructive/20 bg-destructive-soft/60 p-5 md:flex-row md:items-center md:justify-between">
                <div className="space-y-1">
                  <p className="text-sm font-semibold tracking-tight text-foreground">
                    Delete identity
                  </p>
                  <p className="text-sm text-muted-foreground">
                    Permanently remove the identity and associated local references.
                  </p>
                </div>
                <Button variant="destructive" onClick={handleDeleteIdentity}>
                  Delete identity
                </Button>
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Network settings</CardTitle>
              <CardDescription>
                Blockchain connectivity values currently used by the frontend.
              </CardDescription>
            </CardHeader>
            <CardContent>
              <KeyValueList
                items={[
                  {
                    label: 'RPC endpoint',
                    value:
                      process.env.NEXT_PUBLIC_SOLANA_RPC_URL ??
                      'https://api.devnet.solana.com',
                    valueClassName: 'font-mono text-xs md:text-sm',
                  },
                  {
                    label: 'Network',
                    value: 'Devnet',
                  },
                ]}
              />
            </CardContent>
          </Card>
        </>
      )}
    </div>
  );
}
