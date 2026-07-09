import { test, expect, Page } from "@playwright/test";
import * as http from "http";
import * as fs from "fs";
import * as path from "path";
import { AddressInfo } from "net";

/**
 * Frontend tests for the fee quantization bands chart (issue #508).
 *
 * Serves the real static frontend (orderbook_watcher/static) plus a
 * deterministic orderbook.json fixture from an in-process HTTP server, so the
 * suite needs no Docker stack and exercises exactly the files shipped in the
 * orderbook watcher image.
 */

const STATIC_DIR = path.resolve(__dirname, "../../../orderbook_watcher/static");

const FEE_QUANTIZATION = {
  rel_grid: [
    "0.00002",
    "0.00005",
    "0.0001",
    "0.0002",
    "0.0005",
    "0.001",
    "0.002",
    "0.005",
    "0.01",
    "0.02",
    "0.05",
    "0.1",
  ],
  abs_grid: [0, 100, 200, 500, 1000, 2000, 5000, 10000],
};

interface FixtureOffer {
  counterparty: string;
  oid?: number;
  ordertype: string;
  cjfee: string | number;
  minsize?: number;
  maxsize?: number;
  fidelity_bond_value?: number;
  fidelity_bond_data?: Record<string, unknown>;
  directory_nodes?: string[];
  features?: Record<string, boolean>;
}

function payload(offers: FixtureOffer[], extra: Record<string, unknown> = {}) {
  return {
    timestamp: new Date().toISOString(),
    offers,
    fidelitybonds: [],
    directory_nodes: [],
    directory_stats: {},
    feature_stats: {},
    feature_stats_denominator: 0,
    fee_quantization: FEE_QUANTIZATION,
    mempool_url: null,
    ...extra,
  };
}

// Bonded rel makers: 4 exactly on the grid (m1, m2 at 0.02%; m4 at 0.002%;
// m10 at 0.01%), 2 off-grid below a quantum (m3, m5), 1 above the grid (m6).
// Bonded abs makers: m7 exactly at 100 sats, m8 off-grid at 97 sats, m9 free.
// "nobond" must be excluded everywhere (sybil-cheap).
const DEFAULT_OFFERS: FixtureOffer[] = [
  { counterparty: "m1", ordertype: "sw0reloffer", cjfee: "0.0002", maxsize: 100_000_000, fidelity_bond_value: 5e7 },
  { counterparty: "m2", ordertype: "sw0reloffer", cjfee: "0.0002", maxsize: 50_000_000, fidelity_bond_value: 2e7 },
  { counterparty: "m3", ordertype: "sw0reloffer", cjfee: "0.00015", maxsize: 20_000_000, fidelity_bond_value: 1e7 },
  { counterparty: "m4", ordertype: "sw0reloffer", cjfee: "0.00002", maxsize: 300_000_000, fidelity_bond_value: 9e7 },
  { counterparty: "m5", ordertype: "sw0reloffer", cjfee: "0.00123", maxsize: 10_000_000, fidelity_bond_value: 3e6 },
  { counterparty: "m6", ordertype: "sw0reloffer", cjfee: "0.2", maxsize: 10_000_000, fidelity_bond_value: 1e6 },
  { counterparty: "m7", ordertype: "sw0absoffer", cjfee: 100, maxsize: 40_000_000, fidelity_bond_value: 4e7 },
  { counterparty: "m8", ordertype: "sw0absoffer", cjfee: "97", maxsize: 60_000_000, fidelity_bond_value: 2e7 },
  { counterparty: "m9", ordertype: "sw0absoffer", cjfee: 0, maxsize: 15_000_000, fidelity_bond_value: 1e7 },
  {
    counterparty: "m10",
    ordertype: "sw0reloffer",
    cjfee: "0.0001",
    maxsize: 1_000_000,
    fidelity_bond_value: 0,
    fidelity_bond_data: { utxo_txid: "aa", utxo_vout: 0 },
  },
  { counterparty: "nobond", ordertype: "sw0reloffer", cjfee: "0.0002", maxsize: 1_000_000 },
];

const CONTENT_TYPES: Record<string, string> = {
  ".html": "text/html",
  ".js": "text/javascript",
  ".css": "text/css",
  ".ico": "image/x-icon",
};

function startServer(body: unknown): Promise<http.Server> {
  const server = http.createServer((req, res) => {
    const url = (req.url || "/").split("?")[0];
    if (url === "/orderbook.json") {
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify(body));
      return;
    }
    const file =
      url === "/"
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

async function openChart(page: Page, body: unknown): Promise<http.Server> {
  const server = await startServer(body);
  const { port } = server.address() as AddressInfo;
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  await page.goto(`http://127.0.0.1:${port}/`);
  await expect(page.locator("#fee-quant-chart")).not.toBeEmpty();
  expect(errors, `page errors: ${errors.join("; ")}`).toEqual([]);
  return server;
}

test.describe("fee quantization chart", () => {
  test("relative mode: bands, exact/near split, tooltip, legend", async ({ page }) => {
    const server = await openChart(page, payload(DEFAULT_OFFERS));

    await expect(page.locator(".fq-summary")).toHaveText(
      "4 of 7 bonded makers advertise an exact grid fee.",
    );

    // 12 grid bands + the above-grid overflow column (m6 at 20%).
    const counts = page.locator(".fq-count");
    await expect(counts).toHaveCount(13);

    // 0.02% band: m1 + m2 exact, m3 (0.015%) rounds up into it.
    const band = page.locator(".fq-col").nth(3);
    await expect(band.locator(".fq-count")).toHaveText("3");
    await expect(band.locator(".fq-seg-exact")).toHaveCSS("height", /.+/);
    const tooltip = await band.locator(".fq-bar").getAttribute("title");
    expect(tooltip).toContain("2 maker(s) exactly at 0.02% (shared anonymity set).");
    expect(tooltip).toContain("1 maker(s) below it with a unique fee.");
    expect(tooltip).toContain("0.8000 BTC bonded in this band.");
    // Median max size across m1 (1.0), m2 (0.5), m3 (0.2) BTC.
    expect(tooltip).toContain("Median max size: 0.5000 BTC.");
    // Cumulative reach: (9 + 8) / 21.1 of total bonded value.
    expect(tooltip).toMatch(/A taker capped at 0\.02% reaches \d+% of bonded value\./);

    // Exact and near segments split the bar by maker count (2/3 vs 1/3).
    const exactHeight = await band
      .locator(".fq-seg-exact")
      .evaluate((el) => (el as HTMLElement).style.height);
    expect(exactHeight).toMatch(/^66\.66/);

    // Axis ticks: regression for the 10% label (0.1 must not render as 1%),
    // plus the overflow column label.
    const ticks = page.locator(".fq-tick");
    await expect(ticks.first()).toHaveText("0.002%");
    await expect(ticks.nth(11)).toHaveText("10%");
    await expect(ticks.last()).toHaveText("> max");
    const overflowTitle = await page.locator(".fq-bar").last().getAttribute("title");
    expect(overflowTitle).toContain("above the largest quantum");

    await expect(page.locator(".fq-axis-caption")).toHaveText(
      "Advertised relative fee (% of coinjoin amount)",
    );
    await expect(page.locator(".fq-legend-item")).toHaveCount(2);

    server.close();
  });

  test("absolute mode: toggle, free band, unit caption", async ({ page }) => {
    const server = await openChart(page, payload(DEFAULT_OFFERS));

    await page.click("#fee-quant-abs-btn");

    await expect(page.locator(".fq-summary")).toHaveText(
      "2 of 3 bonded makers advertise an exact grid fee.",
    );
    // Zero-fee band is labeled "free"; m9 sits there exactly.
    await expect(page.locator(".fq-tick").first()).toHaveText("free");
    // 100-sat band: m7 exact + m8 (97 sats) near.
    await expect(page.locator(".fq-col").nth(1).locator(".fq-count")).toHaveText("2");
    await expect(page.locator(".fq-axis-caption")).toHaveText(
      "Advertised absolute fee (satoshis per coinjoin)",
    );

    server.close();
  });

  test("deduplicates multiple offers per maker to the cheapest", async ({ page }) => {
    const offers: FixtureOffer[] = [
      { counterparty: "m1", oid: 0, ordertype: "sw0reloffer", cjfee: "0.0002", maxsize: 1_000_000, fidelity_bond_value: 1e7 },
      { counterparty: "m1", oid: 1, ordertype: "sw0reloffer", cjfee: "0.001", maxsize: 2_000_000, fidelity_bond_value: 1e7 },
    ];
    const server = await openChart(page, payload(offers));

    await expect(page.locator(".fq-summary")).toHaveText(
      "1 of 1 bonded makers advertise an exact grid fee.",
    );
    // Only the cheapest offer counts: the 0.02% band has it, 0.1% has none.
    await expect(page.locator(".fq-col").nth(3).locator(".fq-count")).toHaveText("1");
    await expect(page.locator(".fq-col").nth(5).locator(".fq-count")).toHaveText("0");

    server.close();
  });

  test("empty orderbook still renders the grid with a notice", async ({ page }) => {
    const server = await openChart(page, payload([]));

    await expect(page.locator(".fq-summary")).toHaveText(
      "No bonded makers in the orderbook yet.",
    );
    // The grid renders with all-zero bars so the section never looks broken.
    await expect(page.locator(".fq-count")).toHaveCount(12);
    await expect(page.locator(".fq-tick").nth(11)).toHaveText("10%");

    server.close();
  });

  test("missing fee grid shows the unavailable notice", async ({ page }) => {
    const server = await openChart(page, payload([], { fee_quantization: null }));

    await expect(page.locator("#fee-quant-chart")).toHaveText("Fee grid unavailable.");

    server.close();
  });
});
