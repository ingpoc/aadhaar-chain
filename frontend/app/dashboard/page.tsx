'use client';

import Link from 'next/link';
import { useWallet } from '@solana/wallet-adapter-react';
import { ArrowUpRight, BadgeCheck, CreditCard, ShieldCheck } from 'lucide-react';

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
  const { connected } = useWallet();

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
