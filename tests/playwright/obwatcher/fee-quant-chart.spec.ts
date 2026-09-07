import { test, expect, Page } from "@playwright/test";
import * as http from "http";
import * as fs from "fs";
import * as path from "path";
import { AddressInfo } from "net";

/**
 * Frontend tests for the fee quantization bands chart (issue #508).
 *
 * Serves the packaged frontend from orderbook_watcher/src/orderbook_watcher/static
 * with a deterministic orderbook.json fixture from an in-process HTTP server.
 * The suite needs no Docker stack and exercises the files shipped in the image.
 */

const STATIC_DIR = path.resolve(
  __dirname,
  "../../../orderbook_watcher/src/orderbook_watcher/static",
);

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
  txfee?: number;
  fidelity_bond_value?: number;
  fidelity_bond_data?: Record<string, unknown>;
  fidelity_bond_verified?: boolean | null;
  fidelity_bond_verification_stale?: boolean;
  directory_nodes?: string[];
  features?: Record<string, boolean>;
}

const BOND_EXPIRY = 901_152;

function bondPayload(currentBlockHeight: number | null, bondValue = 10_000) {
  const bondData = {
    maker_nick: "bondmaker",
    utxo_txid: "a".repeat(64),
    utxo_vout: 1,
    locktime: 2_000_000_000,
    utxo_pub: "02" + "11".repeat(32),
    cert_pub: "03" + "22".repeat(32),
    cert_expiry: BOND_EXPIRY,
    redeem_script: "00",
    p2wsh_script: "00",
  };
  const offers: FixtureOffer[] = [{
    counterparty: "bondmaker",
    oid: 0,
    ordertype: "sw0reloffer",
    cjfee: "0.001",
    minsize: 10_000,
    maxsize: 1_000_000,
    txfee: 1000,
    fidelity_bond_value: bondValue,
    fidelity_bond_data: bondData,
    directory_nodes: [],
    features: {},
  }];
  return payload(offers, {
    current_block_height: currentBlockHeight,
    fidelitybonds: [{
      counterparty: "bondmaker",
      utxo: { txid: bondData.utxo_txid, vout: bondData.utxo_vout },
      bond_value: bondValue,
      locktime: bondData.locktime,
      amount: 100_000_000,
      cert_expiry: BOND_EXPIRY,
      utxo_pub: bondData.utxo_pub,
    }],
  });
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
    current_block_height: 900_000,
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
    fidelity_bond_data: { utxo_txid: "aa", utxo_vout: 0, cert_expiry: BOND_EXPIRY },
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
    // Cumulative bond share reachable at or under 0.02%: (m4 + m10 + m1 + m2 + m3)
    // = 170,000,000 / 174,000,000 total bonded across all rel makers.
    expect(tooltip).toContain("98% of total bonded value is reachable at or under this fee.");
    // Max coinjoin size with 10 makers: only 5 makers are at or under 0.02%
    // (m4, m10, m1, m2, m3), so the bound is the smallest of those five
    // (m10's 1,000,000 sats) and the tooltip notes the shortfall.
    expect(tooltip).toContain(
      "Max coinjoin size with 10 makers at or under this fee: 1,000,000 sats (0.0100 BTC) " +
        "(only 5 maker(s) at or under this fee).",
    );

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

  test("max coinjoin size stat omits the shortfall note with >=10 makers", async ({ page }) => {
    const offers: FixtureOffer[] = Array.from({ length: 10 }, (_, i) => ({
      counterparty: `bulk${i}`,
      ordertype: "sw0reloffer",
      cjfee: "0.0001",
      maxsize: (i + 1) * 1_000_000,
      fidelity_bond_value: 1e7,
    }));
    const server = await openChart(page, payload(offers));

    // 10 makers exactly at 0.01%: the bound is the smallest maxsize among
    // them (1,000,000 sats), and with exactly 10 available there is no
    // shortfall note.
    const band = page.locator(".fq-col").nth(2);
    const tooltip = await band.locator(".fq-bar").getAttribute("title");
    expect(tooltip).toContain(
      "Max coinjoin size with 10 makers at or under this fee: 1,000,000 sats (0.0100 BTC).",
    );
    expect(tooltip).not.toContain("only");

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

test.describe("feature display names", () => {
  test("uses stable shortnames for maker and directory features", async ({ page }) => {
    const directory = "directory.example:5222";
    const offers: FixtureOffer[] = [{
      counterparty: "featuremaker",
      oid: 0,
      ordertype: "sw0reloffer",
      cjfee: "0.0001",
      minsize: 100_000,
      maxsize: 1_000_000,
      txfee: 0,
      fidelity_bond_value: 10_000,
      directory_nodes: [directory],
      features: { nick_auth: true, ping: true },
    }];
    const body = payload(offers, {
      directory_nodes: [directory],
      directory_stats: {
        [directory]: {
          offer_count: 1,
          bond_offer_count: 1,
          connected: true,
          connection_attempts: 1,
          successful_connections: 1,
          uptime_percentage: 100,
          proto_ver_min: 5,
          proto_ver_max: 5,
          features: { peerlist_features: true, ping: true, nick_auth: true },
        },
      },
      feature_stats: { nick_auth: 1, ping: 1 },
      feature_stats_denominator: 1,
    });
    const server = await openChart(page, body);

    try {
      const featureStats = page.locator("#feature-breakdown .feature-badge");
      await expect(featureStats).toHaveText(["NAU", "PNG"]);
      await expect(featureStats.nth(0)).toHaveAttribute("title", "nick_auth");
      await expect(featureStats.nth(1)).toHaveAttribute("title", "ping");

      const offerBadges = page.locator("#orderbook-tbody .feature-badge");
      await expect(offerBadges).toHaveText(["NAU", "PNG"]);
      await expect(offerBadges.nth(0)).toHaveAttribute("title", "nick_auth");
      await expect(offerBadges.nth(1)).toHaveAttribute("title", "ping");

      const directoryFeatures = page.locator(".directory-features");
      await expect(directoryFeatures).toHaveText("PLF, PNG, NAU");
      await expect(directoryFeatures).toHaveAttribute(
        "title",
        "Directory features: peerlist_features, ping, nick_auth",
      );
    } finally {
      server.close();
    }
  });
});

test.describe("orderbook rendering safety", () => {
  test("renders hostile feature names without creating markup", async ({ page }) => {
    const feature = '<img src=x onerror="window.__featureXssExecuted=true">';
    const directory = "directory.example:5222";
    const offers: FixtureOffer[] = [{
      counterparty: "featuremaker",
      oid: 0,
      ordertype: "sw0reloffer",
      cjfee: "0.0001",
      minsize: 100_000,
      maxsize: 1_000_000,
      txfee: 0,
      fidelity_bond_value: 10_000,
      directory_nodes: [directory],
      features: { [feature]: true },
    }];
    const server = await openChart(page, payload(offers, {
      directory_nodes: [directory],
      directory_stats: {
        [directory]: {
          offer_count: 1,
          connected: true,
          features: { [feature]: true },
        },
      },
      feature_stats: { [feature]: 1 },
      feature_stats_denominator: 1,
    }));

    try {
      await expect(page.locator("#orderbook-tbody .feature-badge")).toHaveAttribute("title", feature);
      await expect(page.locator("#feature-breakdown .feature-badge")).toHaveAttribute("title", feature);
      await expect(page.locator(".directory-features")).toHaveAttribute(
        "title", `Directory features: ${feature}`,
      );
      await expect(page.locator("#orderbook-tbody img, #feature-breakdown img, .directory-features img"))
        .toHaveCount(0);
      expect(await page.evaluate(() => Reflect.get(window, "__featureXssExecuted"))).toBeUndefined();
    } finally {
      server.close();
    }
  });

  test("renders a hostile counterparty as text without executing markup", async ({ page }) => {
    const maliciousNick = '<img src=x onerror="window.__nickXssExecuted=true">';
    const offers: FixtureOffer[] = [{
      counterparty: maliciousNick,
      oid: 0,
      ordertype: "sw0reloffer",
      cjfee: "0.0001",
      minsize: 100_000,
      maxsize: 1_000_000,
      txfee: 0,
      directory_nodes: [],
      features: {},
    }];
    const server = await openChart(page, payload(offers));

    try {
      const counterparty = page.locator("#orderbook-tbody .counterparty .cell-value");
      await expect(counterparty).toHaveText(maliciousNick);
      await expect(counterparty.locator("img")).toHaveCount(0);
      expect(await page.evaluate(() => Reflect.get(window, "__nickXssExecuted"))).toBeUndefined();
    } finally {
      server.close();
    }
  });
});

test.describe("offer selection probability", () => {
  test("counts offers sharing a fidelity bond as one candidate", async ({ page }) => {
    const sharedTxid = "f".repeat(64);
    const offers: FixtureOffer[] = Array.from({ length: 10 }, (_, i) => ({
      counterparty: `shared-bond-maker-${i}`,
      oid: 0,
      ordertype: "sw0absoffer",
      cjfee: 100,
      minsize: 100_000,
      maxsize: 10_000_000,
      fidelity_bond_value: 10_000,
      fidelity_bond_data: {
        utxo_txid: i < 2 ? sharedTxid : i.toString(16).padStart(64, "0"),
        utxo_vout: 0,
        cert_expiry: BOND_EXPIRY,
      },
      directory_nodes: [],
      features: {},
    }));
    const server = await openChart(page, payload(offers));

    try {
      const firstSharedChance = page.locator("#orderbook-tbody tr", {
        hasText: "shared-bond-maker-0",
      }).locator(".selection-probability");
      const secondSharedChance = page.locator("#orderbook-tbody tr", {
        hasText: "shared-bond-maker-1",
      }).locator(".selection-probability");
      const uniqueChance = page.locator("#orderbook-tbody tr", {
        hasText: "shared-bond-maker-2",
      }).locator(".selection-probability");

      await expect(firstSharedChance).toHaveText("1/1*");
      await expect(secondSharedChance).toHaveText("1/1*");
      await expect(firstSharedChance).toHaveAttribute("title", /bond-level chance/);
      await expect(firstSharedChance).toHaveAttribute("title", /keep at most one offer/);
      await expect(uniqueChance).toHaveText("1/1");
    } finally {
      server.close();
    }
  });

  test("shows reciprocal round-level chances for bonded and zero-fee bondless offers", async ({ page }) => {
    const bondedOffers: FixtureOffer[] = Array.from({ length: 20 }, (_, i) => ({
      counterparty: `bonded${i}`,
      oid: 0,
      ordertype: "sw0absoffer",
      cjfee: 100,
      minsize: 100_000,
      maxsize: 10_000_000,
      fidelity_bond_value: i === 0 ? 1_000_000 : 10_000,
      directory_nodes: [],
      features: {},
    }));
    const bondlessOffers: FixtureOffer[] = Array.from({ length: 20 }, (_, i) => ({
      counterparty: `bondless${i}`,
      oid: 0,
      ordertype: "sw0absoffer",
      cjfee: 0,
      minsize: 100_000,
      maxsize: 10_000_000,
      fidelity_bond_value: 0,
      directory_nodes: [],
      features: {},
    }));
    const excludedOffers: FixtureOffer[] = [
      {
        counterparty: "fee-charging-bondless",
        oid: 0,
        ordertype: "sw0absoffer",
        cjfee: 100,
        minsize: 100_000,
        maxsize: 10_000_000,
        fidelity_bond_value: 0,
        directory_nodes: [],
        features: {},
      },
      {
        counterparty: "legacy-bonded",
        oid: 0,
        ordertype: "swabsoffer",
        cjfee: 0,
        minsize: 100_000,
        maxsize: 10_000_000,
        fidelity_bond_value: 100_000,
        directory_nodes: [],
        features: {},
      },
      {
        counterparty: "expired-certificate",
        oid: 0,
        ordertype: "sw0absoffer",
        cjfee: 100,
        minsize: 100_000,
        maxsize: 10_000_000,
        fidelity_bond_value: 100_000,
        fidelity_bond_data: {
          utxo_txid: "e".repeat(64),
          utxo_vout: 0,
          cert_expiry: 899_999,
        },
        fidelity_bond_verified: true,
        fidelity_bond_verification_stale: false,
        directory_nodes: [],
        features: {},
      },
      {
        counterparty: "bond-value-pending",
        oid: 0,
        ordertype: "sw0absoffer",
        cjfee: 100,
        minsize: 100_000,
        maxsize: 10_000_000,
        fidelity_bond_value: 0,
        fidelity_bond_data: {
          utxo_txid: "d".repeat(64),
          utxo_vout: 0,
          cert_expiry: BOND_EXPIRY,
        },
        fidelity_bond_verified: true,
        fidelity_bond_verification_stale: false,
        directory_nodes: [],
        features: {},
      },
    ];
    const server = await openChart(
      page,
      payload([...bondedOffers, ...bondlessOffers, ...excludedOffers]),
    );

    try {
      const bondedChance = page.locator("#orderbook-tbody tr")
        .filter({ has: page.locator('.counterparty .cell-value:text-is("bonded0")') })
        .locator(".selection-probability");
      const lowBondChance = page.locator("#orderbook-tbody tr")
        .filter({ has: page.locator('.counterparty .cell-value:text-is("bonded1")') })
        .locator(".selection-probability");
      const bondlessChance = page.locator("#orderbook-tbody tr")
        .filter({ has: page.locator('.counterparty .cell-value:text-is("bondless0")') })
        .locator(".selection-probability");
      const feeChargingChance = page.locator(
        "#orderbook-tbody tr",
        { hasText: "fee-charging-bondless" },
      ).locator(".selection-probability");
      const legacyChance = page.locator("#orderbook-tbody tr", { hasText: "legacy-bonded" })
        .locator(".selection-probability");
      const expiredChance = page.locator("#orderbook-tbody tr", {
        hasText: "expired-certificate",
      }).locator(".selection-probability");
      const pendingValueChance = page.locator("#orderbook-tbody tr", {
        hasText: "bond-value-pending",
      }).locator(".selection-probability");

      await expect(bondedChance).toHaveText(/^1\/1(?:\.\d)?$/);
      expect(await lowBondChance.textContent()).not.toBe(await bondedChance.textContent());
      await expect(bondlessChance).toHaveText("1/44");
      await expect(bondedChance).toHaveAttribute("title", /5% zero-fee allowance/);
      await expect(feeChargingChance).toHaveText("0");
      await expect(feeChargingChance).toHaveAttribute("title", /nonzero-fee bondless offer/);
      await expect(legacyChance).toHaveText("0");
      await expect(legacyChance).toHaveAttribute("title", /SWA Absolute offers are excluded/);
      await expect(expiredChance).toHaveAttribute(
        "title",
        "The fidelity bond certificate expired at block 899999; the current height is 900000.",
      );
      await expect(pendingValueChance).toHaveAttribute(
        "title",
        /has not established a positive fidelity bond value yet/,
      );

      await page.locator('th[data-sort="selection_probability"]').click();
      await expect(page.locator("#orderbook-tbody tr").first()).toContainText("bonded");
    } finally {
      server.close();
    }
  });

  test("gives zero-fee bonded offers access to allowance slots", async ({ page }) => {
    const offers: FixtureOffer[] = [
      ...Array.from({ length: 10 }, (_, i) => ({
        counterparty: `bonded-fee-${i}`,
        oid: 0,
        ordertype: "sw0absoffer",
        cjfee: 100,
        minsize: 100_000,
        maxsize: 10_000_000,
        fidelity_bond_value: 10_000,
        directory_nodes: [],
        features: {},
      })),
      ...Array.from({ length: 10 }, (_, i) => ({
        counterparty: `bonded-zero-${i}`,
        oid: 0,
        ordertype: "sw0absoffer",
        cjfee: 0,
        minsize: 100_000,
        maxsize: 10_000_000,
        fidelity_bond_value: 10_000,
        directory_nodes: [],
        features: {},
      })),
      ...Array.from({ length: 10 }, (_, i) => ({
        counterparty: `bondless-zero-${i}`,
        oid: 0,
        ordertype: "sw0absoffer",
        cjfee: 0,
        minsize: 100_000,
        maxsize: 10_000_000,
        fidelity_bond_value: 0,
        directory_nodes: [],
        features: {},
      })),
    ];
    const server = await openChart(page, payload(offers));

    try {
      const feeChanceText = await page.locator("#orderbook-tbody tr", {
        hasText: "bonded-fee-0",
      }).locator(".selection-probability").textContent();
      const zeroChanceText = await page.locator("#orderbook-tbody tr", {
        hasText: "bonded-zero-0",
      }).locator(".selection-probability").textContent();
      const bondlessChance = page.locator("#orderbook-tbody tr", {
        hasText: "bondless-zero-0",
      }).locator(".selection-probability");
      const denominator = (text: string | null) => Number(text?.replace("1/", ""));

      expect(denominator(zeroChanceText)).toBeLessThan(denominator(feeChanceText));
      await expect(bondlessChance).toHaveText(/^1\/\d+(?:\.\d)?$/);
      await expect(bondlessChance).toHaveAttribute("title", /5% zero-fee allowance/);
    } finally {
      server.close();
    }
  });

  test("skips non-quantized offers under current taker defaults", async ({ page }) => {
    const eligibleOffers: FixtureOffer[] = Array.from({ length: 9 }, (_, i) => ({
      counterparty: `quantized-${i}`,
      oid: 0,
      ordertype: "sw0absoffer",
      cjfee: 100,
      minsize: 100_000,
      maxsize: 10_000_000,
      fidelity_bond_value: 10_000,
      directory_nodes: [],
      features: {},
    }));
    const offGridOffers: FixtureOffer[] = [
      {
        counterparty: "off-grid-absolute",
        oid: 0,
        ordertype: "sw0absoffer",
        cjfee: 150,
        minsize: 100_000,
        maxsize: 10_000_000,
        fidelity_bond_value: 10_000_000,
        directory_nodes: [],
        features: {},
      },
      {
        counterparty: "off-grid-relative",
        oid: 0,
        ordertype: "sw0reloffer",
        cjfee: "0.00015",
        minsize: 100_000,
        maxsize: 10_000_000,
        fidelity_bond_value: 10_000_000,
        directory_nodes: [],
        features: {},
      },
    ];
    const server = await openChart(page, payload([...eligibleOffers, ...offGridOffers]));

    try {
      const eligibleChance = page.locator("#orderbook-tbody tr", {
        hasText: "quantized-0",
      }).locator(".selection-probability");
      const absoluteChance = page.locator("#orderbook-tbody tr", {
        hasText: "off-grid-absolute",
      }).locator(".selection-probability");
      const relativeChance = page.locator("#orderbook-tbody tr", {
        hasText: "off-grid-relative",
      }).locator(".selection-probability");

      await expect(eligibleChance).toHaveText("1/1");
      await expect(eligibleChance).toHaveAttribute("title", /Non-quantized fee offers are skipped/);
      await expect(absoluteChance).toHaveText("0");
      await expect(relativeChance).toHaveText("0");
      await expect(absoluteChance).toHaveAttribute("title", /not on the public fee grid/);
      await expect(relativeChance).toHaveAttribute("title", /require quantized fees/);
      await expect(page.locator('th[data-sort="selection_probability"]')).toHaveAttribute(
        "title",
        /quantized-only taker defaults/,
      );
    } finally {
      server.close();
    }
  });
});

test.describe("mobile layout", () => {
  test("contains long onion URLs and presents offers as labeled rows", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    const onion = `${"directory".padEnd(56, "x")}.onion:5222`;
    const offers: FixtureOffer[] = [
      {
        counterparty: "J5MobileLayoutMakerWithALongCounterpartyName",
        oid: 0,
        ordertype: "sw0reloffer",
        cjfee: "0.0001",
        minsize: 100_000,
        maxsize: 10_000_000,
        fidelity_bond_value: 10_000,
        directory_nodes: [onion],
        features: { nick_auth: true },
      },
      {
        counterparty: "fee-charging-bondless",
        oid: 0,
        ordertype: "sw0reloffer",
        cjfee: "0.001",
        minsize: 100_000,
        maxsize: 10_000_000,
        fidelity_bond_value: 0,
        directory_nodes: [onion],
        features: {},
      },
    ];
    const body = payload(offers, {
      directory_nodes: [onion],
      directory_stats: {
        [onion]: {
          offer_count: 1,
          bond_offer_count: 1,
          connected: true,
          connection_attempts: 1,
          successful_connections: 1,
          uptime_percentage: 100,
        },
      },
    });
    const server = await openChart(page, body);

    try {
      await expect(page.locator("#orderbook-table thead")).toBeHidden();
      const pickChance = page.locator(
        '#orderbook-tbody td[data-label="Pick Chance"]',
      ).first();
      await expect(pickChance).toHaveText("N/A");
      const pickChanceTrigger = pickChance.locator(".cell-value");
      await expect(pickChanceTrigger).toHaveAttribute("role", "button");
      await pickChance.click();
      await expect(pickChanceTrigger).toHaveAttribute("aria-expanded", "true");
      await expect(page.locator("#selection-probability-tooltip")).toBeVisible();
      await expect(page.locator("#selection-probability-tooltip")).toHaveText(
        /Fewer than 9 qualifying candidates/,
      );
      await page.keyboard.press("Escape");
      await expect(page.locator("#selection-probability-tooltip")).toBeHidden();
      await expect(pickChanceTrigger).toHaveAttribute("aria-expanded", "false");
      await expect(page.locator(".directory-name")).toHaveCSS("overflow-wrap", "anywhere");
      await page.locator("#mobile-sort-column").selectOption("selection_probability");
      await expect(page.locator("#orderbook-tbody tr").first())
        .toContainText("fee-charging-bondless");
      await page.locator("#mobile-sort-direction").click();
      await expect(page.locator("#orderbook-tbody tr").first())
        .toContainText("J5MobileLayoutMakerWithALongCounterpartyName");
      const dimensions = await page.evaluate(() => ({
        clientWidth: document.documentElement.clientWidth,
        scrollWidth: document.documentElement.scrollWidth,
      }));
      expect(dimensions.scrollWidth).toBeLessThanOrEqual(dimensions.clientWidth);
      const directoryNameBox = await page.locator(".directory-name-container").boundingBox();
      const directoryInfoBox = await page.locator(".directory-info").boundingBox();
      expect(directoryNameBox).not.toBeNull();
      expect(directoryInfoBox).not.toBeNull();
      expect(directoryNameBox!.y + directoryNameBox!.height).toBeLessThanOrEqual(
        directoryInfoBox!.y,
      );
    } finally {
      server.close();
    }
  });
});

test.describe("fidelity bond certificate expiry", () => {
  for (const testCase of [
    { name: "before boundary", height: BOND_EXPIRY - 1, expired: false },
    { name: "at boundary", height: BOND_EXPIRY, expired: false },
    { name: "after boundary", height: BOND_EXPIRY + 1, expired: true },
  ]) {
    test(testCase.name, async ({ page }) => {
      const server = await openChart(page, bondPayload(testCase.height));
      try {
        const bondCell = page.locator(".bond-value-clickable");
        if (testCase.expired) {
          await bondCell.focus();
          await page.keyboard.press("Enter");
        } else {
          await bondCell.click();
        }
        const summary = page.locator("#bond-verification-summary");
        if (testCase.expired) {
          await expect(summary).toHaveClass(/expired/);
          await expect(page.locator("#bond-cert-expiry")).toContainText("EXPIRED 1 blocks ago");
          await expect(page.locator("#bond-verification-text")).toContainText(
            "Underlying bond value: 10,000. Restart the maker to renew the certificate.",
          );
          await expect(page.locator(".bond-value-clickable")).toContainText("10,000");
          const warning = page.locator(".bond-expired-indicator");
          await expect(warning).toHaveText("!");
          await expect(warning).toHaveAttribute(
            "title",
            /takers treat this maker as unbonded.*Restart the maker/,
          );
          await expect(page.locator(".fq-summary")).toHaveText(
            "No bonded makers in the orderbook yet.",
          );
        } else {
          await expect(summary).toHaveClass(/valid/);
          await expect(page.locator("#bond-cert-expiry")).not.toContainText("EXPIRED");
        }
      } finally {
        server.close();
      }
    });
  }

  test("unavailable height is not shown as valid", async ({ page }) => {
    const body = bondPayload(null) as any;
    body.mempool_url = "http://127.0.0.1:1";
    let tipRequests = 0;
    page.on("request", request => {
      if (request.url().includes("/blocks/tip/height")) tipRequests += 1;
    });
    const server = await openChart(page, body);
    try {
      await page.locator(".bond-value-clickable").click();
      await expect(page.locator("#bond-verification-summary")).toHaveClass(/pending/);
      await expect(page.locator("#bond-verification-text")).toContainText(
        "Underlying bond value: 10,000.",
      );
      await expect(page.locator(".bond-value-clickable")).toContainText("10,000");
      await expect(page.locator(".bond-unverified-indicator")).toHaveAttribute(
        "title",
        /takers treat this maker as unbonded until block height is available/,
      );
      await expect(page.locator(".fq-summary")).toHaveText(
        "No bonded makers in the orderbook yet.",
      );
      expect(tipRequests).toBe(0);
    } finally {
      server.close();
    }
  });

  test("bond warning stays attached to its value on narrow screens", async ({ page }) => {
    await page.setViewportSize({ width: 320, height: 800 });
    const server = await openChart(page, bondPayload(BOND_EXPIRY + 1));
    try {
      const bondValue = page.locator(".bond-value-clickable > .cell-value");
      const warning = page.locator(".bond-expired-indicator");
      await expect(warning).toBeVisible();

      const layout = await bondValue.evaluate(element => {
        const textNode = element.firstChild;
        const indicator = element.querySelector(".bond-expired-indicator");
        if (!textNode || !indicator) throw new Error("Bond value layout is incomplete");
        const textRange = document.createRange();
        textRange.selectNodeContents(textNode);
        const textRect = textRange.getBoundingClientRect();
        const indicatorRect = indicator.getBoundingClientRect();
        return {
          textRight: textRect.right,
          textTop: textRect.top,
          indicatorLeft: indicatorRect.left,
          indicatorTop: indicatorRect.top,
          clientWidth: document.documentElement.clientWidth,
          scrollWidth: document.documentElement.scrollWidth,
        };
      });

      expect(layout.indicatorLeft).toBeGreaterThanOrEqual(layout.textRight);
      expect(Math.abs(layout.indicatorTop - layout.textTop)).toBeLessThanOrEqual(4);
      expect(layout.scrollWidth).toBeLessThanOrEqual(layout.clientWidth);
    } finally {
      server.close();
    }
  });

  test("modal amount matches the complete script claim", async ({ page }) => {
    const body = bondPayload(BOND_EXPIRY) as any;
    body.fidelitybonds.unshift({
      ...body.fidelitybonds[0],
      amount: 200_000_000,
      locktime: body.fidelitybonds[0].locktime + 1,
      utxo_pub: "03" + "33".repeat(32),
    });
    const server = await openChart(page, body);
    try {
      await page.locator(".bond-value-clickable").click();
      await expect(page.locator("#bond-amount")).toHaveText(
        "100,000,000 sats (1.00000000 BTC)",
      );
    } finally {
      server.close();
    }
  });
});
