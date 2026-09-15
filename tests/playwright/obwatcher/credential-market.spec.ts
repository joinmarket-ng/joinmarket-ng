import { expect, Page, test } from "@playwright/test";
import { AddressInfo } from "net";
import * as fs from "fs";
import * as http from "http";
import * as path from "path";

const STATIC_DIR = path.resolve(
  __dirname,
  "../../../orderbook_watcher/src/orderbook_watcher/static",
);
const SCREENSHOT_DIR = path.resolve(__dirname, "../../../tmp");
const DESKTOP_SCREENSHOT = path.join(SCREENSHOT_DIR, "credential-market-desktop.png");
const MOBILE_SCREENSHOT = path.join(SCREENSHOT_DIR, "credential-market-mobile.png");

const CONTENT_TYPES: Record<string, string> = {
  ".css": "text/css",
  ".html": "text/html",
  ".ico": "image/x-icon",
  ".js": "text/javascript",
};

const FUTURE_EXPIRY = Math.floor(Date.now() / 1000) + 86_400;

function coinjoinOffer(counterparty = "coinjoin-maker") {
  return {
    counterparty,
    oid: 0,
    ordertype: "sw0reloffer",
    cjfee: "0.0001",
    minsize: 100_000,
    maxsize: 1_000_000,
    directory_nodes: [],
    features: {},
  };
}

function observedOffer(
  sellerNick: string,
  priceSats: number,
  expiresAt: number,
  directories: string[],
  products: string[],
  period = 2016,
) {
  return {
    seller_nick: sellerNick,
    directory_nodes: directories,
    listing: {
      body: {
        kind: "listing",
        version: 1,
        network: "regtest",
        period,
        seller_pubkey: "02" + "ab".repeat(32),
        encryption_pubkey: "cd".repeat(32),
        products,
        price_sats: priceSats,
        expires_at: expiresAt,
      },
      signature: "ef".repeat(64),
    },
  };
}

function payload(credentialMarket?: Record<string, unknown>) {
  const body: Record<string, unknown> = {
    timestamp: new Date().toISOString(),
    offers: [coinjoinOffer(), coinjoinOffer("second-coinjoin-maker")],
    fidelitybonds: [],
    directory_nodes: [],
    directory_stats: {},
    feature_stats: {},
    feature_stats_denominator: 0,
    fee_quantization: null,
  };
  if (credentialMarket !== undefined) body.credential_market = credentialMarket;
  return body;
}

function startServer(body: unknown): Promise<http.Server> {
  const server = http.createServer((req, res) => {
    const url = (req.url || "/").split("?")[0];
    if (url === "/orderbook.json") {
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify(body));
      return;
    }

    const file = url === "/"
      ? path.join(STATIC_DIR, "index.html")
      : url.startsWith("/static/")
        ? path.join(STATIC_DIR, url.slice("/static/".length))
        : null;
    if (!file || !file.startsWith(STATIC_DIR) || !fs.existsSync(file)) {
      res.writeHead(404);
      res.end();
      return;
    }
    res.writeHead(200, {
      "content-type": CONTENT_TYPES[path.extname(file)] || "application/octet-stream",
    });
    res.end(fs.readFileSync(file));
  });
  return new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve(server)));
}

async function openWatcher(page: Page, body: unknown): Promise<http.Server> {
  const server = await startServer(body);
  const { port } = server.address() as AddressInfo;
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(String(error)));
  await page.goto(`http://127.0.0.1:${port}/`);
  await expect(page.locator("#coinjoin-offer-count")).toHaveText("2");
  expect(errors, `page errors: ${errors.join("; ")}`).toEqual([]);
  return server;
}

test.describe("credential market offer tabs", () => {
  test("separates product rows and counts from CoinJoin offers", async ({ page }) => {
    const sharedOffer = observedOffer(
      "shared-seller",
      3_000,
      FUTURE_EXPIRY + 600,
      ["shared-directory.onion:5222"],
      ["podle", "bond"],
    );
    const server = await openWatcher(page, payload({
      podle_offers: [
        observedOffer("podle-cheap", 1_000, FUTURE_EXPIRY, ["podle-directory.onion:5222"], ["podle"]),
        sharedOffer,
      ],
      bond_offers: [
        observedOffer("bond-cheap", 2_000, FUTURE_EXPIRY + 300, ["bond-directory.onion:5222"], ["bond"]),
        sharedOffer,
      ],
    }));

    try {
      await expect(page.locator("#coinjoin-offers-panel")).toBeVisible();
      await expect(page.locator("#orderbook-tbody tr")).toHaveCount(2);
      await expect(page.locator("#total-offers")).toHaveText("2");
      await expect(page.locator("#podle-offer-count")).toHaveText("2");
      await expect(page.locator("#bond-offer-count")).toHaveText("2");

      await page.locator("#podle-offers-tab").click();
      await expect(page.locator("#podle-offers-panel")).toBeVisible();
      await expect(page.locator("#podle-offers-tbody tr")).toHaveCount(2);
      await expect(page.locator("#podle-offers-tbody")).toContainText("podle-cheap");
      await expect(page.locator("#podle-offers-tbody")).toContainText("shared-seller");
      await expect(page.locator("#podle-offers-tbody")).not.toContainText("bond-cheap");

      await page.locator("#bond-offers-tab").click();
      await expect(page.locator("#bond-offers-tbody tr")).toHaveCount(2);
      await expect(page.locator("#bond-offers-tbody")).toContainText("bond-cheap");
      await expect(page.locator("#bond-offers-tbody")).toContainText("shared-seller");
      await expect(page.locator("#bond-offers-tbody")).not.toContainText("podle-cheap");
    } finally {
      server.close();
    }
  });

  test("keeps product search and sorting independent and restores the active tab", async ({ page }) => {
    const server = await openWatcher(page, payload({
      podle_offers: [
        observedOffer("podle-low", 1_000, FUTURE_EXPIRY + 300, [], ["podle"]),
        observedOffer("podle-shared", 4_000, FUTURE_EXPIRY, [], ["podle"]),
      ],
      bond_offers: [
        observedOffer("bond-late", 9_000, FUTURE_EXPIRY + 900, [], ["bond"]),
        observedOffer("bond-early", 2_000, FUTURE_EXPIRY + 100, [], ["bond"]),
      ],
    }));

    try {
      await page.locator("#podle-offers-tab").click();
      await page.locator("#podle-search").fill("shared");
      await expect(page.locator("#podle-offers-tbody tr")).toHaveCount(1);
      await expect(page.locator("#podle-offers-tbody")).toContainText("podle-shared");

      await page.locator("#bond-offers-tab").click();
      await expect(page.locator("#bond-offers-tbody tr")).toHaveCount(2);
      await page.locator("#bond-sort").selectOption("expires_at");
      await page.locator("#bond-sort-direction").click();
      await expect(page.locator("#bond-offers-tbody tr").first()).toContainText("bond-late");

      await page.locator("#podle-offers-tab").click();
      await expect(page.locator("#podle-offers-tbody tr")).toHaveCount(1);
      await page.locator("#bond-offers-tab").click();
      await page.reload();
      await expect(page.locator("#bond-offers-tab")).toHaveAttribute("aria-selected", "true");
      await expect(page.locator("#bond-offers-panel")).toBeVisible();
    } finally {
      server.close();
    }
  });

  test("supports keyboard tab navigation", async ({ page }) => {
    const server = await openWatcher(page, payload({ podle_offers: [], bond_offers: [] }));

    try {
      await expect(page.locator(".offer-tabs")).toHaveAttribute("role", "tablist");
      await expect(page.locator("#podle-offers-tab")).toHaveAttribute(
        "aria-controls",
        "podle-offers-panel",
      );
      await expect(page.locator("#podle-offers-panel")).toHaveAttribute("role", "tabpanel");
      await page.locator("#coinjoin-offers-tab").focus();
      await page.keyboard.press("ArrowRight");
      await expect(page.locator("#podle-offers-tab")).toBeFocused();
      await expect(page.locator("#podle-offers-tab")).toHaveAttribute("aria-selected", "true");
      await expect(page.locator("#podle-offers-panel")).toBeVisible();

      await page.keyboard.press("End");
      await expect(page.locator("#bond-offers-tab")).toBeFocused();
      await expect(page.locator("#bond-offers-panel")).toBeVisible();

      await page.keyboard.press("Home");
      await expect(page.locator("#coinjoin-offers-tab")).toBeFocused();
      await expect(page.locator("#coinjoin-offers-panel")).toBeVisible();
    } finally {
      server.close();
    }
  });

  test("shows unavailable data separately from empty product arrays", async ({ page }) => {
    const unavailableServer = await openWatcher(page, payload());

    try {
      await page.locator("#podle-offers-tab").click();
      await expect(page.locator("#podle-market-status")).toHaveText(
        "PoDLE offer data is unavailable from this orderbook.",
      );
      await page.locator("#bond-offers-tab").click();
      await expect(page.locator("#bond-market-status")).toHaveText(
        "Fidelity Bond offer data is unavailable from this orderbook.",
      );
    } finally {
      unavailableServer.close();
    }

    const emptyServer = await openWatcher(page, payload({ podle_offers: [], bond_offers: [] }));
    try {
      await page.locator("#podle-offers-tab").click();
      await expect(page.locator("#podle-market-status")).toHaveText("No active PoDLE offers.");
      await page.locator("#bond-offers-tab").click();
      await expect(page.locator("#bond-market-status")).toHaveText(
        "No active Fidelity Bond offers.",
      );
    } finally {
      emptyServer.close();
    }
  });

  test("mobile tab dimensions stay stable when changing offer kind", async ({ page }) => {
    const server = await openWatcher(page, payload({ podle_offers: [], bond_offers: [] }));
    try {
      for (const width of [320, 390]) {
        await page.setViewportSize({ width, height: 844 });
        const initial = await page.locator(".offer-tabs").boundingBox();
        for (const id of ["podle-offers-tab", "bond-offers-tab", "coinjoin-offers-tab"]) {
          await page.locator(`#${id}`).click();
          expect(await page.locator(".offer-tabs").boundingBox()).toEqual(initial);
          const fits = await page.evaluate(() =>
            document.documentElement.scrollWidth <= document.documentElement.clientWidth,
          );
          expect(fits).toBe(true);
        }
      }
    } finally {
      server.close();
    }
  });

  test("expires listings locally, renders hostile values as text, and stays within mobile width", async ({
    page,
  }) => {
    const hostileSeller = '<img src=x onerror="window.__credentialXssExecuted=true">';
    const longDirectory = `${"directory".padEnd(56, "x")}.onion:5222`;
    const server = await openWatcher(page, payload({
      podle_offers: [
        observedOffer(hostileSeller, 1_234, FUTURE_EXPIRY, [longDirectory], ["podle"]),
        observedOffer("expired-seller", 999, FUTURE_EXPIRY - 172_800, [], ["podle"]),
      ],
      bond_offers: [],
    }));

    try {
      await page.locator("#podle-offers-tab").click();
      const rows = page.locator("#podle-offers-tbody tr");
      await expect(rows).toHaveCount(1);
      await expect(rows.first()).toBeVisible();
      const seller = page.locator("#podle-offers-tbody .credential-seller .cell-value");
      await expect(seller).toHaveText(hostileSeller);
      await expect(seller.locator("img")).toHaveCount(0);
      expect(await page.evaluate(() => Reflect.get(window, "__credentialXssExecuted"))).toBeUndefined();
      await page.setViewportSize({ width: 1440, height: 900 });
      await expect(rows.first()).toBeVisible();
      const desktopDimensions = await page.evaluate(() => ({
        clientWidth: document.documentElement.clientWidth,
        scrollWidth: document.documentElement.scrollWidth,
      }));
      expect(desktopDimensions.scrollWidth).toBeLessThanOrEqual(desktopDimensions.clientWidth);
      await page.screenshot({ path: DESKTOP_SCREENSHOT, fullPage: true });

      await page.setViewportSize({ width: 390, height: 844 });
      await expect(rows.first()).toBeVisible();
      const dimensions = await page.evaluate(() => ({
        clientWidth: document.documentElement.clientWidth,
        scrollWidth: document.documentElement.scrollWidth,
      }));
      expect(dimensions.scrollWidth).toBeLessThanOrEqual(dimensions.clientWidth);
      await page.screenshot({ path: MOBILE_SCREENSHOT, fullPage: true });
    } finally {
      server.close();
    }
  });
});
