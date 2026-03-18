'use client';

import dynamic from 'next/dynamic';
import Link from 'next/link';

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

export const Navbar = () => {
  return (
    <nav className="landing-navbar fixed inset-x-0 top-0 z-50 border-b border-border/60 bg-background/82 backdrop-blur-md">
      <div className="app-shell">
        <div className="flex h-20 items-center justify-between">
          <Link href="/" className="text-lg font-semibold tracking-tight text-foreground">
            AadhaarChain
          </Link>
          <WalletConnectionButton />
        </div>
      </div>
    </nav>
  );
};
