/**
 * E2E test: Simple sending (direct, non-collaborative).
 *
 * Verifies that a user with a funded wallet can send bitcoin to an
 * address using the direct send (non-CoinJoin) flow.
 */

import { test, expect, loginViaUI, dismissDialogs } from "../fixtures";
import * as bitcoinRpc from "../fixtures/bitcoin-rpc";

test.describe("Direct Send", () => {
  test("send bitcoin via UI (non-collaborative)", async ({
    page,
    fundedWallet,
    walletApi,
  }) => {
    // Log in with the funded wallet.
    await loginViaUI(page, fundedWallet.walletName, fundedWallet.password);

    // Stop the maker if it is running — when the maker is active the send form
    // shows "Under construction" and all inputs are disabled.
    await walletApi.stopMaker(fundedWallet.token).catch(() => undefined);
    await loginViaUI(page, fundedWallet.walletName, fundedWallet.password);

    // Navigate to the Send tab directly to avoid matching Cheatsheet links.
    await page.goto("/send", { waitUntil: "domcontentloaded", timeout: 15_000 });

    // Dismiss the Cheatsheet dialog which opens on every page navigation.
    await dismissDialogs(page);

    await expect(page.getByText("Send from")).toBeVisible({
      timeout: 15_000,
    });

    // Select Jar 0 (Apricot) which has funds from global-setup.
    // JAM labels jars with fruit names (Apricot=0, Blueberry=1, ...).
    // Use force:true to bypass any lingering Radix backdrop.
    const jars = page.locator("button").filter({ hasText: /Apricot/i });
    await jars.first().click({ force: true });

    // Dismiss any sidebar/dialog that may have opened on navigation or jar select.
    await dismissDialogs(page);

    // Generate a destination address from the Bitcoin Core wallet.
    const destinationAddress = await bitcoinRpc.rpc<string>("getnewaddress");

    // Wait for the destination field to become enabled (it's disabled until
    // a jar is selected and the wallet display finishes loading).
    const destInput = page.locator("#send-destination");
    await expect(destInput).toBeEnabled({ timeout: 10_000 });

    // Fill in the destination address.
    await destInput.fill(destinationAddress);

    // Fill in the amount (small amount: 50,000 sats = 0.0005 BTC).
    await page.locator("#send-amount").fill("50000");

    // Disable CoinJoin: dismiss any overlay that opened during form filling,
    // then expand "Sending options" accordion and toggle the switch.
    await dismissDialogs(page);

    // The accordion trigger is a <button> inside the <h3> heading.
    await page
      .getByRole("heading", { name: "Sending options" })
      .getByRole("button")
      .click();

    const cjSwitch = page.locator("#switch-is-collaborative-transaction");
    await expect(cjSwitch).toBeVisible({ timeout: 10_000 });
    if (await cjSwitch.isChecked()) {
      await cjSwitch.click();
    }

    // After disabling CoinJoin the button label changes to "Send without privacy".
    await page
      .getByRole("button", { name: /Send without privacy/i })
      .click({ force: true });

    // A confirmation dialog appears for non-collaborative sends.
    await expect(page.getByText("Confirm payment")).toBeVisible({ timeout: 10_000 });

    await page.screenshot({ path: "test-results/send-confirmation.png", fullPage: true });

    await page.getByRole("button", { name: "Confirm" }).click();

    // After a successful send a success alert appears.
    // The form does not auto-reset, so we wait for the success message.
    await expect(
      page.getByText(/successfully sent/i).first(),
    ).toBeVisible({ timeout: 30_000 });

    await bitcoinRpc.mineBlocks(1);
    await page.screenshot({ path: "test-results/send-completed.png", fullPage: true });
  });

  test("send bitcoin via UI (collaborative)", async ({
    page,
    fundedWallet,
    walletApi,
    bitcoinRpc,
  }) => {
    const { token } = fundedWallet;

    await loginViaUI(page, fundedWallet.walletName, fundedWallet.password);

    // The send form is disabled while maker is active.
    await walletApi.stopMaker(token).catch(() => undefined);
    // Ensure no leftover taker run from a previous failed test.
    await walletApi.stopCoinjoin(token).catch(() => undefined);

    await page.goto("/send", { waitUntil: "domcontentloaded", timeout: 15_000 });
    await dismissDialogs(page);

    await expect(page.getByText("Send from")).toBeVisible({ timeout: 15_000 });

    // Use Apricot (jar 0) which is funded by global setup.
    await page
      .locator("button")
      .filter({ hasText: /Apricot/i })
      .first()
      .click({ force: true });

    await dismissDialogs(page);

    const destinationAddress = await bitcoinRpc.rpc<string>("getnewaddress");

    const destInput = page.locator("#send-destination");
    await expect(destInput).toBeEnabled({ timeout: 10_000 });
    await destInput.fill(destinationAddress);
    await page.locator("#send-amount").fill("50000");

    await dismissDialogs(page);
    await page
      .getByRole("heading", { name: "Sending options" })
      .getByRole("button")
      .click();

    const cjSwitch = page.locator("#switch-is-collaborative-transaction");
    await expect(cjSwitch).toBeVisible({ timeout: 10_000 });
    if (!(await cjSwitch.isChecked())) {
      await cjSwitch.click();
    }

    // Keep collaborator count low so the regtest setup can always fill.
    const collaboratorsInput = page
      .locator('input[type="number"][placeholder="Other"]')
      .first();
    await expect(collaboratorsInput).toBeEnabled({ timeout: 10_000 });
    await collaboratorsInput.fill("1");
    await collaboratorsInput.blur();

    const sendBtn = page
      .getByRole("button", {
        name: /^(Send|Ignore warning\s*&\s*try send)$/i,
      })
      .first();
    await expect(sendBtn).toBeVisible({ timeout: 15_000 });
    await sendBtn.click({ force: true });

    await expect(page.getByText("Confirm payment")).toBeVisible({ timeout: 10_000 });
    await expect(page.getByText(/Payment with privacy improvement/i)).toBeVisible({
      timeout: 10_000,
    });
    await page.getByRole("button", { name: "Confirm" }).click();

    await walletApi.waitForSession(
      token,
      (s) => s.coinjoin_in_process === true,
      90_000,
      2_000,
    );
    await walletApi.waitForSession(
      token,
      (s) => s.coinjoin_in_process === false,
      240_000,
      3_000,
    );

    // Mine a block after completion so the environment is stable for later tests.
    await bitcoinRpc.mineBlocks(1);
  });

  test("send via API and verify balance change", async ({
    fundedWallet,
    walletApi,
    bitcoinRpc,
  }) => {
    const { token } = fundedWallet;
    // fundedWallet fixture already called waitForBalance, so we have a confirmed balance.

    // Trigger a UTXO refresh so the wallet display is current.
    await walletApi.getUtxos(token);
    await new Promise((r) => setTimeout(r, 3_000));

    // Snapshot the mixdepth-0 balance.  We compare this specific account
    // rather than the total balance because other tests mine coinbase
    // rewards to addresses in different mixdepths, and those immature
    // coinbases can mature between the "before" and "after" snapshots,
    // inflating the total unpredictably.
    const before = await walletApi.getWalletDisplay(token);
    const md0Before = before.walletinfo.accounts.find((a) => a.account === "0");
    expect(md0Before).toBeTruthy();
    const balanceBefore = parseFloat(md0Before!.account_balance);
    console.log(`[send.spec.ts] Mixdepth 0 balance before: ${balanceBefore}`);
    expect(balanceBefore).toBeGreaterThan(0);

    // Generate a destination address (external, not in the test wallet).
    const destAddr = await bitcoinRpc.rpc<string>("getnewaddress");

    // Send 100,000 sats (0.001 BTC) from mixdepth 0.
    const sendResult = await walletApi.directSend(
      token,
      0,
      destAddr,
      100_000,
    );
    expect(sendResult.txid).toBeTruthy();

    // Mine a block to confirm the transaction.
    await bitcoinRpc.mineBlocks(1);

    // Refresh and re-check.
    await walletApi.getUtxos(token);
    await new Promise((r) => setTimeout(r, 3_000));

    const after = await walletApi.getWalletDisplay(token);
    const md0After = after.walletinfo.accounts.find((a) => a.account === "0");
    expect(md0After).toBeTruthy();
    const balanceAfter = parseFloat(md0After!.account_balance);
    console.log(`[send.spec.ts] Mixdepth 0 balance after: ${balanceAfter}`);
    expect(balanceAfter).toBeLessThan(balanceBefore);
  });
});
