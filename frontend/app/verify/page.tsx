'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { useConnection, useWallet } from '@solana/wallet-adapter-react';
import axios from 'axios';
import { CheckCircle2, Upload, XCircle } from 'lucide-react';

import { identityApi, verificationApi } from '@/lib/api';
import { submitGatewayUnsignedTransaction } from '@/lib/solana';
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

type WizardStep =
  | 'anchor'
  | 'choose'
  | 'upload'
  | 'details'
  | 'processing'
  | 'complete'
  | 'review'
  | 'error';

const SETU_SESSION_KEY = 'aadhaarchain_setu_ekyc';

async function buildCommitment(walletAddress: string, seed: string): Promise<string> {
  const bytes = new TextEncoder().encode(`${walletAddress}:${seed}`);
  const digest = await crypto.subtle.digest('SHA-256', bytes as BufferSource);
  const hash = Array.from(new Uint8Array(digest))
    .map((byte) => byte.toString(16).padStart(2, '0'))
    .join('');
  return `sha256:${hash}`;
}

export default function VerifyPage() {
  const { connected, publicKey, signTransaction } = useWallet();
  const { connection } = useConnection();
  const walletAddress = publicKey?.toBase58() ?? null;

  const [step, setStep] = useState<WizardStep>('anchor');
  const [anchorLoading, setAnchorLoading] = useState(false);
  const [ekycEnabled, setEkycEnabled] = useState(false);
  const [ekycConsent, setEkycConsent] = useState(false);
  const [ekycStarting, setEkycStarting] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [aadhaarNumber, setAadhaarNumber] = useState('');
  const [name, setName] = useState('');
  const [dob, setDob] = useState('');
  const [address, setAddress] = useState('');
  const [consent, setConsent] = useState(false);
  const [verificationId, setVerificationId] = useState('');
  const [status, setStatus] = useState<VerificationStatus | null>(null);
  const [error, setError] = useState('');

  useEffect(() => {
    identityApi
      .getEkycConfig()
      .then((config) => setEkycEnabled(Boolean(config.enabled)))
      .catch(() => setEkycEnabled(false));
  }, []);

  useEffect(() => {
    if (!connected || !walletAddress) {
      return;
    }

    let cancelled = false;
    identityApi
      .getIdentity(walletAddress)
      .then((identity) => {
        if (!cancelled && identity) {
          setStep('choose');
        }
      })
      .catch(() => {
        // stay on anchor step
      });

    return () => {
      cancelled = true;
    };
  }, [connected, walletAddress]);

  useEffect(() => {
    if (typeof window === 'undefined') return;

    const params = new URLSearchParams(window.location.search);
    const returning = params.get('ekyc') === 'setu' || params.has('id');
    const storedRaw = sessionStorage.getItem(SETU_SESSION_KEY);
    if (!returning && !storedRaw) return;

    let setuId = params.get('id') || params.get('setu_id') || '';
    let storedVerificationId = '';
    if (storedRaw) {
      try {
        const stored = JSON.parse(storedRaw) as { setuId?: string; verificationId?: string };
        setuId = setuId || stored.setuId || '';
        storedVerificationId = stored.verificationId || '';
      } catch {
        // ignore corrupt session
      }
    }
    if (!setuId) return;

    let cancelled = false;
    setStep('processing');
    if (storedVerificationId) setVerificationId(storedVerificationId);

    const sync = async () => {
      try {
        const result = await verificationApi.syncSetuEkyc(setuId);
        if (cancelled) return;
        setVerificationId(result.verification_id);
        if (result.status === 'verified') {
          sessionStorage.removeItem(SETU_SESSION_KEY);
          setStep('complete');
          return;
        }
        if (result.status === 'failed') {
          sessionStorage.removeItem(SETU_SESSION_KEY);
          setError(`Setu eKYC failed (${result.setu_status}).`);
          setStep('error');
          return;
        }
        // still processing — poll verification status
      } catch {
        if (!cancelled) {
          setError('Failed to sync Setu eKYC status.');
          setStep('error');
        }
      }
    };

    void sync();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (step !== 'processing' || !verificationId) return;

    let cancelled = false;
    const poll = async () => {
      try {
        const nextStatus = await verificationApi.getStatus(verificationId);
        if (cancelled) return;
        setStatus(nextStatus);
        if (nextStatus.status === 'verified') setStep('complete');
        else if (nextStatus.status === 'manual_review') {
          setError(nextStatus.metadata?.reason || 'Manual review required.');
          setStep('review');
        } else if (nextStatus.status === 'failed') {
          setError(nextStatus.metadata?.reason || nextStatus.error || 'Verification failed.');
          setStep('error');
        }
      } catch {
        if (!cancelled) {
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

  async function handleCreateAnchor() {
    if (!connected || !publicKey || !signTransaction) {
      setError('Connect a wallet that supports transaction signing.');
      return;
    }

    setAnchorLoading(true);
    setError('');
    try {
      const seed = `${publicKey.toBase58()}:${Date.now()}`;
      const commitment = await buildCommitment(publicKey.toBase58(), seed);
      const createResponse = await identityApi.createIdentity(publicKey.toBase58(), { commitment });
      if (createResponse.signature) {
        await submitGatewayUnsignedTransaction(connection, createResponse.signature, signTransaction);
      }
      setStep('choose');
    } catch (err) {
      if (axios.isAxiosError(err) && err.response?.status === 409) {
        setStep('choose');
        return;
      }
      setError('Failed to anchor identity. Please try again.');
    } finally {
      setAnchorLoading(false);
    }
  }

  async function handleStartSetuEkyc() {
    if (!connected || !publicKey) return;
    if (!ekycConsent) {
      setError('Consent is required for Aadhaar OTP verification.');
      return;
    }
    setEkycStarting(true);
    setError('');
    try {
      const started = await verificationApi.startSetuEkyc(publicKey.toBase58(), true);
      sessionStorage.setItem(
        SETU_SESSION_KEY,
        JSON.stringify({
          setuId: started.setuId,
          verificationId: started.verificationId,
          wallet: publicKey.toBase58(),
        })
      );
      setVerificationId(started.verificationId);
      window.location.assign(started.kycUrl);
    } catch {
      setError('Failed to start Setu eKYC. Check gateway credentials.');
      setEkycStarting(false);
    }
  }

  async function handleSubmitVerification() {
    if (!connected || !publicKey || !file) return;
    if (!aadhaarNumber || !name || !dob || !consent) {
      setError('Complete all required fields and consent.');
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
    } catch {
      setError('Failed to submit Aadhaar verification.');
    }
  }

  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="Verification"
        title="Verify your identity"
        description={
          ekycEnabled
            ? 'Anchor your wallet, complete Aadhaar OTP via Setu, and unlock trust for connected apps.'
            : 'Anchor your wallet, upload Aadhaar evidence, and track verification in one guided flow.'
        }
      />

      {!connected ? (
        <Notice tone="warning" title="Wallet required">
          Connect your wallet before starting verification.
        </Notice>
      ) : null}

      {step === 'anchor' ? (
        <VerificationFlowShell
          title="Anchor your identity"
          description="Your wallet will be bound to a commitment-backed identity record. No seed input required."
          badge={<StatusBadge tone="neutral" label="Step 1 of 3" />}
        >
          <div className="detail-stack">
            {error ? (
              <Notice tone="destructive" title="Unable to continue">
                {error}
              </Notice>
            ) : null}
            <Button onClick={() => void handleCreateAnchor()} disabled={!connected || anchorLoading}>
              {anchorLoading ? 'Anchoring...' : 'Continue'}
            </Button>
          </div>
        </VerificationFlowShell>
      ) : null}

      {step === 'choose' ? (
        <VerificationFlowShell
          title="Choose verification path"
          description={
            ekycEnabled
              ? 'Prefer Aadhaar OTP (Setu). Document upload remains available for local demo.'
              : 'Document upload is the local demo path. Configure Setu eKYC for production OTP.'
          }
          badge={<StatusBadge tone="neutral" label="Step 2 of 3" />}
        >
          <div className="detail-stack">
            {ekycEnabled ? (
              <>
                <label className="flex items-start gap-3 rounded-[1.5rem] border border-border/80 bg-background/70 p-4">
                  <input
                    type="checkbox"
                    checked={ekycConsent}
                    onChange={(e) => setEkycConsent(e.target.checked)}
                    className="mt-1 size-4"
                  />
                  <span className="text-sm text-muted-foreground">
                    I consent to Aadhaar OTP verification via Setu. Masked identity fields only are stored.
                  </span>
                </label>
                <Button onClick={() => void handleStartSetuEkyc()} disabled={!connected || ekycStarting}>
                  {ekycStarting ? 'Opening Setu…' : 'Verify with Aadhaar OTP'}
                </Button>
              </>
            ) : (
              <Notice tone="info" title="Setu eKYC not configured">
                Gateway needs SETU_EKYC_ENABLED=true and sandbox credentials. Until then, use document upload.
              </Notice>
            )}
            {error ? (
              <Notice tone="destructive" title="Unable to continue">
                {error}
              </Notice>
            ) : null}
            <div className="page-actions">
              <Button variant="outline" onClick={() => setStep('upload')} disabled={!connected}>
                Upload document {ekycEnabled ? '(demo)' : ''}
              </Button>
            </div>
          </div>
        </VerificationFlowShell>
      ) : null}

      {step === 'upload' ? (
        <VerificationFlowShell
          title="Upload Aadhaar document"
          description="Use a PDF or image of your Aadhaar card."
          icon={Upload}
          badge={<StatusBadge tone="neutral" label="Step 2 of 3" />}
        >
          <div className="detail-stack">
            <div className="field-stack">
              <Label htmlFor="aadhaar-file">Aadhaar document</Label>
              <Input
                id="aadhaar-file"
                type="file"
                accept="image/*,application/pdf"
                onChange={(e) => {
                  setFile(e.target.files?.[0] ?? null);
                  setError('');
                }}
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
              <Button
                onClick={() => {
                  if (!file) setError('Please upload a document.');
                  else {
                    setError('');
                    setStep('details');
                  }
                }}
                disabled={!connected}
              >
                Continue
              </Button>
              <Button variant="outline" onClick={() => setStep('choose')}>
                Back
              </Button>
            </div>
          </div>
        </VerificationFlowShell>
      ) : null}

      {step === 'details' ? (
        <VerificationFlowShell
          title="Verification details"
          description="Claims must match your uploaded document."
          badge={<StatusBadge tone="neutral" label="Step 2 of 3" />}
        >
          <div className="detail-stack">
            <div className="grid gap-4 md:grid-cols-2">
              <div className="field-stack">
                <Label htmlFor="aadhaar-number">Aadhaar number</Label>
                <Input
                  id="aadhaar-number"
                  placeholder="12-digit Aadhaar number"
                  value={aadhaarNumber}
                  onChange={(e) =>
                    setAadhaarNumber(e.target.value.replace(/\D/g, '').slice(0, 12))
                  }
                  maxLength={12}
                />
              </div>
              <div className="field-stack">
                <Label htmlFor="full-name">Full name</Label>
                <Input
                  id="full-name"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                />
              </div>
              <div className="field-stack">
                <Label htmlFor="dob">Date of birth</Label>
                <Input
                  id="dob"
                  type="date"
                  value={dob}
                  onChange={(e) => setDob(e.target.value)}
                />
              </div>
              <div className="field-stack">
                <Label htmlFor="address">Address (optional)</Label>
                <Input
                  id="address"
                  value={address}
                  onChange={(e) => setAddress(e.target.value)}
                />
              </div>
            </div>
            <label className="flex items-start gap-3 rounded-[1.5rem] border border-border/80 bg-background/70 p-4">
              <input
                type="checkbox"
                checked={consent}
                onChange={(e) => setConsent(e.target.checked)}
                className="mt-1 size-4"
              />
              <span className="text-sm text-muted-foreground">
                I consent to identity verification. Raw documents stay off-chain.
              </span>
            </label>
            {error ? (
              <Notice tone="destructive" title="Unable to submit">
                {error}
              </Notice>
            ) : null}
            <div className="page-actions">
              <Button onClick={() => void handleSubmitVerification()} disabled={!connected}>
                Submit for verification
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
          description="Your trust state is ready for connected apps."
          badge={<StatusBadge status="verified" />}
        >
          <div className="page-actions">
            <Button asChild>
              <Link href="/home">Go to Home</Link>
            </Button>
          </div>
        </VerificationFlowShell>
      ) : null}

      {step === 'review' ? (
        <VerificationFlowShell
          tone="warning"
          title="Manual review required"
          badge={<StatusBadge status="manual_review" />}
        >
          <div className="detail-stack">
            <Notice tone="warning">{error}</Notice>
            <VerificationEvidenceSummary status={status} />
            <Button asChild>
              <Link href="/home">Go to Home</Link>
            </Button>
          </div>
        </VerificationFlowShell>
      ) : null}

      {step === 'error' ? (
        <VerificationFlowShell
          tone="destructive"
          icon={XCircle}
          title="Verification failed"
          badge={<StatusBadge status="failed" />}
        >
          <Notice tone="destructive">{error}</Notice>
          <Button variant="outline" onClick={() => setStep('choose')}>
            Retry
          </Button>
        </VerificationFlowShell>
      ) : null}
    </div>
  );
}
