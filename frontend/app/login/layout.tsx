import type { Metadata } from 'next';
import type { ReactNode } from 'react';

export const metadata: Metadata = {
  title: 'Sign in — AadhaarChain',
  description: 'Connect your wallet and sign in to portfolio apps with AadhaarChain SSO.',
};

export default function LoginLayout({ children }: { children: ReactNode }) {
  return children;
}
