'use client';

import { useState } from 'react';
import { useRouter } from 'next/navigation';
import { useWallet } from '@solana/wallet-adapter-react';

import { identityApi } from '@/lib/api';
import { PageHeader } from '@/components/layout/page-header';
import { Notice } from '@/components/ui/notice';
import { Button } from '@/components/ui/button';
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';

export default function CreateIdentityPage() {
  const { connected, publicKey } = useWallet();
  const [seed, setSeed] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const router = useRouter();

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();

    if (!connected) {
      setError('Please connect your wallet first.');
      return;
    }

    if (!publicKey) {
      setError('Wallet public key is unavailable.');
      return;
    }

    setLoading(true);
    setError('');

    try {
      const commitment = await buildCommitment(
        publicKey.toBase58(),
        seed.trim() || `${publicKey.toBase58()}:${Date.now()}`
      );

      await identityApi.createIdentity(publicKey.toBase58(), { commitment });
      router.push('/dashboard');
    } catch {
      setError('Failed to create identity. Please try again.');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="Identity setup"
        title="Create an identity anchor"
        description="Bind the current wallet to a commitment-backed identity record that downstream verifications can update without exposing raw identity material on-chain."
      />

      {!connected ? (
        <Notice tone="warning" title="Wallet required">
          Please connect your wallet before creating an identity anchor.
        </Notice>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle>Commitment seed</CardTitle>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleSubmit} className="space-y-5">
            <div className="field-stack">
              <Label htmlFor="seed">Commitment seed</Label>
              <Input
                id="seed"
                placeholder="Optional local seed for the commitment"
                value={seed}
                onChange={(event) => setSeed(event.target.value)}
                disabled={!connected || loading}
              />
              <p className="text-sm text-muted-foreground">
                Leave this empty to derive a seed from the current wallet and timestamp.
              </p>
            </div>

            {error ? (
              <Notice tone="destructive" title="Unable to create identity">
                {error}
              </Notice>
            ) : null}

            <div className="page-actions">
              <Button type="submit" disabled={!connected || loading}>
                {loading ? 'Creating identity...' : 'Create identity'}
              </Button>
              <Button
                type="button"
                variant="outline"
                disabled={!connected || loading}
                onClick={() =>
                  setSeed(`${publicKey?.toBase58() ?? 'wallet'}:${Date.now()}`)
                }
              >
                Auto-generate seed
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>What happens next</CardTitle>
        </CardHeader>
        <CardContent>
          <ol className="space-y-3 text-sm text-muted-foreground">
            <li>1. AadhaarChain derives a DID from the connected wallet.</li>
            <li>2. A commitment is produced from the wallet and local seed.</li>
            <li>3. The resulting identity anchor is written without raw identity material.</li>
            <li>4. Verification workflows can then attach trust state and consented claims.</li>
          </ol>
        </CardContent>
      </Card>
    </div>
  );
}

async function buildCommitment(walletAddress: string, seed: string): Promise<string> {
  const encoder = new TextEncoder();
  const bytes = encoder.encode(`${walletAddress}:${seed}`);
  const digest = await crypto.subtle.digest('SHA-256', bytes as BufferSource);
  const hash = Array.from(new Uint8Array(digest))
    .map((byte) => byte.toString(16).padStart(2, '0'))
    .join('');

  return `sha256:${hash}`;
}
