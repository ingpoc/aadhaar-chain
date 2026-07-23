import { Connection, Transaction } from '@solana/web3.js';

/**
 * Deserialize a gateway-provided base64 legacy transaction, sign with the wallet,
 * and submit to the configured Solana RPC.
 */
export async function submitGatewayUnsignedTransaction(
  connection: Connection,
  unsignedTransactionBase64: string,
  signTransaction: (transaction: Transaction) => Promise<Transaction>,
): Promise<string> {
  const transaction = Transaction.from(Buffer.from(unsignedTransactionBase64, 'base64'));
  const signed = await signTransaction(transaction);
  const signature = await connection.sendRawTransaction(signed.serialize(), {
    skipPreflight: false,
    preflightCommitment: 'confirmed',
  });
  const { blockhash, lastValidBlockHeight } = await connection.getLatestBlockhash('confirmed');
  await connection.confirmTransaction(
    { signature, blockhash, lastValidBlockHeight },
    'confirmed',
  );
  return signature;
}
