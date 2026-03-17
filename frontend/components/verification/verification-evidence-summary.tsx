import { KeyValueList } from '@/components/ui/key-value-list';
import { formatStatusLabel } from '@/components/ui/status-badge';
import type { VerificationStatus } from '@/lib/types';

export function VerificationEvidenceSummary({
  status,
}: {
  status: VerificationStatus | null;
}) {
  const metadata = status?.metadata;

  if (!metadata) {
    return null;
  }

  return (
    <div className="space-y-4 rounded-[1.5rem] border border-border/80 bg-background/70 p-5">
      <p className="page-eyebrow">Evidence summary</p>

      <KeyValueList
        items={[
          {
            label: 'Evidence status',
            value: formatStatusLabel(metadata.evidenceStatus),
          },
          {
            label: 'Decision',
            value: formatStatusLabel(metadata.decision),
          },
          {
            label: 'Document agent',
            value: formatStatusLabel(metadata.document.provenance.status),
          },
          {
            label: 'Fraud agent',
            value: formatStatusLabel(metadata.fraud.provenance.status),
          },
          {
            label: 'Compliance agent',
            value: formatStatusLabel(metadata.compliance.provenance.status),
          },
        ]}
      />

      {metadata.document.source ? (
        <div className="space-y-3">
          <p className="page-eyebrow">Document source</p>
          <KeyValueList
            items={[
              {
                label: 'Transport',
                value: formatStatusLabel(metadata.document.source.transport),
              },
              {
                label: 'File',
                value: metadata.document.source.fileName ?? 'Unknown',
              },
              {
                label: 'Content type',
                value: metadata.document.source.contentType ?? 'Unknown',
              },
              {
                label: 'Size',
                value:
                  typeof metadata.document.source.sizeBytes === 'number'
                    ? `${metadata.document.source.sizeBytes} bytes`
                    : 'Unknown',
              },
            ]}
          />
          {metadata.document.source.sha256 ? (
            <p className="font-mono text-xs text-muted-foreground">
              SHA256: {metadata.document.source.sha256}
            </p>
          ) : null}
        </div>
      ) : null}

      {metadata.blockingGaps.length ? (
        <div className="space-y-2">
          <p className="page-eyebrow">Blocking gaps</p>
          <ul className="space-y-2 pl-5 text-sm text-muted-foreground">
            {metadata.blockingGaps.map((gap) => (
              <li key={`${gap.stage}-${gap.code}`} className="list-disc">
                {gap.message}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}
