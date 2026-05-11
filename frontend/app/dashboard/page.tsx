'use client';

import { useState } from 'react';
import Link from 'next/link';
import { useConnection, useWallet } from '@solana/wallet-adapter-react';
import { ArrowUpRight, BadgeCheck, CreditCard, ShieldCheck } from 'lucide-react';
import { SystemProgram, Transaction } from '@solana/web3.js';

import { IdentityCard } from '@/components/identity/IdentityCard';
import { PageHeader } from '@/components/layout/page-header';
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { EmptyState } from '@/components/ui/empty-state';
import { Button } from '@/components/ui/button';
import { Notice } from '@/components/ui/notice';
import { WalletInfo } from '@/components/wallet/WalletInfo';

const quickActions = [
  {
    href: '/verify/aadhaar',
    title: 'Verify Aadhaar',
    description: 'Upload evidence and publish trust state for the identity anchor.',
    icon: ShieldCheck,
  },
  {
    href: '/verify/pan',
    title: 'Verify PAN',
    description: 'Run PAN verification with the same evidence and review workflow.',
    icon: BadgeCheck,
  },
  {
    href: '/credentials',
    title: 'Review credentials',
    description: 'Inspect issued verification credentials and the current sharing surface.',
    icon: CreditCard,
  },
];

export default function DashboardPage() {
  const { connection } = useConnection();
  const { connected, publicKey, signTransaction } = useWallet();
  const [signing, setSigning] = useState(false);
  const [signingResult, setSigningResult] = useState<{
    tone: 'success' | 'destructive';
    title: string;
    message: string;
  } | null>(null);

  async function handleSignOwnershipTransaction() {
    if (!connected || !publicKey || !signTransaction) {
      setSigningResult({
        tone: 'destructive',
        title: 'Wallet signing unavailable',
        message: 'Connect a wallet that supports Solana transaction signing before running this checkpoint.',
      });
      return;
    }

    setSigning(true);
    setSigningResult(null);

    try {
      const { blockhash, lastValidBlockHeight } = await connection.getLatestBlockhash();
      const transaction = new Transaction({
        blockhash,
        feePayer: publicKey,
        lastValidBlockHeight,
      }).add(
        SystemProgram.transfer({
          fromPubkey: publicKey,
          toPubkey: publicKey,
          lamports: 0,
        }),
      );

      const signedTransaction = await signTransaction(transaction);
      const signature = signedTransaction.signatures.find((entry) =>
        entry.publicKey.equals(publicKey),
      )?.signature;

      setSigningResult({
        tone: 'success',
        title: 'Transaction signed',
        message: signature
          ? `Wallet approval produced a local transaction signature for ${truncateSignature(signature)}. The transaction was not submitted.`
          : 'Wallet approval completed. The transaction was not submitted.',
      });
    } catch (error) {
      setSigningResult({
        tone: 'destructive',
        title: 'Transaction signing failed',
        message: error instanceof Error ? error.message : 'The wallet did not sign the transaction.',
      });
    } finally {
      setSigning(false);
    }
  }

  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="Authenticated app"
        title="Control center"
        description="Inspect wallet readiness, identity state, and the next verification moves from one consistent operations surface."
        actions={
          connected ? (
            <>
              <Button variant="outline" asChild>
                <Link href="/credentials">Credentials</Link>
              </Button>
              <Button asChild>
                <Link href="/identity/create">Create identity</Link>
              </Button>
            </>
          ) : undefined
        }
      />

      {!connected ? (
        <EmptyState
          title="Connect a wallet to enter the app"
          description="AadhaarChain uses the wallet as the anchor for identity ownership, verification artifacts, and credential access."
          action={
            <Button asChild>
              <Link href="/">Return to landing</Link>
            </Button>
          }
        />
      ) : (
        <>
          <div className="section-grid">
            <WalletInfo />
            <IdentityCard />
          </div>

          <Card>
            <CardHeader>
              <CardTitle>Transaction signing checkpoint</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              <p className="text-sm text-muted-foreground">
                Request a wallet signature for a local devnet ownership transaction before relying on
                signed trust flows. This checkpoint does not submit the transaction.
              </p>
              {signingResult ? (
                <Notice tone={signingResult.tone} title={signingResult.title}>
                  {signingResult.message}
                </Notice>
              ) : null}
              <Button type="button" onClick={handleSignOwnershipTransaction} disabled={signing}>
                {signing ? 'Waiting for wallet approval...' : 'Sign ownership transaction'}
              </Button>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Quick next steps</CardTitle>
            </CardHeader>
            <CardContent className="action-grid">
              {quickActions.map((action) => {
                const Icon = action.icon;

                return (
                  <Link key={action.href} href={action.href} className="block">
                    <Card className="card-interactive h-full border-border/80 bg-background">
                      <CardContent className="flex h-full flex-col justify-between gap-6 pt-6 md:pt-8">
                        <div className="space-y-4">
                          <div className="flex size-12 items-center justify-center rounded-2xl bg-accent text-primary">
                            <Icon className="size-5" />
                          </div>
                          <div className="space-y-2">
                            <p className="text-base font-semibold tracking-tight text-foreground">
                              {action.title}
                            </p>
                            <p className="text-sm text-muted-foreground">
                              {action.description}
                            </p>
                          </div>
                        </div>
                        <div className="inline-status justify-between text-sm font-medium text-foreground">
                          <span>Open flow</span>
                          <ArrowUpRight className="size-4" />
                        </div>
                      </CardContent>
                    </Card>
                  </Link>
                );
              })}
            </CardContent>
          </Card>
        </>
      )}
    </div>
  );
}

function truncateSignature(signature: Uint8Array): string {
  const hex = Array.from(signature)
    .slice(0, 8)
    .map((byte) => byte.toString(16).padStart(2, '0'))
    .join('');

  return `${hex}...`;
}
