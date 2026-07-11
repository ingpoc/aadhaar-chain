'use client';

import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useSearchParams } from 'next/navigation';
import { useWallet } from '@solana/wallet-adapter-react';
import { WalletMultiButton } from '@solana/wallet-adapter-react-ui';

import { PageHeader } from '@/components/layout/page-header';
import { Notice } from '@/components/ui/notice';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { encodeBase58 } from '@/lib/base58';

const API_BASE_URL = process.env.NEXT_PUBLIC_API_URL || 'http://127.0.0.1:43101';
const DEV_BURNER_WALLET_ENABLED = process.env.NEXT_PUBLIC_DEV_BURNER_WALLET === 'true';
const BURNER_WALLET_NAME = 'Burner Wallet';

const AUDIENCE_MAP: Record<string, 'buyer' | 'seller'> = {
  ondcbuyer: 'buyer',
  ondcseller: 'seller',
};

const ALLOWED_RETURN_ORIGINS = new Set([
  'http://127.0.0.1:43102',
  'http://localhost:43102',
  'http://127.0.0.1:43103',
  'http://localhost:43103',
  'https://ondcbuyer.aadharcha.in',
  'https://ondcseller.aadharcha.in',
]);

function resolveAudience(aud: string | null): 'buyer' | 'seller' | null {
  if (!aud) {
    return null;
  }
  return AUDIENCE_MAP[aud] ?? null;
}

function isAllowedReturnUrl(returnUrl: string | null): returnUrl is string {
  if (!returnUrl) {
    return false;
  }

  try {
    const parsed = new URL(returnUrl);
    return ALLOWED_RETURN_ORIGINS.has(parsed.origin);
  } catch {
    return false;
  }
}

function LoginContent() {
  const searchParams = useSearchParams();
  const { connected, publicKey, signMessage, connect, select, wallets } = useWallet();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const devConnectAttempted = useRef(false);
  const devSignInAttempted = useRef(false);

  const returnUrl = searchParams.get('return');
  const audParam = searchParams.get('aud');
  const devAuto = searchParams.get('dev_auto') === '1';
  const audience = useMemo(() => resolveAudience(audParam), [audParam]);
  const walletAddress = publicKey?.toBase58() ?? null;

  const runSignIn = useCallback(async () => {
    if (!connected || !walletAddress || !signMessage) {
      setError('Connect a Solana wallet before signing in.');
      return;
    }

    if (!audience) {
      setError('This login request is missing a supported app audience.');
      return;
    }

    if (!isAllowedReturnUrl(returnUrl)) {
      setError('This login request has an unsupported return URL.');
      return;
    }

    setLoading(true);
    setError('');

    try {
      const issueResponse = await fetch(
        `${API_BASE_URL}/api/identity/${walletAddress}/proof-token`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          credentials: 'include',
          body: JSON.stringify({
            audience,
            purpose: 'sso_login',
          }),
        },
      );

      if (!issueResponse.ok) {
        const payload = await issueResponse.json().catch(() => null);
        throw new Error(payload?.detail || payload?.message || 'Failed to issue login challenge.');
      }

      const issuePayload = await issueResponse.json();
      const issued = issuePayload.data;
      const messageBytes = new TextEncoder().encode(issued.message);
      const signatureBytes = await signMessage(messageBytes);

      const verifyResponse = await fetch(`${API_BASE_URL}/api/identity/proof-token/verify`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({
          token_id: issued.token_id,
          wallet_address: walletAddress,
          audience,
          message: issued.message,
          signature: encodeBase58(signatureBytes),
        }),
      });

      const verifyPayload = await verifyResponse.json();
      if (!verifyResponse.ok || !verifyPayload.data?.valid) {
        throw new Error(verifyPayload.message || 'Wallet signature verification failed.');
      }

      window.location.href = returnUrl;
    } catch (signInError) {
      setError(signInError instanceof Error ? signInError.message : 'Sign in failed.');
    } finally {
      setLoading(false);
    }
  }, [audience, connected, returnUrl, signMessage, walletAddress]);

  useEffect(() => {
    if (!DEV_BURNER_WALLET_ENABLED || !devAuto) {
      return;
    }
    if (!audience || !isAllowedReturnUrl(returnUrl)) {
      return;
    }

    if (!connected) {
      if (devConnectAttempted.current) {
        return;
      }
      devConnectAttempted.current = true;
      void (async () => {
        try {
          const burner = wallets.find((wallet) => wallet.adapter.name === BURNER_WALLET_NAME);
          if (burner) {
            select(burner.adapter.name);
          }
          await connect();
        } catch (autoError) {
          setError(autoError instanceof Error ? autoError.message : 'Dev wallet connect failed.');
        }
      })();
      return;
    }

    if (!walletAddress || !signMessage) {
      return;
    }

    if (devSignInAttempted.current || loading) {
      return;
    }
    devSignInAttempted.current = true;
    void runSignIn();
  }, [
    audience,
    connect,
    connected,
    devAuto,
    loading,
    returnUrl,
    runSignIn,
    select,
    signMessage,
    walletAddress,
    wallets,
  ]);

  const handleSignIn = () => {
    void runSignIn();
  };

  return (
    <div className="page-stack max-w-xl">
      <PageHeader
        eyebrow="Portfolio login"
        title="Sign in with AadhaarChain"
        description="Connect your wallet and sign a short challenge to open a shared session for ONDC buyer or seller apps."
      />

      {!audience ? (
        <Notice tone="destructive" title="Unsupported app">
          Only `ondcbuyer` and `ondcseller` login audiences are supported.
        </Notice>
      ) : null}

      {!isAllowedReturnUrl(returnUrl) ? (
        <Notice tone="destructive" title="Unsupported return URL">
          The requested return URL is not allowlisted for portfolio SSO.
        </Notice>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle>Wallet sign-in</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex flex-wrap items-center gap-3">
            <WalletMultiButton />
            {connected ? (
              <span className="text-sm text-muted-foreground">{walletAddress}</span>
            ) : null}
          </div>

          <p className="text-sm text-muted-foreground">
            App audience: <strong>{audParam ?? 'unknown'}</strong>
          </p>

          {DEV_BURNER_WALLET_ENABLED ? (
            <Notice tone="info" title="Local browser testing">
              Append <code>?dev_auto=1</code> to this URL to auto-connect the dev Burner Wallet and
              complete SSO without Phantom.
            </Notice>
          ) : null}

          {error ? (
            <Notice tone="destructive" title="Unable to sign in">
              {error}
            </Notice>
          ) : null}

          <Button
            type="button"
            disabled={
              !connected ||
              loading ||
              !audience ||
              !isAllowedReturnUrl(returnUrl)
            }
            onClick={handleSignIn}
          >
            {loading ? 'Signing in…' : 'Sign in and continue'}
          </Button>
        </CardContent>
      </Card>
    </div>
  );
}

export default function LoginPage() {
  return (
    <Suspense
      fallback={
        <div className="page-stack">
          <PageHeader
            eyebrow="Portfolio login"
            title="Sign in with AadhaarChain"
            description="Loading login request…"
          />
        </div>
      }
    >
      <LoginContent />
    </Suspense>
  );
}
