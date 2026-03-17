'use client';

import { useWallet } from '@solana/wallet-adapter-react';
import { CreditCard } from 'lucide-react';

import { PageHeader } from '@/components/layout/page-header';
import { Button } from '@/components/ui/button';
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { EmptyState } from '@/components/ui/empty-state';
import { KeyValueList } from '@/components/ui/key-value-list';
import { StatusBadge } from '@/components/ui/status-badge';

const credentials = [
  {
    id: 'cred-001',
    type: 'Aadhaar Verification',
    issuer: 'UIDAI',
    issuedAt: '2024-01-15',
    status: 'verified',
  },
  {
    id: 'cred-002',
    type: 'PAN Verification',
    issuer: 'Income Tax Department',
    issuedAt: '2024-01-16',
    status: 'verified',
  },
];

export default function CredentialsPage() {
  const { connected } = useWallet();

  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="Credential registry"
        title="Issued credentials"
        description="Inspect the placeholder credential inventory and the metadata that downstream verifiers would eventually consume."
        actions={
          connected ? (
            <Button variant="outline" disabled>
              Issuance coming soon
            </Button>
          ) : undefined
        }
      />

      {!connected ? (
        <EmptyState
          title="Wallet connection required"
          description="Credential views stay bound to the active wallet so that identity ownership and issuance history remain aligned."
          icon={CreditCard}
        />
      ) : credentials.length === 0 ? (
        <EmptyState
          title="No credentials issued yet"
          description="Complete an Aadhaar or PAN verification and the credential registry will populate from the trust surface."
          icon={CreditCard}
        />
      ) : (
        <div className="grid gap-4 lg:grid-cols-2">
          {credentials.map((credential) => (
            <Card key={credential.id}>
              <CardHeader>
                <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
                  <div className="space-y-2">
                    <CardTitle>{credential.type}</CardTitle>
                    <CardDescription>Issued by {credential.issuer}</CardDescription>
                  </div>
                  <StatusBadge status={credential.status} />
                </div>
              </CardHeader>
              <CardContent>
                <KeyValueList
                  items={[
                    {
                      label: 'Issued',
                      value: new Date(credential.issuedAt).toLocaleDateString(),
                    },
                    {
                      label: 'Credential ID',
                      value: credential.id,
                      valueClassName: 'font-mono text-xs',
                    },
                  ]}
                />
              </CardContent>
              <CardFooter className="gap-3">
                <Button variant="outline" size="sm" className="flex-1">
                  View
                </Button>
                <Button variant="outline" size="sm" className="flex-1">
                  Share
                </Button>
              </CardFooter>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
