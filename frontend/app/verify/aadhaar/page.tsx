'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { useWallet } from '@solana/wallet-adapter-react';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { Progress } from '@/components/ui/progress';
import { CheckCircle2, Loader2, Upload, XCircle } from 'lucide-react';
import { verificationApi } from '@/lib/api';
import type { VerificationStatus } from '@/lib/types';

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
          setError(nextStatus.metadata?.reason || nextStatus.error || 'Manual review required');
          setStep('review');
        } else if (nextStatus.status === 'failed') {
          setError(nextStatus.metadata?.reason || nextStatus.error || nextStatus.decision || 'Verification failed');
          setStep('error');
        }
      } catch (pollError) {
        if (!cancelled) {
          console.error('Failed to poll Aadhaar verification status', pollError);
          setError('Failed to fetch verification status');
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

  const handleFileUpload = (event: React.ChangeEvent<HTMLInputElement>) => {
    const selectedFile = event.target.files?.[0];
    if (selectedFile) {
      setFile(selectedFile);
      setError('');
    }
  };

  const handleContinue = () => {
    if (!file) {
      setError('Please upload your Aadhaar document');
      return;
    }
    setError('');
    setStep('details');
  };

  const handleSubmitVerification = async () => {
    if (!connected || !publicKey) {
      setError('Please connect your wallet first');
      return;
    }
    if (!aadhaarNumber.match(/^\d{12}$/)) {
      setError('Please enter a valid 12-digit Aadhaar number');
      return;
    }
    if (!name.trim() || !dob) {
      setError('Please provide your name and date of birth');
      return;
    }
    if (!file) {
      setError('Please upload an Aadhaar document before submitting');
      return;
    }
    if (!consent) {
      setError('You must accept the consent terms before submitting verification');
      return;
    }

    setError('');

    try {
      const response = await verificationApi.submitAadhaar(publicKey.toBase58(), {
        name: name.trim(),
        dob,
        uid: aadhaarNumber,
        address: address.trim() || undefined,
        documentFile: file,
        consentProvided: consent,
      });

      setVerificationId(response.verificationId);
      setStatus({
        verificationId: response.verificationId,
        status: 'pending',
        currentStep: 'document_received',
        progress: 0,
        steps: [
          {
            name: 'document_received',
            status: 'completed',
          },
        ],
      });
      setStep('processing');
    } catch (submitError) {
      console.error('Failed to submit Aadhaar verification', submitError);
      setError('Failed to submit Aadhaar verification');
      setStep('error');
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <div>
          <h1>Aadhaar Verification</h1>
          <p className="text-muted-foreground">
            Submit Aadhaar verification and track the backend workflow state.
          </p>
        </div>
      </div>

      {!connected && (
        <Alert className="border-yellow-200 bg-yellow-50 dark:bg-yellow-950/20">
          <AlertDescription className="text-yellow-800 dark:text-yellow-200">
            Please connect your wallet to continue.
          </AlertDescription>
        </Alert>
      )}

      {step === 'upload' && (
        <Card>
          <CardHeader>
            <CardTitle>Upload Aadhaar Document</CardTitle>
            <CardDescription>
              Upload the document you want to submit for verification.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="aadhaar-file">Document</Label>
              <Input
                id="aadhaar-file"
                type="file"
                accept="image/*,.pdf"
                onChange={handleFileUpload}
              />
            </div>

            {file && (
              <div className="flex items-center gap-2 rounded-md border border-green-200 bg-green-50 px-3 py-2 text-sm text-green-800 dark:border-green-900 dark:bg-green-950/30 dark:text-green-200">
                <Upload className="h-4 w-4" />
                {file.name}
              </div>
            )}

            {error && (
              <Alert className="border-red-200 bg-red-50 dark:bg-red-950/20">
                <AlertDescription className="text-red-800 dark:text-red-200">
                  {error}
                </AlertDescription>
              </Alert>
            )}

            <Button onClick={handleContinue} disabled={!connected}>
              Continue
            </Button>
          </CardContent>
        </Card>
      )}

      {step === 'details' && (
        <Card>
          <CardHeader>
            <CardTitle>Verification Details</CardTitle>
            <CardDescription>
              Provide the details needed to create the verification record.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="aadhaar-number">Aadhaar Number</Label>
              <Input
                id="aadhaar-number"
                placeholder="12-digit Aadhaar number"
                value={aadhaarNumber}
                onChange={(event) => setAadhaarNumber(event.target.value.replace(/\D/g, '').slice(0, 12))}
                maxLength={12}
              />
            </div>

            <div className="space-y-2">
              <Label htmlFor="full-name">Full Name</Label>
              <Input
                id="full-name"
                placeholder="Name as per Aadhaar"
                value={name}
                onChange={(event) => setName(event.target.value)}
              />
            </div>

            <div className="space-y-2">
              <Label htmlFor="dob">Date of Birth</Label>
              <Input
                id="dob"
                type="date"
                value={dob}
                onChange={(event) => setDob(event.target.value)}
              />
            </div>

            <div className="space-y-2">
              <Label htmlFor="address">Address</Label>
              <Input
                id="address"
                placeholder="Optional address"
                value={address}
                onChange={(event) => setAddress(event.target.value)}
              />
            </div>

            <label className="flex items-start gap-3 rounded-md border border-border p-3">
              <input
                type="checkbox"
                checked={consent}
                onChange={(event) => setConsent(event.target.checked)}
                className="mt-1 h-4 w-4"
              />
              <span className="text-sm text-muted-foreground">
                I consent to the use of this information for identity verification. Raw identity material should remain off-chain; only verification state and related trust artifacts should be used downstream.
              </span>
            </label>

            {error && (
              <Alert className="border-red-200 bg-red-50 dark:bg-red-950/20">
                <AlertDescription className="text-red-800 dark:text-red-200">
                  {error}
                </AlertDescription>
              </Alert>
            )}

            <div className="flex gap-3">
              <Button onClick={handleSubmitVerification} disabled={!connected}>
                Submit Verification
              </Button>
              <Button type="button" variant="outline" onClick={() => setStep('upload')}>
                Back
              </Button>
            </div>
          </CardContent>
        </Card>
      )}

      {step === 'processing' && (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Loader2 className="h-5 w-5 animate-spin" />
              Verification In Progress
            </CardTitle>
            <CardDescription>
              Tracking backend verification state for {verificationId}.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <Progress value={(status?.progress ?? 0) * 100} className="h-2" />
            <p className="text-sm text-muted-foreground text-center">
              {labelForStep(status?.currentStep)}
            </p>
            <div className="space-y-2 text-sm">
              {(status?.steps ?? []).map((item) => (
                <div key={`${item.name}-${item.status}`} className="flex items-center gap-2 text-muted-foreground">
                  <span>{iconForStatus(item.status)}</span>
                  <span>{labelForStep(item.name)}</span>
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      )}

      {step === 'complete' && (
        <Card className="border-green-200 bg-green-50 dark:bg-green-950/20">
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-green-800 dark:text-green-200">
              <CheckCircle2 className="h-6 w-6" />
              Verification Complete
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <p className="text-green-700 dark:text-green-300">
              Aadhaar verification completed with decision: {status?.decision ?? 'approve'}.
            </p>
            <EvidenceSummary status={status} />
            <Button asChild>
              <Link href="/dashboard">Return to Dashboard</Link>
            </Button>
          </CardContent>
        </Card>
      )}

      {step === 'review' && (
        <Card className="border-yellow-200 bg-yellow-50 dark:bg-yellow-950/20">
          <CardHeader>
            <CardTitle className="text-yellow-800 dark:text-yellow-200">
              Manual Review Required
            </CardTitle>
            <CardDescription>
              The backend refused to auto-approve this verification because the evidence contract is incomplete.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <p className="text-yellow-800 dark:text-yellow-200">
              {status?.metadata?.reason || error}
            </p>
            <EvidenceSummary status={status} />
            <div className="flex gap-3">
              <Button variant="outline" onClick={() => setStep('details')}>
                Update Submission
              </Button>
              <Button asChild>
                <Link href="/dashboard">Return to Dashboard</Link>
              </Button>
            </div>
          </CardContent>
        </Card>
      )}

      {step === 'error' && (
        <Card className="border-red-200 bg-red-50 dark:bg-red-950/20">
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-red-800 dark:text-red-200">
              <XCircle className="h-6 w-6" />
              Verification Failed
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <p className="text-red-700 dark:text-red-300">
              {error || 'The verification workflow failed.'}
            </p>
            <Button variant="outline" onClick={() => setStep('details')}>
              Retry
            </Button>
          </CardContent>
        </Card>
      )}
    </div>
  );
}

function iconForStatus(status: VerificationStatus['steps'][number]['status']): string {
  switch (status) {
    case 'completed':
      return '✓';
    case 'in_progress':
      return '…';
    case 'failed':
      return '✕';
    default:
      return '○';
  }
}

function labelForStep(step?: string): string {
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

function EvidenceSummary({ status }: { status: VerificationStatus | null }) {
  const metadata = status?.metadata;
  if (!metadata) {
    return null;
  }

  return (
      <div className="space-y-3 rounded-md border border-border/60 bg-background/80 p-4 text-sm">
      <p className="font-medium">
        Evidence status: {metadata.evidenceStatus}
      </p>

      {metadata.document.source && (
        <div className="space-y-1 text-muted-foreground">
          <p>Document transport: {metadata.document.source.transport}</p>
          {metadata.document.source.fileName && (
            <p>Document file: {metadata.document.source.fileName}</p>
          )}
          {metadata.document.source.contentType && (
            <p>Document content type: {metadata.document.source.contentType}</p>
          )}
          {typeof metadata.document.source.sizeBytes === 'number' && (
            <p>Document size: {metadata.document.source.sizeBytes} bytes</p>
          )}
          {metadata.document.source.sha256 && (
            <p>Document sha256: {metadata.document.source.sha256}</p>
          )}
        </div>
      )}

      {metadata.blockingGaps.length > 0 && (
        <div className="space-y-1">
          <p className="font-medium">Blocking gaps</p>
          <ul className="list-disc space-y-1 pl-5 text-muted-foreground">
            {metadata.blockingGaps.map((gap) => (
              <li key={`${gap.stage}-${gap.code}`}>{gap.message}</li>
            ))}
          </ul>
        </div>
      )}

      <div className="space-y-1 text-muted-foreground">
        <p>Document input: {metadata.document.inputKind}</p>
        <p>Document agent: {metadata.document.provenance.status}</p>
        <p>Fraud agent: {metadata.fraud.provenance.status}</p>
        <p>Compliance agent: {metadata.compliance.provenance.status}</p>
      </div>
    </div>
  );
}
