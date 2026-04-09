/**
 * E2E test: Wallet creation flow.
 *
 * Verifies that a user can create a new wallet through the jam-ng UI,
 * confirm their seed phrase, verify the mnemonic, and land on the main
 * wallet page.
 */

import { test, expect, loadCredentials } from "../fixtures";

test.describe("Wallet Creation", () => {
  test("create a new wallet via UI", async ({ page, walletApi }) => {
    const walletName = `pw-create-${Date.now()}`;
    const password = "testpass-create-123";

    // Lock the shared test wallet so we can create a new one.
    // jmwalletd only allows one wallet open at a time.
    const creds = loadCredentials();
    await walletApi.lockWallet(creds.token);

    // Navigate to the create wallet page.
    await page.goto("/create-wallet");
    await expect(page.getByText("Create Wallet")).toBeVisible();

    // Step 1: Fill in wallet details.
    await page.locator("#create-wallet-name").fill(walletName);
    await page.locator("#create-password").fill(password);
    await page.locator("#create-confirm-password").fill(password);

    // Submit the form.
    await page.getByRole("button", { name: "Create" }).click();

    // Step 2: Seed confirmation page should appear.
    await expect(
      page.getByText("Wallet created successfully!"),
    ).toBeVisible({ timeout: 30_000 });

    // Toggle to reveal the seed phrase.
    await page.locator("#switch-reveal-seed").click();

    // Verify the seed phrase is displayed (12 or 24 words).
    await expect(page.getByText("Seed Phrase").first()).toBeVisible();

    // Extract the seed words from the SeedPhraseGrid before proceeding.
    // Each word is rendered inside a grid cell as:
    //   <div class="bg-background ..."><span>N.</span><span>word</span></div>
    // We extract the word text from each cell by taking the last <span>.
    const seedCells = page.locator(".grid.font-mono .bg-background");
    const seedWords: string[] = [];
    const cellCount = await seedCells.count();
    for (let i = 0; i < cellCount; i++) {
      const wordSpan = seedCells.nth(i).locator("span").last();
      const word = await wordSpan.textContent();
      if (word && word.trim()) {
        seedWords.push(word.trim());
      }
    }
    console.log(`[wallet-creation] Extracted ${seedWords.length} seed words`);
    expect(seedWords.length).toBeGreaterThanOrEqual(12);

    // Confirm the backup.
    await page.locator("#switch-confirm-backup").click();

    // Click "Next" to proceed to the mnemonic verification step.
    await page.getByRole("button", { name: "Next" }).click();

    // Step 3: Verify mnemonic — click each word in the correct order.
    await expect(
      page.getByText("Verify Mnemonic Phrase"),
    ).toBeVisible({ timeout: 10_000 });

    // The shuffled words are shown as buttons. For each word in the original
    // mnemonic order, find a matching button (not yet picked) and click it.
    for (const word of seedWords) {
      // Find all non-disabled buttons with this word text.
      const wordBtn = page
        .getByRole("button", { name: word, exact: true })
        .filter({ hasNot: page.locator("[disabled]") })
        .first();
      await wordBtn.click();
      // Brief pause to allow UI animation/state updates.
      await page.waitForTimeout(100);
    }

    // All words selected — click "Fund Wallet" to complete.
    await page.getByRole("button", { name: "Fund Wallet" }).click();

    // Should be redirected to the main wallet page.
    await page.waitForURL("/", { timeout: 30_000 });
    await expect(page.getByText("Wallet distribution")).toBeVisible({
      timeout: 15_000,
    });

    // Take a screenshot for CI verification.
    await page.screenshot({
      path: "test-results/wallet-created.png",
      fullPage: true,
    });
  });

  test("wallet appears in login list after creation", async ({
    page,
    walletApi,
  }) => {
    const creds = loadCredentials();

    // Unlock the shared test wallet — this implicitly locks any currently-open
    // wallet (jmwalletd only allows one at a time) and gives us a valid token.
    // The previous test may have left a UI-created wallet open whose token we
    // don't have, so we can't lock it directly; force-unlocking our known
    // wallet is the safest way to take over the session.
    const unlocked = await walletApi.forceUnlock(creds.walletName, creds.password);
    const currentToken = unlocked.token;

    // Lock it so we can create a new wallet next.
    await walletApi.lockWallet(currentToken);

    // Create a new wallet via API.
    const walletName = `pw-list-${Date.now()}.jmdat`;
    const password = "testpass-list-123";
    const created = await walletApi.createWallet(walletName, password);
    console.log(`[wallet-creation] Created wallet, token length: ${created.token?.length}`);

    // Wait briefly for the backend to propagate auth state
    await page.waitForTimeout(1000);

    // Lock it so we can test the login page.
    await walletApi.lockWallet(created.token);


    // Navigate to the login page.
    await page.goto("/login");
    await expect(page.getByText("Welcome to Jam")).toBeVisible();

    // The JAM intro dialog may appear on top of the login form. Dismiss it
    // so the combobox is accessible.
    // Import dismissDialogs-like logic inline.
    const DISMISS_LABELS = ["Skip intro", "Close", "Get started", "Ok"];
    for (let i = 0; i < 6; i++) {
      await page.waitForTimeout(400);
      const dialog = page.locator('[role="dialog"]:visible').first();
      if (!(await dialog.isVisible().catch(() => false))) break;
      let dismissed = false;
      for (const label of DISMISS_LABELS) {
        const btn = page.getByRole("button", { name: label }).first();
        if (await btn.isVisible().catch(() => false)) {
          await btn.click();
          dismissed = true;
          break;
        }
      }
      if (!dismissed) await page.keyboard.press("Escape");
      await page
        .locator('[role="dialog"]:visible')
        .first()
        .waitFor({ state: "hidden", timeout: 2_000 })
        .catch(() => null);
    }

    // Open the wallet selector.
    const selectTrigger = page.locator('[role="combobox"]');
    await selectTrigger.click();

    // The wallet should appear in the list.
    const displayName = walletName.replace(".jmdat", "");
    await expect(
      page.getByRole("option", { name: displayName }),
    ).toBeVisible();

    // Re-unlock the shared test wallet for subsequent tests.
    await walletApi.unlockWallet(creds.walletName, creds.password);
  });
});
