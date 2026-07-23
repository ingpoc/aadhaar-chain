'use client';

import { useEffect, useState, useSyncExternalStore } from 'react';
import Link from 'next/link';
import { useWallet } from '@solana/wallet-adapter-react';

import { IdentityCard } from '@/components/identity/IdentityCard';
import { WalletInfo } from '@/components/wallet/WalletInfo';
import { PageHeader } from '@/components/layout/page-header';
import { EmptyState } from '@/components/ui/empty-state';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { identityApi } from '@/lib/api';
import type { TrustReadSurface } from '@/lib/types';

const PARTNER_APPS = [
  {
    name: 'ONDC Buyer',
    description: 'Search, cart, and checkout with verified trust.',
    url: 'http://127.0.0.1:43102/search',
    loginUrl:
      'http://127.0.0.1:43100/login?return=http%3A%2F%2F127.0.0.1%3A43102%2Fsearch&aud=ondcbuyer',
  },
  {
    name: 'ONDC Seller',
    description: 'Catalog and order management for verified sellers.',
    url: 'http://127.0.0.1:43103/dashboard',
    loginUrl:
      'http://127.0.0.1:43100/login?return=http%3A%2F%2F127.0.0.1%3A43103%2Fdashboard&aud=ondcseller',
  },
  {
    name: 'FlatWatch',
    description: 'Society finance with accountable identity.',
    url: 'http://127.0.0.1:43105/',
    loginUrl: null as string | null,
  },
];

function resolveHomeTrustState(surface: TrustReadSurface | null) {
  if (!surface) {
    return 'no_identity' as const;
  }

  const latest = surface.verifications[0];
  if (!latest) {
    return 'unverified' as const;
  }
  if (latest.workflowStatus === 'verified') {
    return 'verified' as const;
  }
  if (latest.workflowStatus === 'manual_review') {
    return 'manual_review' as const;
  }
  return 'unverified' as const;
}

const subscribeToClientMount = () => () => {};
const getClientMountSnapshot = () => true;
const getServerMountSnapshot = () => false;

export default function HomePage() {
  const { connected, publicKey } = useWallet();
  const walletAddress = publicKey?.toBase58() ?? null;
  const [trust, setTrust] = useState<TrustReadSurface | null>(null);
  const [fetchedForWallet, setFetchedForWallet] = useState<string | null>(null);
  // SSR + first client paint must match; autoConnect/localStorage can flip
  // `connected` before hydration.
  const mounted = useSyncExternalStore(
    subscribeToClientMount,
    getClientMountSnapshot,
    getServerMountSnapshot,
  );
  const walletReady = mounted && connected;

  useEffect(() => {
    if (!walletReady || !walletAddress) {
      return;
    }

    let cancelled = false;
    identityApi
      .getTrustSurface(walletAddress)
      .then((surface) => {
        if (!cancelled) {
          setTrust(surface);
          setFetchedForWallet(walletAddress);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setTrust(null);
          setFetchedForWallet(walletAddress);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [walletReady, walletAddress]);

  const activeTrust = walletReady && walletAddress && fetchedForWallet === walletAddress ? trust : null;
  const trustState = resolveHomeTrustState(activeTrust);
  const isVerified = trustState === 'verified';
  const isPending = trustState === 'manual_review';

  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="Home"
        title={
          !walletReady
            ? 'Your identity passport'
            : isVerified
              ? 'Your identity, ready to travel'
              : isPending
                ? 'Verification in progress'
                : 'Complete your verification'
        }
        description={
          !walletReady
            ? 'Connect a wallet to anchor your identity and verify once for every app in the portfolio.'
            : isVerified
              ? 'Government-grade trust is active on your wallet. Open a connected app or manage sharing.'
              : isPending
                ? 'We are reviewing your documents. You can track progress in Activity.'
                : 'Verify your Aadhaar once to unlock trusted checkout and seller actions across the portfolio.'
        }
        actions={
          walletReady && isVerified ? (
            <div className="flex flex-wrap gap-2">
              {PARTNER_APPS.map((app) => (
                <Button key={app.name} asChild variant={app.name === 'ONDC Buyer' ? 'default' : 'outline'}>
                  <a href={app.url}>{app.name}</a>
                </Button>
              ))}
            </div>
          ) : walletReady && !isVerified && !isPending ? (
            <Button asChild>
              <Link href="/verify">Verify Aadhaar</Link>
            </Button>
          ) : undefined
        }
      />

      {!walletReady ? (
        <EmptyState
          title="Connect your wallet"
          description="Your wallet anchors your identity on AadhaarChain."
          action={
            <Button asChild variant="outline">
              <Link href="/">Connect on landing</Link>
            </Button>
          }
        />
      ) : (
        <>
          <div className="section-grid items-start">
            <WalletInfo />
            <IdentityCard />
          </div>

          {isPending ? (
            <Card>
              <CardHeader>
                <CardTitle>Under review</CardTitle>
              </CardHeader>
              <CardContent className="space-y-3 text-sm text-muted-foreground">
                <p>Your Aadhaar submission is being reviewed.</p>
                <Button variant="outline" asChild>
                  <Link href="/activity">View activity</Link>
                </Button>
              </CardContent>
            </Card>
          ) : null}

          <div className="space-y-3">
            <h2 className="text-lg font-semibold tracking-tight">Connected apps</h2>
            <p className="text-sm text-muted-foreground">
              Portfolio apps that read your wallet-bound trust. Sign in from each app when
              prompted.
            </p>
            <div className="grid gap-4 lg:grid-cols-2">
              {PARTNER_APPS.map((app) => (
                <Card key={app.name}>
                  <CardHeader>
                    <CardTitle>{app.name}</CardTitle>
                  </CardHeader>
                  <CardContent className="space-y-4">
                    <p className="text-sm text-muted-foreground">{app.description}</p>
                    <div className="flex flex-wrap gap-2">
                      <Button asChild size="sm">
                        <a href={app.url}>Open app</a>
                      </Button>
                      {app.loginUrl ? (
                        <Button asChild size="sm" variant="outline">
                          <a href={app.loginUrl}>Sign in with AadhaarChain</a>
                        </Button>
                      ) : null}
                    </div>
                  </CardContent>
                </Card>
              ))}
            </div>
          </div>
        </>
      )}
    </div>
  );
}
