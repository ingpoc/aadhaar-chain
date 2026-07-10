#!/usr/bin/env node
/**
 * Deterministic portfolio grader driven by test-ledger.json
 */
import { spawnSync } from 'node:child_process';
import { createHash, randomBytes } from 'node:crypto';
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = join(__dirname, '..');
const LEDGER = JSON.parse(readFileSync(join(ROOT, 'test-ledger.json'), 'utf8'));
const ARTIFACTS = join(ROOT, 'artifacts');
mkdirSync(ARTIFACTS, { recursive: true });

const SERVICES = LEDGER.services;

function base58Encode(buffer) {
  const ALPHABET = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz';
  let zeros = 0;
  while (zeros < buffer.length && buffer[zeros] === 0) zeros += 1;
  const digits = [0];
  for (let i = zeros; i < buffer.length; i += 1) {
    let carry = buffer[i];
    for (let j = 0; j < digits.length; j += 1) {
      carry += digits[j] << 8;
      digits[j] = carry % 58;
      carry = (carry / 58) | 0;
    }
    while (carry > 0) {
      digits.push(carry % 58);
      carry = (carry / 58) | 0;
    }
  }
  let out = '1'.repeat(zeros);
  for (let i = digits.length - 1; i >= 0; i -= 1) out += ALPHABET[digits[i]];
  return out;
}

async function makeWallet() {
  // Prefer solders via python for real Solana keypairs (proof signing).
  const py = spawnSync(
    '/agent/repos/aadhaar-chain/gateway/.venv/bin/python',
    [
      '-c',
      [
        'from solders.keypair import Keypair',
        'import json',
        'kp=Keypair()',
        'print(json.dumps({"wallet": str(kp.pubkey()), "secret": list(bytes(kp))}))',
      ].join(';'),
    ],
    { encoding: 'utf8' },
  );
  if (py.status !== 0) {
    throw new Error(`Failed to create Solana keypair: ${py.stderr}`);
  }
  const parsed = JSON.parse(py.stdout.trim());
  return {
    wallet: parsed.wallet,
    secret: Uint8Array.from(parsed.secret),
    signMessage(message) {
      const script = [
        'from solders.keypair import Keypair',
        'import sys, json',
        'secret=bytes(json.loads(sys.argv[1]))',
        'msg=sys.argv[2].encode("utf-8")',
        'kp=Keypair.from_bytes(secret)',
        'sig=kp.sign_message(msg)',
        'print(str(sig))',
      ].join(';');
      const res = spawnSync(
        '/agent/repos/aadhaar-chain/gateway/.venv/bin/python',
        ['-c', script, JSON.stringify([...this.secret]), message],
        { encoding: 'utf8' },
      );
      if (res.status !== 0) throw new Error(res.stderr);
      return res.stdout.trim();
    },
  };
}

async function http(serviceKey, method, path, { json, headers, expectStatus } = {}) {
  const service = SERVICES[serviceKey];
  if (!service) throw new Error(`Unknown service ${serviceKey}`);
  const url = `${service.url}${path}`;
  const init = {
    method,
    headers: {
      ...(json ? { 'Content-Type': 'application/json' } : {}),
      ...(headers || {}),
    },
    body: json ? JSON.stringify(json) : undefined,
  };
  const res = await fetch(url, init);
  const text = await res.text();
  let body;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = text;
  }
  if (expectStatus !== undefined) {
    const allowed = Array.isArray(expectStatus) ? expectStatus : [expectStatus];
    if (!allowed.includes(res.status)) {
      throw new Error(`${method} ${url} expected ${allowed} got ${res.status}: ${text.slice(0, 300)}`);
    }
  }
  return { status: res.status, body, text, url };
}

function runCommand(command, cwd) {
  const res = spawnSync(command, {
    cwd,
    shell: true,
    encoding: 'utf8',
    env: process.env,
    timeout: 600000,
  });
  return {
    exitCode: res.status ?? 1,
    stdout: res.stdout || '',
    stderr: res.stderr || '',
  };
}

function deepHasForbidden(obj, keys) {
  const found = [];
  const walk = (v, path) => {
    if (!v || typeof v !== 'object') return;
    for (const [k, val] of Object.entries(v)) {
      if (keys.includes(k)) found.push(`${path}.${k}`);
      walk(val, `${path}.${k}`);
    }
  };
  walk(obj, '$');
  return found;
}

const results = [];
let walletCtx = null;
let fwToken = null;

async function ensureWallet() {
  if (!walletCtx) walletCtx = await makeWallet();
  return walletCtx;
}

async function seedFixture(state, documentType = 'aadhaar') {
  const { wallet } = await ensureWallet();
  // Use a fresh wallet per fixture when state is no_identity to avoid residue,
  // otherwise reuse and overwrite via fixture endpoint.
  if (state === 'no_identity') {
    walletCtx = await makeWallet();
  }
  const w = (await ensureWallet()).wallet;
  await http('aadhaar_chain_gateway', 'POST', `/api/identity/dev/fixtures/${w}`, {
    json: { fixture_state: state, document_type: documentType },
    expectStatus: 200,
  });
  return w;
}

async function gradeAcHealth() {
  const res = await http('aadhaar_chain_gateway', 'GET', '/health');
  if (res.status !== 200) throw new Error(`health ${res.status}`);
  return { ok: true, body: res.body };
}

async function gradeAcIdentityCreate() {
  const { wallet } = await ensureWallet();
  const commitment = `sha256:${createHash('sha256').update(`${wallet}:qa`).digest('hex')}`;
  await http('aadhaar_chain_gateway', 'POST', `/api/identity/${wallet}`, {
    json: { commitment },
    expectStatus: [200, 201, 409],
  });
  const identity = await http('aadhaar_chain_gateway', 'GET', `/api/identity/${wallet}`, {
    expectStatus: 200,
  });
  const trust = await http('aadhaar_chain_gateway', 'GET', `/api/identity/${wallet}/trust`, {
    expectStatus: 200,
  });
  const trustState = trust.body?.data?.trust_state;
  if (!identity.body?.data) throw new Error('identity missing');
  if (!['identity_present_unverified', 'verified', 'manual_review', 'revoked_or_blocked'].includes(trustState)) {
    throw new Error(`unexpected trust_state ${trustState}`);
  }
  return { ok: true, trust_state: trustState, wallet };
}

async function gradeAcFixtureMatrix() {
  const states = LEDGER.test_wallet.fixture_states;
  const outcomes = [];
  for (const state of states) {
    const w = await seedFixture(state);
    const trust = await http('aadhaar_chain_gateway', 'GET', `/api/identity/${w}/trust`, {
      expectStatus: 200,
    });
    const got = trust.body?.data?.trust_state ?? (state === 'no_identity' ? 'no_identity' : null);
    // no_identity fixture may clear identity; trust endpoint might 404 or return no_identity
    if (state === 'no_identity') {
      const identity = await http('aadhaar_chain_gateway', 'GET', `/api/identity/${w}`, {
        expectStatus: 200,
      });
      if (identity.body?.data) {
        // Some implementations still return trust surface; accept either null identity or no_identity state
        if (got && got !== 'no_identity') throw new Error(`no_identity fixture left state ${got}`);
      }
      outcomes.push({ state, got: got || 'no_identity', wallet: w });
      continue;
    }
    if (got !== state) throw new Error(`fixture ${state} produced ${got}`);
    outcomes.push({ state, got, wallet: w });
  }
  return { ok: true, outcomes };
}

async function gradeAcProofRequiresVerified() {
  await seedFixture('identity_present_unverified');
  const { wallet } = await ensureWallet();
  const denied = await http(
    'aadhaar_chain_gateway',
    'POST',
    `/api/identity/${wallet}/proof-token`,
    {
      json: { audience: 'buyer', purpose: 'buyer_checkout_identity_proof' },
    },
  );
  if (![400, 403, 409, 422].includes(denied.status)) {
    throw new Error(`expected denial for unverified, got ${denied.status}`);
  }
  await seedFixture('verified');
  const { wallet: w2 } = await ensureWallet();
  const allowed = await http(
    'aadhaar_chain_gateway',
    'POST',
    `/api/identity/${w2}/proof-token`,
    {
      json: { audience: 'buyer', purpose: 'buyer_checkout_identity_proof' },
      expectStatus: 200,
    },
  );
  if (!allowed.body?.data?.token_id || !allowed.body?.data?.message) {
    throw new Error('proof token missing fields');
  }
  return { ok: true, token_id: allowed.body.data.token_id };
}

async function gradeAcProofSignVerify() {
  await seedFixture('verified');
  const kp = await ensureWallet();
  const issued = await http(
    'aadhaar_chain_gateway',
    'POST',
    `/api/identity/${kp.wallet}/proof-token`,
    {
      json: { audience: 'seller', purpose: 'seller_catalog_identity_proof' },
      expectStatus: 200,
    },
  );
  const token = issued.body.data;
  const signature = kp.signMessage(token.message);
  const verified = await http('aadhaar_chain_gateway', 'POST', '/api/identity/proof-token/verify', {
    json: {
      token_id: token.token_id,
      wallet_address: kp.wallet,
      audience: 'seller',
      message: token.message,
      signature,
    },
    expectStatus: 200,
  });
  if (!verified.body?.data?.valid) {
    throw new Error(`proof invalid: ${JSON.stringify(verified.body)}`);
  }
  return { ok: true, trust_state: verified.body.data.trust_state };
}

async function gradeAcNoRawPii() {
  await seedFixture('verified');
  const { wallet } = await ensureWallet();
  const trust = await http('aadhaar_chain_gateway', 'GET', `/api/identity/${wallet}/trust`, {
    expectStatus: 200,
  });
  const forbidden = [
    'aadhaar_number',
    'pan_number',
    'uid',
    'document_bytes',
    'ocr_raw',
    'full_name',
  ];
  const found = deepHasForbidden(trust.body, forbidden);
  if (found.length) throw new Error(`raw PII keys on trust surface: ${found.join(',')}`);
  return { ok: true };
}

async function gradeFwHealth() {
  const res = await http('flatwatch_backend', 'GET', '/api/health', { expectStatus: 200 });
  return { ok: true, body: res.body };
}

async function gradeFwAuth() {
  const res = await http('flatwatch_backend', 'POST', '/api/auth/login', {
    json: { email: 'resident@flatwatch.test', password: 'dev-local' },
    expectStatus: 200,
  });
  const token = res.body?.access_token || res.body?.token || res.body?.data?.access_token;
  if (!token) throw new Error(`no token in ${JSON.stringify(res.body).slice(0, 200)}`);
  fwToken = token;
  return { ok: true, role: res.body?.user?.role || res.body?.role || 'resident' };
}

async function gradeFwSync() {
  if (!fwToken) await gradeFwAuth();
  await http('flatwatch_backend', 'POST', '/api/transactions/sync', {
    headers: { Authorization: `Bearer ${fwToken}` },
    expectStatus: 200,
  });
  const summary = await http('flatwatch_backend', 'GET', '/api/transactions/summary', {
    headers: { Authorization: `Bearer ${fwToken}` },
    expectStatus: 200,
  });
  const data = summary.body?.data || summary.body;
  const keys = Object.keys(data || {});
  if (!keys.length) throw new Error('empty summary');
  return { ok: true, keys };
}

async function gradeFwTrustBypass() {
  // Root-cause probe: can resident upload receipt without verified wallet trust?
  if (!fwToken) await gradeFwAuth();
  const form = new FormData();
  const blob = new Blob([randomBytes(64)], { type: 'application/pdf' });
  form.append('file', blob, 'qa-probe.pdf');
  const res = await fetch(`${SERVICES.flatwatch_backend.url}/api/receipts/upload`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${fwToken}` },
    body: form,
  });
  const text = await res.text();
  // Current expected bug: 200/201 without trust. Record as fail for fix tracker.
  const bypassed = res.status >= 200 && res.status < 300;
  return {
    ok: !bypassed,
    status: res.status,
    bypassed,
    detail: text.slice(0, 300),
    note: bypassed
      ? 'ROOT CAUSE: receipt upload succeeds without AadhaarChain verified trust'
      : 'Server correctly rejected untrusted elevated write',
  };
}

async function gradeBuyerPolicy() {
  const cmd = runCommand('npm test -- --run src/lib/buyerActionPolicy.test.ts src/lib/trust.test.ts', '/agent/repos/ondc-buyer');
  if (cmd.exitCode !== 0) throw new Error(cmd.stderr || cmd.stdout.slice(-500));
  return { ok: true };
}

async function gradeSellerPolicy() {
  const cmd = runCommand('npm test -- --run src/lib/sellerActionPolicy.test.ts src/lib/trust.test.ts', '/agent/repos/ondc-seller');
  if (cmd.exitCode !== 0) throw new Error(cmd.stderr || cmd.stdout.slice(-500));
  return { ok: true };
}

async function gradeBuyerUnit() {
  const cmd = runCommand('npm test', '/agent/repos/ondc-buyer');
  if (cmd.exitCode !== 0) throw new Error(cmd.stderr || cmd.stdout.slice(-800));
  return { ok: true, tail: cmd.stdout.slice(-200) };
}

async function gradeSellerUnit() {
  const cmd = runCommand('npm test', '/agent/repos/ondc-seller');
  if (cmd.exitCode !== 0) throw new Error(cmd.stderr || cmd.stdout.slice(-800));
  return { ok: true, tail: cmd.stdout.slice(-200) };
}

async function gradeFwBackendSuite() {
  const cmd = runCommand(
    'PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q -p pytest_asyncio.plugin --asyncio-mode=auto',
    '/agent/repos/flatwatch/backend',
  );
  if (cmd.exitCode !== 0) throw new Error(cmd.stderr || cmd.stdout.slice(-800));
  return { ok: true, tail: cmd.stdout.slice(-200) };
}

async function gradeFwFrontendSuite() {
  const cmd = runCommand('npm test -- --runInBand', '/agent/repos/flatwatch/frontend');
  if (cmd.exitCode !== 0) throw new Error(cmd.stderr || cmd.stdout.slice(-800));
  return { ok: true, tail: cmd.stdout.slice(-200) };
}

async function gradeAcGatewaySuite() {
  const cmd = runCommand(
    'PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q',
    '/agent/repos/aadhaar-chain/gateway',
  );
  if (cmd.exitCode !== 0) throw new Error(cmd.stderr || cmd.stdout.slice(-800));
  return { ok: true, tail: cmd.stdout.slice(-200) };
}

async function gradeSellerCrossAppGap() {
  const buyer = readFileSync('/agent/repos/ondc-buyer/src/lib/mockSearch.ts', 'utf8');
  const seller = readFileSync('/agent/repos/ondc-seller/src/lib/mockCatalog.ts', 'utf8');
  const buyerOrders = readFileSync('/agent/repos/ondc-buyer/src/lib/localOrders.ts', 'utf8');
  const sellerOrders = readFileSync('/agent/repos/ondc-seller/src/lib/localSellerOrders.ts', 'utf8');
  const skuAligned =
    buyer.includes('basmati-rice-5kg') &&
    seller.includes('basmati-rice-5kg') &&
    buyer.includes('mustard-oil-1l') &&
    seller.includes('mustard-oil-1l');
  const bridgePresent =
    buyerOrders.includes('ondc-portfolio-demo-orders') &&
    sellerOrders.includes('ondc-portfolio-demo-orders');
  return {
    ok: skuAligned && bridgePresent,
    skuAligned,
    bridgePresent,
    note:
      skuAligned && bridgePresent
        ? 'Demo SKUs aligned and buyer→seller order bridge present'
        : 'Demo catalog/order bridge still incomplete',
  };
}

const GRADERS = {
  'ac.health': gradeAcHealth,
  'ac.identity_create_api': gradeAcIdentityCreate,
  'ac.fixture_matrix': gradeAcFixtureMatrix,
  'ac.proof_token_requires_verified': gradeAcProofRequiresVerified,
  'ac.proof_token_sign_verify': gradeAcProofSignVerify,
  'ac.no_raw_pii_on_trust_surface': gradeAcNoRawPii,
  'ac.gateway_suite': gradeAcGatewaySuite,
  'buyer.policy_matrix': gradeBuyerPolicy,
  'buyer.unit_suite': gradeBuyerUnit,
  'seller.policy_matrix': gradeSellerPolicy,
  'seller.unit_suite': gradeSellerUnit,
  'seller.cross_app_demo_gap': gradeSellerCrossAppGap,
  'fw.health': gradeFwHealth,
  'fw.auth_login': gradeFwAuth,
  'fw.sync_and_summary': gradeFwSync,
  'fw.trust_api_bypass_root_cause': gradeFwTrustBypass,
  'fw.backend_suite': gradeFwBackendSuite,
  'fw.frontend_suite': gradeFwFrontendSuite,
};

const only = process.argv.slice(2);
const selected = only.length ? only : Object.keys(GRADERS);

for (const id of selected) {
  const fn = GRADERS[id];
  if (!fn) {
    results.push({ id, status: 'skip', error: 'no grader' });
    continue;
  }
  const started = Date.now();
  try {
    const detail = await fn();
    results.push({
      id,
      status: detail.ok === false ? 'fail' : 'pass',
      ms: Date.now() - started,
      detail,
    });
    console.log(`${detail.ok === false ? 'FAIL' : 'PASS'} ${id}`);
  } catch (err) {
    results.push({
      id,
      status: 'fail',
      ms: Date.now() - started,
      error: String(err?.message || err),
    });
    console.error(`FAIL ${id}:`, err?.message || err);
  }
}

const report = {
  generated_at: new Date().toISOString(),
  pass: results.filter((r) => r.status === 'pass').length,
  fail: results.filter((r) => r.status === 'fail').length,
  results,
};
writeFileSync(join(ARTIFACTS, 'deterministic-report.json'), JSON.stringify(report, null, 2));
console.log(`\nDeterministic: ${report.pass} pass / ${report.fail} fail`);
process.exit(report.fail ? 1 : 0);
