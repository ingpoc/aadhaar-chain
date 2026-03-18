'use client';

import { useState } from 'react';
import dynamic from 'next/dynamic';
import Link from 'next/link';
import { Menu } from 'lucide-react';
import { usePathname } from 'next/navigation';

import { Button } from '@/components/ui/button';
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from '@/components/ui/sheet';
import { cn } from '@/lib/utils';

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

const links = [
  { href: '/dashboard', label: 'Dashboard' },
  { href: '/identity/create', label: 'Create Identity' },
  { href: '/verify/aadhaar', label: 'Verify Aadhaar' },
  { href: '/verify/pan', label: 'Verify PAN' },
  { href: '/credentials', label: 'Credentials' },
  { href: '/settings', label: 'Settings' },
];

export function Navbar() {
  const pathname = usePathname();
  const [open, setOpen] = useState(false);

  return (
    <nav className="sticky top-0 z-50 border-b border-border/70 bg-background/92 backdrop-blur-md">
      <div className="app-shell">
        <div className="flex h-20 items-center justify-between gap-6">
          <div className="flex items-center gap-6">
            <Link href="/" className="text-lg font-semibold tracking-tight text-foreground">
              AadhaarChain
            </Link>
            <div className="hidden items-center gap-2 rounded-full border border-border/70 bg-card/80 p-1 md:flex">
              {links.map((link) => {
                const isActive = pathname === link.href;

                return (
                  <Link
                    key={link.href}
                    href={link.href}
                    className={cn(
                      'rounded-full px-4 py-2 text-sm font-medium tracking-tight transition-colors',
                      isActive
                        ? 'bg-secondary text-foreground'
                        : 'text-muted-foreground hover:text-foreground'
                    )}
                  >
                    {link.label}
                  </Link>
                );
              })}
            </div>
          </div>

          <div className="flex items-center gap-3">
            <WalletConnectionButton />

            <Sheet open={open} onOpenChange={setOpen}>
              <SheetTrigger asChild>
                <Button
                  variant="outline"
                  size="icon"
                  className="md:hidden"
                  aria-label="Open navigation"
                >
                  <Menu className="size-5" />
                </Button>
              </SheetTrigger>
              <SheetContent side="right">
                <SheetHeader>
                  <SheetTitle>Navigation</SheetTitle>
                  <SheetDescription>
                    Move between the authenticated identity surfaces.
                  </SheetDescription>
                </SheetHeader>
                <div className="mt-8 flex flex-col gap-2 px-6 pb-6">
                  {links.map((link) => {
                    const isActive = pathname === link.href;

                    return (
                      <Link
                        key={link.href}
                        href={link.href}
                        onClick={() => setOpen(false)}
                        className={cn(
                          'rounded-2xl border px-4 py-3 text-sm font-medium tracking-tight transition-colors',
                          isActive
                            ? 'border-primary/15 bg-accent text-foreground'
                            : 'border-border text-muted-foreground hover:bg-secondary hover:text-foreground'
                        )}
                      >
                        {link.label}
                      </Link>
                    );
                  })}
                </div>
              </SheetContent>
            </Sheet>
          </div>
        </div>
      </div>
    </nav>
  );
}
