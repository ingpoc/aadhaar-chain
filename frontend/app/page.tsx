import { redirect } from 'next/navigation';

/** Apex landing is the static AgentGuard ONDC hub (Buyer + Seller links). */
export default function LandingPage() {
  redirect('/hub.html');
}
