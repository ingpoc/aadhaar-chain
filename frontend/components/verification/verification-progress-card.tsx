import { Loader2 } from 'lucide-react';

import { Progress } from '@/components/ui/progress';
import { StatusDot } from '@/components/ui/status-dot';
import { VerificationFlowShell } from '@/components/verification/verification-flow-shell';
import type { VerificationStatus } from '@/lib/types';

export function VerificationProgressCard({
  status,
  verificationId,
}: {
  status: VerificationStatus | null;
  verificationId: string;
}) {
  return (
    <VerificationFlowShell
      title="Verification in progress"
      description={`Tracking the backend verification state for ${verificationId}.`}
      icon={Loader2}
      tone="info"
    >
      <div className="space-y-5">
        <Progress value={(status?.progress ?? 0) * 100} />
        <p className="text-center text-sm text-muted-foreground">
          {labelForStep(status?.currentStep)}
        </p>
        <div className="list-panel space-y-3">
          {(status?.steps ?? []).map((item) => (
            <div
              key={`${item.name}-${item.status}`}
              className="flex items-center justify-between gap-4 text-sm"
            >
              <div className="inline-status">
                <StatusDot tone={toneForStepStatus(item.status)} />
                <span className="text-foreground">{labelForStep(item.name)}</span>
              </div>
              <span className="text-muted-foreground">
                {formatStepStatus(item.status)}
              </span>
            </div>
          ))}
        </div>
      </div>
    </VerificationFlowShell>
  );
}

export function labelForStep(step?: string): string {
  switch (step) {
    case 'document_received':
      return 'Document received';
    case 'parsing':
      return 'Parsing submitted evidence';
    case 'fraud_check':
      return 'Running fraud checks';
    case 'compliance_check':
      return 'Checking compliance';
    case 'blockchain_upload':
      return 'Publishing trust state';
    case 'complete':
      return 'Verification complete';
    default:
      return 'Awaiting backend status';
  }
}

function formatStepStatus(status: VerificationStatus['steps'][number]['status']) {
  switch (status) {
    case 'completed':
      return 'Complete';
    case 'in_progress':
      return 'In progress';
    case 'failed':
      return 'Failed';
    default:
      return 'Pending';
  }
}

function toneForStepStatus(
  status: VerificationStatus['steps'][number]['status']
): 'neutral' | 'info' | 'success' | 'warning' | 'destructive' {
  switch (status) {
    case 'completed':
      return 'success';
    case 'in_progress':
      return 'info';
    case 'failed':
      return 'destructive';
    default:
      return 'neutral';
  }
}
