import { ed25519 } from '@noble/curves/ed25519';
import type { WalletName } from '@solana/wallet-adapter-base';
import {
  BaseSignInMessageSignerWalletAdapter,
  isVersionedTransaction,
  WalletNotConnectedError,
  WalletReadyState,
} from '@solana/wallet-adapter-base';
import type { SolanaSignInInput, SolanaSignInOutput } from '@solana/wallet-standard-features';
import { createSignInMessage } from '@solana/wallet-standard-util';
import type { Transaction, TransactionVersion, VersionedTransaction } from '@solana/web3.js';
import { Keypair } from '@solana/web3.js';

export const DevBurnerWalletName = 'Burner Wallet' as WalletName<'Burner Wallet'>;

const STORAGE_KEY = 'aadhaarchain-portfolio-dev-burner-v1';

function loadOrCreateDevKeypair(): Keypair {
  if (typeof window === 'undefined') {
    return Keypair.generate();
  }

  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (raw) {
      const secret = Uint8Array.from(JSON.parse(raw) as number[]);
      return Keypair.fromSecretKey(secret);
    }
  } catch {
    // Fall through to a fresh keypair.
  }

  const keypair = Keypair.generate();
  window.localStorage.setItem(STORAGE_KEY, JSON.stringify(Array.from(keypair.secretKey)));
  return keypair;
}

/**
 * Local-only burner wallet with a stable keypair per browser profile.
 * Unsafe for production; replaces the random UnsafeBurnerWalletAdapter.
 */
export class DevBurnerWalletAdapter extends BaseSignInMessageSignerWalletAdapter {
  name = DevBurnerWalletName;
  url = 'https://github.com/anza-xyz/wallet-adapter#usage';
  icon =
    'data:image/svg+xml;base64,PHN2ZyB3aWR0aD0iMzQiIGhlaWdodD0iMzAiIGZpbGw9Im5vbmUiIHhtbG5zPSJodHRwOi8vd3d3LnczLm9yZy8yMDAwL3N2ZyI+PHBhdGggZmlsbC1ydWxlPSJldmVub2RkIiBjbGlwLXJ1bGU9ImV2ZW5vZGQiIGQ9Ik0zNCAxMC42djIuN2wtOS41IDE2LjVoLTQuNmw2LTEwLjVhMi4xIDIuMSAwIDEgMCAyLTMuNGw0LjgtOC4zYTQgNCAwIDAgMSAxLjMgM1ptLTQuMyAxOS4xaC0uNmw0LjktOC40djQuMmMwIDIuMy0yIDQuMy00LjMgNC4zWm0yLTI4LjRjLS4zLS44LTEtMS4zLTItMS4zaC0xLjlsLTIuNCA0LjNIMzBsMS43LTNabS0zIDVoLTQuNkwxMC42IDI5LjhoNC43TDI4LjggNi40Wk0xOC43IDBoNC42bC0yLjUgNC4zaC00LjZMMTguNiAwWk0xNSA2LjRoNC42TDYgMjkuOEg0LjJjLS44IDAtMS43LS4zLTIuNC0uOEwxNSA2LjRaTTE0IDBIOS40TDcgNC4zaDQuNkwxNCAwWm0tMy42IDYuNEg1LjdMMCAxNi4ydjhMMTAuMyA2LjRaTTQuMyAwaC40TDAgOC4ydi00QzAgMiAxLjkgMCA0LjMgMFoiIGZpbGw9IiM5OTQ1RkYiLz48L3N2Zz4=';
  supportedTransactionVersions: ReadonlySet<TransactionVersion> = new Set(['legacy', 0]);

  private _keypair: Keypair | null = null;

  get connecting() {
    return false;
  }

  get publicKey() {
    return this._keypair?.publicKey ?? null;
  }

  get readyState() {
    return WalletReadyState.Loadable;
  }

  async connect(): Promise<void> {
    this._keypair = loadOrCreateDevKeypair();
    this.emit('connect', this._keypair.publicKey);
  }

  async disconnect(): Promise<void> {
    this._keypair = null;
    this.emit('disconnect');
  }

  async signTransaction<T extends Transaction | VersionedTransaction>(transaction: T): Promise<T> {
    if (!this._keypair) throw new WalletNotConnectedError();

    if (isVersionedTransaction(transaction)) {
      transaction.sign([this._keypair]);
    } else {
      transaction.partialSign(this._keypair);
    }

    return transaction;
  }

  async signMessage(message: Uint8Array): Promise<Uint8Array> {
    if (!this._keypair) throw new WalletNotConnectedError();
    return ed25519.sign(message, this._keypair.secretKey.slice(0, 32));
  }

  async signIn(input: SolanaSignInInput = {}): Promise<SolanaSignInOutput> {
    if (!this._keypair) {
      await this.connect();
    }

    const { publicKey, secretKey } = this._keypair!;
    const domain = input.domain || window.location.host;
    const address = input.address || publicKey.toBase58();

    const signedMessage = createSignInMessage({
      ...input,
      domain,
      address,
    });
    const signature = ed25519.sign(signedMessage, secretKey.slice(0, 32));

    this.emit('connect', publicKey);

    return {
      account: {
        address,
        publicKey: publicKey.toBytes(),
        chains: [],
        features: [],
      },
      signedMessage,
      signature,
    };
  }
}
