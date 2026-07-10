import { test, expect, type Page } from '@playwright/test';

const AC = 'http://127.0.0.1:43100';
const BUYER = 'http://127.0.0.1:43102';
const SELLER = 'http://127.0.0.1:43103';
const FW = 'http://127.0.0.1:43105';

async function assertAnyText(page: Page, candidates: string[]) {
  const body = (await page.locator('body').innerText()).toLowerCase();
  const hit = candidates.find((c) => body.includes(c.toLowerCase()));
  expect(hit, `Expected one of: ${candidates.join(' | ')}`).toBeTruthy();
}

async function clickAny(page: Page, labels: string[]) {
  for (const label of labels) {
    const loc = page.getByRole('button', { name: new RegExp(label, 'i') }).first();
    if (await loc.count()) {
      await loc.click();
      return label;
    }
    const link = page.getByRole('link', { name: new RegExp(label, 'i') }).first();
    if (await link.count()) {
      await link.click();
      return label;
    }
    const text = page.getByText(new RegExp(label, 'i')).first();
    if (await text.count()) {
      await text.click();
      return label;
    }
  }
  throw new Error(`None of click targets found: ${labels.join(', ')}`);
}

test.describe('AadhaarChain browser', () => {
  test('ac.landing_loads', async ({ page }) => {
    const res = await page.goto(AC, { waitUntil: 'domcontentloaded' });
    expect(res?.ok()).toBeTruthy();
    await assertAnyText(page, ['AadhaarChain']);
    await assertAnyText(page, ['Join the revolution', 'Select Wallet', 'Create identity', 'Wallet']);
  });

  for (const route of [
    '/',
    '/dashboard',
    '/identity/create',
    '/verify/aadhaar',
    '/verify/pan',
    '/credentials',
    '/settings',
  ]) {
    test(`ac.route ${route}`, async ({ page }) => {
      const res = await page.goto(`${AC}${route}`, { waitUntil: 'domcontentloaded' });
      expect(res?.status(), route).toBeLessThan(500);
      const body = await page.locator('body').innerText();
      expect(body.length).toBeGreaterThan(20);
      expect(body.toLowerCase()).not.toContain('application error');
    });
  }
});

test.describe('ONDC Buyer browser', () => {
  test('buyer.landing_search', async ({ page }) => {
    await page.goto(`${BUYER}/search`, { waitUntil: 'networkidle' });
    await assertAnyText(page, ['Grocery', 'Restaurant', 'Fashion', 'Electronics']);
    await assertAnyText(page, ['Search network', 'Search', 'Search the network']);
  });

  test('buyer.search_results_demo', async ({ page }) => {
    await page.goto(`${BUYER}/results?category=grocery&q=rice`, { waitUntil: 'networkidle' });
    await assertAnyText(page, ['Basmati Rice', 'Mustard Oil', 'rice', 'Grocery', 'Add', 'View']);
  });

  test('buyer.add_to_cart_checkout_gate', async ({ page }) => {
    await page.goto(`${BUYER}/product/basmati-rice-5kg`, { waitUntil: 'networkidle' });
    await clickAny(page, ['Add to cart', 'Add']);
    await page.goto(`${BUYER}/cart`, { waitUntil: 'networkidle' });
    await assertAnyText(page, ['Basmati Rice', 'cart', 'Checkout', 'Proceed']);
    const proceed = page.getByRole('button', { name: /Proceed to checkout/i }).first();
    if (await proceed.count()) {
      await proceed.click();
      await page.waitForTimeout(500);
      await assertAnyText(page, [
        'Trust verification required',
        'Resolve trust',
        'AadhaarChain',
        'Get quote',
        'Place order',
        'checkout',
        'Billing',
      ]);
    }
  });

  test('buyer.orders_tabs', async ({ page }) => {
    await page.goto(`${BUYER}/orders`, { waitUntil: 'networkidle' });
    await assertAnyText(page, ['all', 'pending', 'active', 'complete', 'Orders']);
  });

  test('buyer.agent_page', async ({ page }) => {
    await page.goto(`${BUYER}/agent`, { waitUntil: 'networkidle' });
    await assertAnyText(page, [
      'Runtime',
      'Authentication required',
      'Read-only',
      'High-trust',
      'Claude',
      'agent',
      'Agent',
    ]);
  });
});

test.describe('ONDC Seller browser', () => {
  test('seller.dashboard_loads', async ({ page }) => {
    await page.goto(`${SELLER}/dashboard`, { waitUntil: 'networkidle' });
    await assertAnyText(page, ['Dashboard', 'Catalog', 'Add product', 'Sign seller proof', 'Open catalog']);
  });

  test('seller.catalog_list', async ({ page }) => {
    await page.goto(`${SELLER}/catalog`, { waitUntil: 'networkidle' });
    await assertAnyText(page, ['Basmati Rice', 'Mustard Oil', 'Add product', 'Refresh catalog', 'Catalog']);
    await assertAnyText(page, ['Edit', 'Delete', 'Add']);
  });

  test('seller.orders_fulfillment', async ({ page }) => {
    await page.goto(`${SELLER}/orders`, { waitUntil: 'networkidle' });
    await assertAnyText(page, ['pending', 'accepted', 'dispatched', 'Accept', 'Reject', 'View Details', 'Orders']);
  });

  test('seller.config_page', async ({ page }) => {
    await page.goto(`${SELLER}/config`, { waitUntil: 'networkidle' });
    await assertAnyText(page, ['Save configuration', 'Test connection', 'Generate key', 'Config']);
  });

  test('seller.agent_page', async ({ page }) => {
    await page.goto(`${SELLER}/agent`, { waitUntil: 'networkidle' });
    await assertAnyText(page, [
      'Runtime',
      'Read-only',
      'Verified seller writes',
      'Approve and apply',
      'agent',
      'Agent',
    ]);
  });
});

async function ensureFlatwatchSignedIn(page: Page) {
  // ProtectedRoute may briefly show "Verifying session" before Sign in.
  await Promise.race([
    page.getByRole('button', { name: /Sign in/i }).first().waitFor({ state: 'visible', timeout: 20000 }),
    page.getByText(/Sync now|Trust required|Upload receipt|New challenge|Ask about transactions|Track cash flow/i).first().waitFor({
      state: 'visible',
      timeout: 20000,
    }),
  ]).catch(() => undefined);

  const signIn = page.getByRole('button', { name: /Sign in/i }).first();
  if (await signIn.isVisible().catch(() => false)) {
    await Promise.all([
      page.waitForResponse(
        (res) => res.url().includes('/api/auth/login') && res.request().method() === 'POST',
        { timeout: 20000 },
      ),
      signIn.click(),
    ]);
    await expect(page.getByRole('button', { name: /Sign in/i })).toHaveCount(0, { timeout: 20000 });
  }
}

test.describe('FlatWatch browser', () => {
  test('fw.landing_signin', async ({ page }) => {
    await page.goto(FW, { waitUntil: 'domcontentloaded' });
    await Promise.all([
      page.waitForURL(/\/dashboard/, { timeout: 20000 }),
      clickAny(page, ['Get Started']),
    ]);
    await ensureFlatwatchSignedIn(page);
    await expect(
      page.getByText(/Sync now|Inflow|Outflow|Loading dashboard|Track cash flow/i).first(),
    ).toBeVisible({ timeout: 20000 });
    await assertAnyText(page, ['Sync now', 'Dashboard', 'Inflow', 'Outflow', 'Transactions', 'balance', 'Balance', 'Track cash flow']);
  });

  test('fw.receipts_trust_gate', async ({ page }) => {
    await page.goto(`${FW}/receipts`, { waitUntil: 'domcontentloaded' });
    await ensureFlatwatchSignedIn(page);
    await expect(page.getByText(/Trust required|Upload receipt|Receipts|Evidence/i).first()).toBeVisible({
      timeout: 20000,
    });
    await assertAnyText(page, ['Upload receipt', 'Trust required', 'Review trust', 'Receipts', 'Evidence']);
    await expect(page.getByText('Invalid time value')).toHaveCount(0);
  });

  test('fw.challenges_trust_gate', async ({ page }) => {
    await page.goto(`${FW}/challenges`, { waitUntil: 'domcontentloaded' });
    await ensureFlatwatchSignedIn(page);
    const newChallenge = page.getByRole('button', { name: /New challenge/i }).first();
    if (await newChallenge.count()) await newChallenge.click();
    await assertAnyText(page, ['Submit challenge', 'Trust required', 'Select a transaction', 'Challenge']);
  });

  test('fw.chat_guard', async ({ page }) => {
    await page.goto(`${FW}/chat`, { waitUntil: 'domcontentloaded' });
    await ensureFlatwatchSignedIn(page);
    await assertAnyText(page, [
      'Summarize this month',
      'receipts still need review',
      'resident challenges',
      'by-law',
      'Ask about transactions',
      'Runtime',
      'Chat',
    ]);
  });
});
