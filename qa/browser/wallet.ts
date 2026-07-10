/**
 * Playwright helpers: create/inject a Phantom-compatible Solana wallet
 * that signs with a real ed25519 keypair (solders on the Node side).
 */
import { spawnSync } from 'node:child_process';
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import type { Page } from '@playwright/test';

const __dirname = dirname(fileURLToPath(import.meta.url));
const FIXTURE = join(__dirname, '../fixtures/test-wallet.json');
const PY = '/agent/repos/aadhaar-chain/gateway/.venv/bin/python';
const GATEWAY = 'http://127.0.0.1:43101';

export type TestWallet = {
  wallet: string;
  secret: number[];
  pubkeyBytes: number[];
};

function createWalletViaSolders(): TestWallet {
  const res = spawnSync(
    PY,
    [
      '-c',
      [
        'from solders.keypair import Keypair',
        'import json',
        'kp=Keypair()',
        'print(json.dumps({"wallet": str(kp.pubkey()), "secret": list(bytes(kp)), "pubkeyBytes": list(bytes(kp.pubkey()))}))',
      ].join(';'),
    ],
    { encoding: 'utf8' },
  );
  if (res.status !== 0) throw new Error(res.stderr);
  return JSON.parse(res.stdout.trim()) as TestWallet;
}

function ensurePubkeyBytes(wallet: { wallet: string; secret: number[]; pubkeyBytes?: number[] }): TestWallet {
  if (wallet.pubkeyBytes?.length === 32) {
    return wallet as TestWallet;
  }
  const res = spawnSync(
    PY,
    [
      '-c',
      'from solders.keypair import Keypair; import sys, json; kp=Keypair.from_bytes(bytes(json.loads(sys.argv[1]))); print(json.dumps(list(bytes(kp.pubkey()))))',
      JSON.stringify(wallet.secret),
    ],
    { encoding: 'utf8' },
  );
  if (res.status !== 0) throw new Error(res.stderr);
  return {
    wallet: wallet.wallet,
    secret: wallet.secret,
    pubkeyBytes: JSON.parse(res.stdout.trim()) as number[],
  };
}

export function loadOrCreateTestWallet(): TestWallet {
  mkdirSync(dirname(FIXTURE), { recursive: true });
  try {
    const raw = JSON.parse(readFileSync(FIXTURE, 'utf8')) as {
      wallet: string;
      secret: number[];
      pubkeyBytes?: number[];
    };
    const wallet = ensurePubkeyBytes(raw);
    writeFileSync(FIXTURE, JSON.stringify(wallet, null, 2));
    return wallet;
  } catch {
    const wallet = createWalletViaSolders();
    writeFileSync(FIXTURE, JSON.stringify(wallet, null, 2));
    return wallet;
  }
}

export function signMessageBytes(secret: number[], messageBytes: number[]): number[] {
  const res = spawnSync(
    PY,
    [
      '-c',
      [
        'from solders.keypair import Keypair',
        'import sys, json',
        'secret=bytes(json.loads(sys.argv[1]))',
        'msg=bytes(json.loads(sys.argv[2]))',
        'sig=Keypair.from_bytes(secret).sign_message(msg)',
        'print(json.dumps(list(bytes(sig))))',
      ].join(';'),
      JSON.stringify(secret),
      JSON.stringify(messageBytes),
    ],
    { encoding: 'utf8' },
  );
  if (res.status !== 0) throw new Error(res.stderr);
  return JSON.parse(res.stdout.trim()) as number[];
}

export async function seedVerifiedTrust(wallet: string): Promise<void> {
  const res = await fetch(`${GATEWAY}/api/identity/dev/fixtures/${wallet}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ fixture_state: 'verified', document_type: 'aadhaar' }),
  });
  if (!res.ok) {
    throw new Error(`fixture seed failed: ${res.status} ${await res.text()}`);
  }
  const trust = await fetch(`${GATEWAY}/api/identity/${wallet}/trust`);
  const body = await trust.json();
  if (body?.data?.trust_state !== 'verified') {
    throw new Error(`expected verified trust, got ${JSON.stringify(body)}`);
  }
}

/**
 * Inject a Phantom-compatible provider before any app JS runs.
 * Signing is delegated to Node via exposeFunction (real ed25519).
 */
export async function installInjectedWallet(page: Page, wallet: TestWallet): Promise<void> {
  // exposeFunction can only be registered once per page; ignore duplicates on retries.
  try {
    await page.exposeFunction('__qaSignMessageBytes', async (messageBytes: number[]) => {
      return signMessageBytes(wallet.secret, messageBytes);
    });
  } catch (err) {
    if (!String(err).includes('has been already registered')) throw err;
  }

  await page.addInitScript(
    ({ walletAddress, pubkeyBytes }) => {
      const listeners: Record<string, Set<(...args: unknown[]) => void>> = {};

      function emit(event: string, ...args: unknown[]) {
        for (const fn of listeners[event] || []) fn(...args);
      }

      class PublicKey {
        private readonly bytes: Uint8Array;
        constructor(
          private readonly value: string,
          bytes: number[],
        ) {
          this.bytes = new Uint8Array(bytes);
        }
        toBase58() {
          return this.value;
        }
        toString() {
          return this.value;
        }
        toJSON() {
          return this.value;
        }
        equals(other: { toString(): string }) {
          return this.value === other.toString();
        }
        toBytes() {
          return this.bytes;
        }
      }

      const publicKey = new PublicKey(walletAddress, pubkeyBytes);
      let connected = false;

      const provider = {
        isPhantom: true,
        isConnected: false,
        publicKey: null as PublicKey | null,
        connect: async () => {
          connected = true;
          provider.isConnected = true;
          provider.publicKey = publicKey;
          emit('connect', publicKey);
          return { publicKey };
        },
        disconnect: async () => {
          connected = false;
          provider.isConnected = false;
          provider.publicKey = null;
          emit('disconnect');
        },
        signMessage: async (message: Uint8Array) => {
          if (!connected) await provider.connect();
          const bytes = Array.from(message);
          const signatureBytes: number[] = await (
            window as unknown as { __qaSignMessageBytes: (b: number[]) => Promise<number[]> }
          ).__qaSignMessageBytes(bytes);
          return { signature: new Uint8Array(signatureBytes) };
        },
        signTransaction: async (tx: unknown) => tx,
        signAllTransactions: async (txs: unknown[]) => txs,
        on: (event: string, fn: (...args: unknown[]) => void) => {
          listeners[event] = listeners[event] || new Set();
          listeners[event].add(fn);
        },
        off: (event: string, fn: (...args: unknown[]) => void) => {
          listeners[event]?.delete(fn);
        },
        request: async ({ method }: { method: string }) => {
          if (method === 'connect') return provider.connect();
          if (method === 'disconnect') return provider.disconnect();
          throw new Error(`Unsupported wallet request: ${method}`);
        },
      };

      Object.defineProperty(window, 'solana', {
        configurable: true,
        writable: true,
        value: provider,
      });
      Object.defineProperty(window, 'phantom', {
        configurable: true,
        writable: true,
        value: { solana: provider },
      });
    },
    { walletAddress: wallet.wallet, pubkeyBytes: wallet.pubkeyBytes },
  );
}

export async function connectPhantom(page: Page): Promise<void> {
  // Wait for wallet adapter detection of injected Phantom.
  await page.waitForFunction(
    () => Boolean(window.phantom?.solana?.isPhantom || window.solana?.isPhantom),
    null,
    { timeout: 10000 },
  );

  // Already connected (button shows truncated pubkey like "EL4m..6fFu").
  const already = await page.evaluate(() =>
    Array.from(document.querySelectorAll('button')).some((el) => {
      const t = (el.textContent || '').trim();
      return /^[1-9A-HJ-NP-Za-km-z]{4}\.\.[1-9A-HJ-NP-Za-km-z]{4}$/.test(t);
    }),
  );
  if (already) return;

  const select = page.getByRole('button', { name: /Select Wallet/i }).first();
  await expectVisible(select, 15000);
  await select.click();

  // Modal lists "Phantom" / "Detected" — match broadly, not role=button only.
  const phantom = page.getByText(/Phantom/i).first();
  await expectVisible(phantom, 10000);
  await phantom.click();

  await page.waitForFunction(
    () => {
      const texts = Array.from(document.querySelectorAll('button')).map((el) =>
        (el.textContent || '').trim(),
      );
      const stillSelect = texts.some((t) => /Select Wallet/i.test(t));
      const connected = texts.some((t) =>
        /^[1-9A-HJ-NP-Za-km-z]{4}\.\.[1-9A-HJ-NP-Za-km-z]{4}$/.test(t),
      );
      return connected && !stillSelect;
    },
    null,
    { timeout: 20000 },
  );
}

async function expectVisible(locator: import('@playwright/test').Locator, timeout: number) {
  await locator.waitFor({ state: 'visible', timeout });
}
