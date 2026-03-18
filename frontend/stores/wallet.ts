import { create } from 'zustand';
import { Connection, PublicKey, LAMPORTS_PER_SOL } from '@solana/web3.js';

export interface WalletState {
  address: string | null;
  balance: number;
  isConnected: boolean;
  setAddress: (address: string | null) => void;
  setBalance: (balance: number) => void;
  setConnected: (connected: boolean) => void;
  fetchBalance: (connection: Connection, publicKey: PublicKey) => Promise<void>;
}

export const useWalletStore = create<WalletState>((set) => ({
  address: null,
  balance: 0,
  isConnected: false,

  setAddress: (address) => set({ address }),

  setBalance: (balance) => set({ balance }),

  setConnected: (isConnected) => set({ isConnected }),

  fetchBalance: async (connection, publicKey) => {
    try {
      const balance = await connection.getBalance(publicKey);
      set({ balance: balance / LAMPORTS_PER_SOL });
    } catch (error) {
      console.error('Failed to fetch balance:', error);
      set({ balance: 0 });
    }
  },
}));
