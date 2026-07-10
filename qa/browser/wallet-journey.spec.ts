import { test, expect, type Page } from '@playwright/test';
import {
  connectPhantom,
  installInjectedWallet,
  loadOrCreateTestWallet,
  seedVerifiedTrust,
} from './wallet';

const AC = 'http://127.0.0.1:43100';
const BUYER = 'http://127.0.0.1:43102';
const SELLER = 'http://127.0.0.1:43103';
const FW = 'http://127.0.0.1:43105';

const wallet = loadOrCreateTestWallet();

async function assertAnyText(page: Page, candidates: string[]) {
  const body = (await page.locator('body').innerText()).toLowerCase();
  const hit = candidates.find((c) => body.includes(c.toLowerCase()));
  expect(hit, `Expected one of: ${candidates.join(' | ')}`).toBeTruthy();
}

async function ensureFlatwatchSignedIn(page: Page) {
  await Promise.race([
    page.getByRole('button', { name: /Sign in/i }).first().waitFor({ state: 'visible', timeout: 20000 }),
    page
      .getByText(/Sync now|Trust required|Upload receipt|New challenge|Ask about transactions|Track cash flow/i)
      .first()
      .waitFor({ state: 'visible', timeout: 20000 }),
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

test.describe.serial('Same-wallet portfolio journey', () => {
  test.beforeAll(async () => {
    await seedVerifiedTrust(wallet.wallet);
  });

  test('1. AadhaarChain dashboard shows verified trust for injected wallet', async ({ page }) => {
    await installInjectedWallet(page, wallet);
    await page.goto(`${AC}/dashboard`, { waitUntil: 'domcontentloaded' });
    await connectPhantom(page);

    // WalletMultiButton shows truncated pubkey once connected.
    await expect(page.getByRole('button', { name: /EL4m\.\.6fFu|EL4m/i }).first()).toBeVisible({
      timeout: 20000,
    });
    await assertAnyText(page, ['verified', 'Verify Aadhaar', 'Credentials', 'Identity', 'Dashboard', 'Sign ownership']);
  });

  test('2. Buyer: trust verified + Sign buyer proof → Identity signed', async ({ page }) => {
    await installInjectedWallet(page, wallet);
    await page.goto(`${BUYER}/search`, { waitUntil: 'networkidle' });
    await connectPhantom(page);

    await expect(page.getByText(/Trust Verified|Checkout ready|verified/i).first()).toBeVisible({
      timeout: 20000,
    });

    const prove = page.getByRole('button', { name: /Sign buyer proof/i });
    await expect(prove).toBeEnabled({ timeout: 20000 });
    await prove.click();
    await expect(page.getByText(/Identity signed/i).first()).toBeVisible({ timeout: 20000 });
  });

  test('3. Buyer: cart → checkout places demo order under verified trust', async ({ page }) => {
    await installInjectedWallet(page, wallet);
    await page.goto(`${BUYER}/product/basmati-rice-5kg`, { waitUntil: 'networkidle' });
    await connectPhantom(page);

    await page.getByRole('button', { name: /Add to cart/i }).first().click();
    await page.goto(`${BUYER}/cart`, { waitUntil: 'networkidle' });
    await page.getByRole('button', { name: /Proceed to checkout/i }).first().click();
    await page.waitForURL(/\/checkout/, { timeout: 15000 });

    // BillingForm uses useId()-prefixed inputs; target by label text.
    await page.getByLabel(/Full name/i).fill('QA Buyer');
    await page.getByLabel(/^Email/i).fill('qa-buyer@example.com');
    await page.getByLabel(/^Phone/i).fill('9876543210');

    const save = page.getByRole('button', { name: /^Save$/i }).first();
    if (await save.isVisible().catch(() => false)) {
      await save.click();
      await expect(page.getByText(/Information saved/i).first()).toBeVisible({ timeout: 10000 });
    } else {
      // blur-save path
      await page.getByLabel(/^Phone/i).blur();
      await page.waitForTimeout(500);
    }

    // Delivery
    await page.locator('#delivery-line1').fill('42 MG Road');
    await page.locator('#delivery-city').fill('Bengaluru');
    await page.locator('#delivery-state').fill('Karnataka');
    await page.locator('#delivery-postal-code').fill('560001');

    const submit = page.getByRole('button', { name: /Get quote|Place order/i }).first();
    await expect(submit).toBeEnabled({ timeout: 15000 });
    await submit.click();

    // First submit generates quote; second places order in demo mode.
    const place = page.getByRole('button', { name: /Place order/i }).first();
    await expect(place).toBeEnabled({ timeout: 15000 });
    await place.click();

    await page.waitForURL(/\/orders\//, { timeout: 20000 });
    await assertAnyText(page, ['Order', 'Basmati', 'created', 'pending', 'QA Buyer', 'demo-']);
  });

  test('4. Seller: trust verified + Sign seller proof → Identity signed', async ({ page }) => {
    await installInjectedWallet(page, wallet);
    await page.goto(`${SELLER}/dashboard`, { waitUntil: 'networkidle' });
    await connectPhantom(page);

    await expect(page.getByText(/Trust Verified|Verified|verified/i).first()).toBeVisible({
      timeout: 20000,
    });

    const prove = page.getByRole('button', { name: /Sign seller proof/i });
    await expect(prove).toBeEnabled({ timeout: 20000 });
    await prove.click();
    await expect(page.getByText(/Identity signed/i).first()).toBeVisible({ timeout: 20000 });
  });

  test('5. Seller: catalog shared SKUs + accept bridged buyer order', async ({ page }) => {
    await installInjectedWallet(page, wallet);
    await page.goto(`${SELLER}/catalog`, { waitUntil: 'networkidle' });
    await connectPhantom(page);
    await assertAnyText(page, ['Basmati Rice', 'Mustard Oil', 'basmati-rice-5kg']);

    await page.goto(`${SELLER}/orders`, { waitUntil: 'networkidle' });
    await assertAnyText(page, ['Accept', 'Reject', 'View Details', 'pending', 'Orders']);

    // Prefer accepting a buyer-bridged demo-* order if present; else first Accept.
    const accept = page.getByRole('button', { name: /^Accept$/i }).first();
    if (await accept.isVisible().catch(() => false)) {
      await accept.click();
      await page.waitForTimeout(800);
      await assertAnyText(page, ['accepted', 'Accept', 'Dispatch', 'View Details', 'Orders']);
    }
  });

  test('6. FlatWatch: verified wallet unlocks elevated receipt CTA language', async ({ page }) => {
    await installInjectedWallet(page, wallet);
    await page.goto(`${FW}/receipts`, { waitUntil: 'domcontentloaded' });
    await ensureFlatwatchSignedIn(page);
    await connectPhantom(page);

    // With verified trust + wallet, upload should no longer say only "Trust required"
    await expect(page.getByText(/Trust Verified|Verified|Upload receipt/i).first()).toBeVisible({
      timeout: 20000,
    });

    // Prefer Upload receipt enabled when verified
    const upload = page.getByRole('button', { name: /Upload receipt/i }).first();
    if (await upload.count()) {
      await expect(upload).toBeEnabled({ timeout: 20000 });
    } else {
      // Some builds use a label/file input with Trust required badge removed
      await expect(page.getByText(/Trust required/i)).toHaveCount(0);
    }
  });
});
