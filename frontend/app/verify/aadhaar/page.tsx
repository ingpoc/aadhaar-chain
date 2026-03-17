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

export default function VerifyAadhaarPage() {
  const { connected, publicKey } = useWallet();
  const [step, setStep] = useState<ViewStep>('upload');
  const [aadhaarNumber, setAadhaarNumber] = useState('');
  const [name, setName] = useState('');
  const [dob, setDob] = useState('');
  const [address, setAddress] = useState('');
  const [file, setFile] = useState<File | null>(null);
  const [consent, setConsent] = useState(false);
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
          console.error('Failed to poll Aadhaar verification status', pollError);
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
      setError('Please upload an Aadhaar document.');
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
      setError('Please upload an Aadhaar document.');
      return;
    }

    if (!aadhaarNumber || !name || !dob) {
      setError('Aadhaar number, full name, and date of birth are required.');
      return;
    }

    if (!consent) {
      setError('Consent is required before Aadhaar verification can start.');
      return;
    }

    setError('');

    try {
      const response = await verificationApi.submitAadhaar(publicKey.toBase58(), {
        uid: aadhaarNumber,
        name,
        dob,
        address: address.trim() || undefined,
        documentFile: file,
        consentProvided: consent,
      });

      setVerificationId(response.verificationId);
      setStatus(null);
      setStep('processing');
    } catch (submissionError) {
      console.error('Failed to submit Aadhaar verification', submissionError);
      setError('Failed to submit Aadhaar verification.');
    }
  };

  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="Verification workflow"
        title="Verify Aadhaar"
        description="Upload evidence, attach required claims, and monitor the trust pipeline as the backend evaluates document, fraud, and compliance stages."
      />

      {!connected ? (
        <Notice tone="warning" title="Wallet required">
          Please connect your wallet before starting the Aadhaar verification flow.
        </Notice>
      ) : null}

      {step === 'upload' ? (
        <VerificationFlowShell
          title="Upload supporting document"
          description="Use a PDF or image that clearly represents the Aadhaar document you want the agents to evaluate."
          icon={Upload}
          badge={<StatusBadge tone="neutral" label="Step 1" />}
        >
          <div className="detail-stack">
            <div className="field-stack">
              <Label htmlFor="aadhaar-file">Aadhaar document</Label>
              <Input
                id="aadhaar-file"
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
          description="Provide the core claims that should match the uploaded Aadhaar document."
          badge={<StatusBadge tone="neutral" label="Step 2" />}
        >
          <div className="detail-stack">
            <div className="grid gap-4 md:grid-cols-2">
              <div className="field-stack">
                <Label htmlFor="aadhaar-number">Aadhaar number</Label>
                <Input
                  id="aadhaar-number"
                  placeholder="12-digit Aadhaar number"
                  value={aadhaarNumber}
                  onChange={(event) =>
                    setAadhaarNumber(event.target.value.replace(/\D/g, '').slice(0, 12))
                  }
                  maxLength={12}
                />
              </div>

              <div className="field-stack">
                <Label htmlFor="full-name">Full name</Label>
                <Input
                  id="full-name"
                  placeholder="Name as per Aadhaar"
                  value={name}
                  onChange={(event) => setName(event.target.value)}
                />
              </div>

              <div className="field-stack">
                <Label htmlFor="dob">Date of birth</Label>
                <Input
                  id="dob"
                  type="date"
                  value={dob}
                  onChange={(event) => setDob(event.target.value)}
                />
              </div>

              <div className="field-stack">
                <Label htmlFor="address">Address</Label>
                <Input
                  id="address"
                  placeholder="Optional address"
                  value={address}
                  onChange={(event) => setAddress(event.target.value)}
                />
              </div>
            </div>

            <label className="flex items-start gap-3 rounded-[1.5rem] border border-border/80 bg-background/70 p-4">
              <input
                type="checkbox"
                checked={consent}
                onChange={(event) => setConsent(event.target.checked)}
                className="mt-1 size-4 rounded border-input text-primary focus:ring-primary/20"
              />
              <span className="text-sm text-muted-foreground">
                I consent to the use of this information for identity verification. Raw
                document material should remain off-chain while only verification state and
                downstream trust artifacts are published.
              </span>
            </label>

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
          description={`Aadhaar verification completed with decision: ${status?.decision ?? 'approve'}.`}
          badge={<StatusBadge status={status?.decision ?? 'verified'} />}
        >
          <div className="detail-stack">
            <Notice tone="success">
              The verification finished successfully and the resulting trust state is ready for downstream consumption.
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
          description="The backend refused to auto-approve this verification because the evidence contract remains incomplete."
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
