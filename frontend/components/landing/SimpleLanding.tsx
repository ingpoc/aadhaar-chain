'use client';

import dynamic from 'next/dynamic';
import Link from 'next/link';

import { Button } from '@/components/ui/button';

function WalletButtonSkeleton() {
  return <div className="h-11 w-36 animate-pulse rounded-full bg-secondary" />;
}

const WalletConnectionButton = dynamic(
  () =>
    import('@/components/wallet/WalletButton').then((mod) => ({
      default: mod.WalletConnectionButton,
    })),
  {
    ssr: false,
    loading: () => <WalletButtonSkeleton />,
  }
);

export function SimpleLanding() {
  return (
    <div className="min-h-screen bg-background">
      <header className="app-shell flex h-20 items-center justify-between">
        <span className="text-lg font-semibold tracking-tight">AadhaarChain</span>
        <WalletConnectionButton />
      </header>
      <main className="app-shell flex min-h-[calc(100vh-5rem)] flex-col items-center justify-center py-16 text-center">
        <p className="text-xs font-semibold uppercase tracking-[0.2em] text-muted-foreground">
          Verified trust · AgentGuard
        </p>
        <h1 className="mt-4 max-w-2xl font-serif text-4xl font-normal tracking-tight text-foreground md:text-5xl">
          Verify once. Delegate safely.
        </h1>
        <p className="mt-6 max-w-xl text-base text-muted-foreground md:text-lg">
          Bind government-grade verification to your account. Share minimal proof with apps —
          never your documents again.
        </p>
        <div className="mt-10 flex flex-wrap items-center justify-center gap-3">
          <Button size="lg" asChild>
            <Link href="/verify">Get started</Link>
          </Button>
          <Button size="lg" variant="outline" asChild>
            <Link href="/home">Open home</Link>
          </Button>
        </div>
      </main>
    </div>
  );
}
