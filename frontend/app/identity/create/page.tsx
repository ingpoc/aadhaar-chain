'use client';

import { useState } from 'react';
import { useWallet } from '@solana/wallet-adapter-react';
import { identityApi } from '@/lib/api';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { useRouter } from 'next/navigation';

export default function CreateIdentityPage() {
  const { connected, publicKey } = useWallet();
  const [seed, setSeed] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const router = useRouter();

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();

    if (!connected) {
      setError('Please connect your wallet first');
      return;
    }

    if (!publicKey) {
      setError('Wallet public key is unavailable');
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

      alert('Identity created successfully!');
      router.push('/dashboard');
    } catch {
      setError('Failed to create identity. Please try again.');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <div>
          <h1>Create Identity</h1>
          <p className="text-muted-foreground">
            Create your wallet-bound identity anchor
          </p>
        </div>
      </div>

      {!connected && (
        <Alert className="border-yellow-200 bg-yellow-50 dark:bg-yellow-950/20">
          <AlertDescription className="text-yellow-800 dark:text-yellow-200">
            Please connect your wallet to continue
          </AlertDescription>
        </Alert>
      )}

      <Card>
        <CardHeader>
          <CardTitle>Identity Commitment</CardTitle>
          <CardDescription>
            Your DID is derived from your wallet. This step creates the initial identity commitment.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleSubmit} className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="seed">Commitment Seed</Label>
              <Input
                id="seed"
                placeholder="Optional local seed for the commitment"
                value={seed}
                onChange={(e) => setSeed(e.target.value)}
                disabled={!connected || loading}
              />
              <p className="text-xs text-muted-foreground">
                Leave empty to auto-generate a commitment seed from your wallet and the current timestamp.
              </p>
            </div>

            {error && (
              <Alert className="border-red-200 bg-red-50 dark:bg-red-950/20">
                <AlertDescription className="text-red-800 dark:text-red-200">
                  {error}
                </AlertDescription>
              </Alert>
            )}

            <div className="flex gap-3">
              <Button type="submit" disabled={!connected || loading}>
                {loading ? 'Creating...' : 'Create Identity'}
              </Button>
              <Button
                type="button"
                variant="outline"
                onClick={() => setSeed(`${publicKey?.toBase58() ?? 'wallet'}:${Date.now()}`)}
                disabled={!connected}
              >
                Auto-Generate Seed
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>What happens next?</CardTitle>
        </CardHeader>
        <CardContent className="space-y-2 text-sm text-muted-foreground">
          <p>1. A DID is derived from your wallet address</p>
          <p>2. A commitment is created for the identity anchor</p>
          <p>3. Raw identity material stays off-chain</p>
          <p>4. You can then add verification state and consented claims</p>
        </CardContent>
      </Card>
    </div>
  );
}

async function buildCommitment(walletAddress: string, seed: string): Promise<string> {
  const encoder = new TextEncoder();
  const bytes = encoder.encode(`${walletAddress}:${seed}`);
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  const hash = Array.from(new Uint8Array(digest))
    .map((byte) => byte.toString(16).padStart(2, '0'))
    .join('');
  return `sha256:${hash}`;
}
