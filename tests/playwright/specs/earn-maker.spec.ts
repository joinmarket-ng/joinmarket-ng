/**
 * E2E test: Start maker and verify offer loads.
 *
 * This test validates the fix for the "Loading offer..." forever bug.
 * After starting the maker bot, the offer should appear in the session
 * and the Earn tab should display the offer details instead of showing
 * "Loading offer..." indefinitely.
 */

import { test, expect, loginViaUI, dismissDialogs } from "../fixtures";

test.describe("Maker / Earn", () => {
  test("start maker via UI and verify offer is displayed", async ({
    page,
    fundedWallet,
    walletApi,
  }) => {
    // Log in with the funded wallet.
    await loginViaUI(page, fundedWallet.walletName, fundedWallet.password);

    // Stop the maker if it is already running (e.g. from a previous test run),
    // otherwise the earn form inputs will be disabled.
    await walletApi.stopMaker(fundedWallet.token).catch(() => undefined);

    // Navigate to the Earn tab directly to avoid matching Cheatsheet links.
    await page.goto("/earn", { waitUntil: "domcontentloaded", timeout: 15_000 });

    // Dismiss the Cheatsheet dialog which opens on every page navigation.
    await dismissDialogs(page);

    // Bondless makers can only publish the default free offer. The minimum
    // amount is available under the collapsed Earn options section.
    await page.getByRole("button", { name: "Earn options" }).click();
    const minAmountInput = page.locator("#offerMinAmount");
    await expect(minAmountInput).toBeEnabled({ timeout: 20_000 });
    await minAmountInput.fill("100000");

    await page.getByRole("button", { name: "Start", exact: true }).click();

    // The maker should start. We might briefly see "Waiting for maker
    // to start..." or "Loading offer..." but it should resolve.

    // BUG FIX VALIDATION: The offer card should appear within a
    // reasonable time, NOT stay at "Loading offer..." forever.
    await expect(page.getByText("Offer Id")).toBeVisible({
      timeout: 90_000,
    });

    // Verify the offer card displays the expected details.
    await expect(page.getByText("absolute", { exact: true })).toBeVisible();
    await expect(page.getByText("Minimum Size", { exact: true })).toBeVisible();
    await expect(page.getByText("Maximum Size", { exact: true })).toBeVisible();
    // The offer card shows a "Fee" field label when the maker is running.
    await expect(page.getByText("Fee", { exact: true }).first()).toBeVisible();

    // The "Loading offer..." text should NOT be visible.
    await expect(page.getByText("Loading offer...")).not.toBeVisible();

    // Take a screenshot to verify the offer card.
    await page.screenshot({
      path: "test-results/maker-offer-displayed.png",
      fullPage: true,
    });

    // Stop the maker.
    await page.getByRole("button", { name: "Stop" }).click();

    // Wait for the maker to stop - the form should reappear.
    await expect(
      page.getByRole("button", { name: "Start", exact: true }),
    ).toBeVisible({ timeout: 30_000 });
  });

  test("offer_list is populated in session API after maker starts", async ({
    fundedWallet,
    walletApi,
  }) => {
    const { token } = fundedWallet;

    // Stop any maker that may still be running from a previous test.
    try {
      await walletApi.stopMaker(token);
      await walletApi.waitForSession(token, (s) => !s.maker_running, 30_000);
    } catch {
      // No maker running — that's fine.
    }

    // Start the maker via API.
    await walletApi.startMaker(token, {
      ordertype: "sw0absoffer",
      cjfee_a: 250,
      minsize: 100_000,
    });

    // Wait for the session to show maker_running with offers.
    const session = await walletApi.waitForSession(
      token,
      (s) =>
        s.maker_running === true &&
        Array.isArray(s.offer_list) &&
        s.offer_list.length > 0,
      90_000,
    );

    // Validate the offer list.
    expect(session.offer_list).not.toBeNull();
    expect(session.offer_list!.length).toBeGreaterThan(0);

    const offer = session.offer_list![0];
    expect(offer.ordertype).toBe("sw0absoffer");
    expect(offer.cjfee).toBeTruthy();
    // size_factor randomizes only maxsize; the configured minimum stays exact.
    expect(Number(offer.minsize)).toBe(100_000);

    // Stop the maker.
    await walletApi.stopMaker(token);

    // Verify maker stops.
    const stopped = await walletApi.waitForSession(
      token,
      (s) => s.maker_running === false,
      30_000,
    );
    expect(stopped.maker_running).toBe(false);
  });
});
