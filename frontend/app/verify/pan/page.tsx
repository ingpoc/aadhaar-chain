'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { useWallet } from '@solana/wallet-adapter-react';
import { CheckCircle2, Upload, XCircle } from 'lucide-react';

import { verificationApi } from '@/lib/api';
import type { VerificationStatus } from '@/lib/types';
import { PageHeader } from '@/components/layout/page-header';
import { VerificationFlowShell } from '@/components/verification/verification-flow-shell';
import { VerificationEvidenceSummary } from '@/components/verification/verification-evidence-summary';
import { VerificationProgressCard } from '@/components/verification/verification-progress-card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Notice } from '@/components/ui/notice';
import { StatusBadge } from '@/components/ui/status-badge';

type ViewStep = 'upload' | 'details' | 'processing' | 'complete' | 'review' | 'error';

export default function VerifyPanPage() {
  const { connected, publicKey } = useWallet();
  const [step, setStep] = useState<ViewStep>('upload');
  const [panNumber, setPanNumber] = useState('');
  const [name, setName] = useState('');
  const [dob, setDob] = useState('');
  const [file, setFile] = useState<File | null>(null);
  const [verificationId, setVerificationId] = useState('');
  const [status, setStatus] = useState<VerificationStatus | null>(null);
  const [error, setError] = useState('');

  useEffect(() => {
    if (step !== 'processing' || !verificationId) {
      return;
    }

    let cancelled = false;

    const poll = async () => {
      try {
        const nextStatus = await verificationApi.getStatus(verificationId);
        if (cancelled) {
          return;
        }

        setStatus(nextStatus);

        if (nextStatus.status === 'verified') {
          setStep('complete');
        } else if (nextStatus.status === 'manual_review') {
          setError(nextStatus.metadata?.reason || nextStatus.error || 'Manual review required.');
          setStep('review');
        } else if (nextStatus.status === 'failed') {
          setError(
            nextStatus.metadata?.reason ||
              nextStatus.error ||
              nextStatus.decision ||
              'Verification failed.'
          );
          setStep('error');
        }
      } catch (pollError) {
        if (!cancelled) {
          console.error('Failed to poll PAN verification status', pollError);
          setError('Failed to fetch verification status.');
          setStep('error');
        }
      }
    };

    poll();
    const interval = window.setInterval(poll, 1200);

    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, [step, verificationId]);

  const handleFileChange = (event: React.ChangeEvent<HTMLInputElement>) => {
    setFile(event.target.files?.[0] ?? null);
    setError('');
  };

  const handleContinue = () => {
    if (!connected) {
      setError('Please connect your wallet to continue.');
      return;
    }

    if (!file) {
      setError('Please upload a PAN document.');
      return;
    }

    setError('');
    setStep('details');
  };

  const handleSubmitVerification = async () => {
    if (!connected || !publicKey) {
      setError('Please connect your wallet to continue.');
      return;
    }

    if (!file) {
      setError('Please upload a PAN document.');
      return;
    }

    if (!panNumber || !name || !dob) {
      setError('PAN number, full name, and date of birth are required.');
      return;
    }

    setError('');

    try {
      const response = await verificationApi.submitPan(publicKey.toBase58(), {
        panNumber,
        name,
        dob,
        documentFile: file,
      });

      setVerificationId(response.verificationId);
      setStatus(null);
      setStep('processing');
    } catch (submissionError) {
      console.error('Failed to submit PAN verification', submissionError);
      setError('Failed to submit PAN verification.');
    }
  };

  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="Verification workflow"
        title="Verify PAN"
        description="Use the same trust pipeline to upload PAN evidence, submit matching claims, and observe the backend verification stages in one standardized surface."
      />

      {!connected ? (
        <Notice tone="warning" title="Wallet required">
          Please connect your wallet before starting the PAN verification flow.
        </Notice>
      ) : null}

      {step === 'upload' ? (
        <VerificationFlowShell
          title="Upload supporting document"
          description="Use a clear image or PDF of the PAN card that the verification pipeline should evaluate."
          icon={Upload}
          badge={<StatusBadge tone="neutral" label="Step 1" />}
        >
          <div className="detail-stack">
            <div className="field-stack">
              <Label htmlFor="pan-file">PAN document</Label>
              <Input
                id="pan-file"
                type="file"
                accept="image/*,application/pdf"
                onChange={handleFileChange}
              />
            </div>

            {file ? (
              <Notice tone="success" title="Evidence attached">
                {file.name}
              </Notice>
            ) : null}

            {error ? (
              <Notice tone="destructive" title="Unable to continue">
                {error}
              </Notice>
            ) : null}

            <div className="page-actions">
              <Button onClick={handleContinue} disabled={!connected}>
                Continue
              </Button>
            </div>
          </div>
        </VerificationFlowShell>
      ) : null}

      {step === 'details' ? (
        <VerificationFlowShell
          title="Verification details"
          description="Provide the claims that should match the uploaded PAN document."
          badge={<StatusBadge tone="neutral" label="Step 2" />}
        >
          <div className="detail-stack">
            <div className="grid gap-4 md:grid-cols-2">
              <div className="field-stack">
                <Label htmlFor="pan-number">PAN number</Label>
                <Input
                  id="pan-number"
                  placeholder="ABCDE1234F"
                  value={panNumber}
                  onChange={(event) => setPanNumber(event.target.value.toUpperCase())}
                  maxLength={10}
                  className="uppercase"
                />
              </div>

              <div className="field-stack">
                <Label htmlFor="name">Name as per PAN</Label>
                <Input
                  id="name"
                  placeholder="Full name"
                  value={name}
                  onChange={(event) => setName(event.target.value)}
                />
              </div>

              <div className="field-stack md:col-span-2">
                <Label htmlFor="dob">Date of birth</Label>
                <Input
                  id="dob"
                  type="date"
                  value={dob}
                  onChange={(event) => setDob(event.target.value)}
                />
              </div>
            </div>

            {error ? (
              <Notice tone="destructive" title="Unable to submit verification">
                {error}
              </Notice>
            ) : null}

            <div className="page-actions">
              <Button onClick={handleSubmitVerification} disabled={!connected}>
                Submit verification
              </Button>
              <Button variant="outline" onClick={() => setStep('upload')}>
                Back
              </Button>
            </div>
          </div>
        </VerificationFlowShell>
      ) : null}

      {step === 'processing' ? (
        <VerificationProgressCard status={status} verificationId={verificationId} />
      ) : null}

      {step === 'complete' ? (
        <VerificationFlowShell
          tone="success"
          icon={CheckCircle2}
          title="Verification complete"
          description={`PAN verification completed with decision: ${status?.decision ?? 'approve'}.`}
          badge={<StatusBadge status={status?.decision ?? 'verified'} />}
        >
          <div className="detail-stack">
            <Notice tone="success">
              PAN verification finished successfully and the resulting trust state can now inform downstream workflows.
            </Notice>
            <VerificationEvidenceSummary status={status} />
            <div className="page-actions">
              <Button asChild>
                <Link href="/dashboard">Return to dashboard</Link>
              </Button>
            </div>
          </div>
        </VerificationFlowShell>
      ) : null}

      {step === 'review' ? (
        <VerificationFlowShell
          tone="warning"
          title="Manual review required"
          description="The backend requires manual intervention before this verification can be approved."
          badge={<StatusBadge status="manual_review" />}
        >
          <div className="detail-stack">
            <Notice tone="warning">{status?.metadata?.reason || error}</Notice>
            <VerificationEvidenceSummary status={status} />
            <div className="page-actions">
              <Button variant="outline" onClick={() => setStep('details')}>
                Update details
              </Button>
              <Button asChild>
                <Link href="/dashboard">Return to dashboard</Link>
              </Button>
            </div>
          </div>
        </VerificationFlowShell>
      ) : null}

      {step === 'error' ? (
        <VerificationFlowShell
          tone="destructive"
          icon={XCircle}
          title="Verification failed"
          description="The verification workflow could not be completed."
          badge={<StatusBadge status="failed" />}
        >
          <div className="detail-stack">
            <Notice tone="destructive">{error || 'The verification workflow failed.'}</Notice>
            <VerificationEvidenceSummary status={status} />
            <div className="page-actions">
              <Button variant="outline" onClick={() => setStep('details')}>
                Retry
              </Button>
            </div>
          </div>
        </VerificationFlowShell>
      ) : null}
    </div>
  );
}
