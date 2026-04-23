/**
 * Playwright global setup: creates and funds the shared test wallet once.
 *
 * Funding strategy:
 *   - Unlock the wallet first so jmwalletd imports its descriptors into
 *     Bitcoin Core. Only then will sent UTXOs be tracked.
 *   - Wait for the Bitcoin Core descriptor wallet to finish its initial rescan
 *     (triggered on first import). This can take >2 minutes for ~5000 blocks.
 *   - Use `sendtoaddress` from the `fidelity_funder` Bitcoin Core wallet,
 *     which was pre-funded at chain start (50 BTC coinbase at low height).
 *     This works at any current block height, unlike `generatetoaddress`
 *     whose coinbase reward is dust at high block heights.
 *   - Mine 1 confirmation block (generatetoaddress to any address).
 *   - Call GET /utxos which triggers an automatic descriptor wallet refresh
 *     in jmwalletd, making the funded UTXO visible.
 *   - Poll /display until balance is sufficient.
 *
 * Writes credentials to tmp/test-wallet.json for use by all test fixtures.
 * If the wallet already exists and has sufficient funds, skips all of this.
 */

import * as api from "./fixtures/jmwalletd-api";
import * as btc from "./fixtures/bitcoin-rpc";
import { saveCredentials } from "./fixtures";

const WALLET_NAME = "pw-test.jmdat";
const PASSWORD = "testpassword123";
const MIN_BALANCE_BTC = 0.005;
const JMWALLETD_URL = process.env.JMWALLETD_URL || "https://localhost:29183";
const BITCOIN_RPC_URL = process.env.BITCOIN_RPC_URL || "http://localhost:18443";
const BITCOIN_RPC_USER = process.env.BITCOIN_RPC_USER || "test";
const BITCOIN_RPC_PASS = process.env.BITCOIN_RPC_PASS || "test";

async function waitForJmwalletd(timeoutMs = 120_000): Promise<void> {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    try {
      const res = await fetch(`${JMWALLETD_URL}/api/v1/session`);
      if (res.ok) return;
    } catch {
      // not ready yet
    }
    await new Promise((r) => setTimeout(r, 2_000));
  }
  throw new Error(`jmwalletd at ${JMWALLETD_URL} did not become ready in time`);
}

async function getBalance(token: string): Promise<number> {
  const res = await fetch(`${JMWALLETD_URL}/api/v1/wallet/${WALLET_NAME}/display`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  const data = await res.json();
  return parseFloat(data?.walletinfo?.total_balance ?? "0");
}

async function triggerUtxoRefresh(token: string): Promise<void> {
  // GET /utxos triggers an automatic descriptor wallet refresh in jmwalletd,
  // making any newly received UTXOs visible without an explicit rescan.
  await fetch(`${JMWALLETD_URL}/api/v1/wallet/${WALLET_NAME}/utxos`, {
    headers: { Authorization: `Bearer ${token}` },
  });
}

/**
 * Poll Bitcoin Core until the descriptor wallet has finished its rescan.
 * After `importdescriptors` with background_full_rescan=True, Bitcoin Core
 * does a background rescan that can take several minutes for ~5000 blocks.
 * During this time listunspent returns 0 UTXOs.
 */
async function waitForRescan(
  descriptorWalletName: string,
  timeoutMs = 600_000,
): Promise<void> {
  const start = Date.now();
  console.log(
    `[global-setup] Waiting for Bitcoin Core descriptor wallet '${descriptorWalletName}' rescan...`,
  );
  while (Date.now() - start < timeoutMs) {
    try {
      const res = await fetch(`${BITCOIN_RPC_URL}/wallet/${descriptorWalletName}`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Basic ${Buffer.from(`${BITCOIN_RPC_USER}:${BITCOIN_RPC_PASS}`).toString("base64")}`,
        },
        body: JSON.stringify({ method: "getwalletinfo", params: [], id: 1 }),
      });
      const data = await res.json();
      const scanning = data?.result?.scanning;
      if (scanning === false) {
        console.log("[global-setup] Rescan complete.");
        return;
      }
      const progress =
        typeof scanning === "object" ? ` (${Math.round(scanning.progress * 100)}%)` : "";
      console.log(`[global-setup] Rescan in progress${progress}, waiting...`);
    } catch (err) {
      console.warn("[global-setup] getwalletinfo failed, retrying:", err);
    }
    await new Promise((r) => setTimeout(r, 5_000));
  }
  throw new Error(
    `Bitcoin Core descriptor wallet '${descriptorWalletName}' rescan did not complete in time`,
  );
}

async function waitForBalance(
  token: string,
  minBtc: number,
  timeoutMs = 120_000,
): Promise<void> {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    await triggerUtxoRefresh(token);
    const bal = await getBalance(token);
    if (bal >= minBtc) {
      console.log(`[global-setup] Balance ready: ${bal} BTC`);
      return;
    }
    await new Promise((r) => setTimeout(r, 3_000));
  }
  throw new Error(`Wallet balance did not reach ${minBtc} BTC within timeout`);
}

/**
 * Consolidate UTXOs in the jmwalletd descriptor wallet using Bitcoin Core.
 * Having too many small UTXOs causes "tx-size" errors when building transactions.
 * We collect all UTXOs and build a self-consolidation transaction into a single output.
 */
async function consolidateUtxos(
  jmWalletdToken: string,
  descriptorWalletName: string,
): Promise<void> {
  // Get all UTXOs via Bitcoin Core RPC.
  const listRes = await fetch(`${BITCOIN_RPC_URL}/wallet/${descriptorWalletName}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Basic ${Buffer.from(`${BITCOIN_RPC_USER}:${BITCOIN_RPC_PASS}`).toString("base64")}`,
    },
    body: JSON.stringify({ method: "listunspent", params: [1, 9999999], id: 1 }),
  });
  const listData = await listRes.json();
  const utxos: Array<{ txid: string; vout: number; amount: number }> = listData?.result ?? [];

  if (utxos.length <= 4) {
    console.log(`[global-setup] UTXO count ${utxos.length} is fine, no consolidation needed.`);
    return;
  }

  console.log(`[global-setup] Consolidating ${utxos.length} UTXOs into fewer outputs...`);

  // Get a fresh address from mixdepth 0 to consolidate to.
  const { address: consolidateAddr } = await api.getNewAddress(jmWalletdToken, WALLET_NAME, 0);

  // Consolidate in batches of 100 UTXOs using sendall with explicit inputs.
  // sendall handles fee calculation automatically (no need to pre-subtract fees).
  const BATCH_SIZE = 100;
  let consolidated = 0;
  for (let i = 0; i < utxos.length; i += BATCH_SIZE) {
    const batch = utxos.slice(i, i + BATCH_SIZE);
    if (batch.length <= 1) continue;

    const inputs = batch.map((u) => ({ txid: u.txid, vout: u.vout }));

    const sendRes = await fetch(`${BITCOIN_RPC_URL}/wallet/${descriptorWalletName}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Basic ${Buffer.from(`${BITCOIN_RPC_USER}:${BITCOIN_RPC_PASS}`).toString("base64")}`,
      },
      body: JSON.stringify({
        method: "sendall",
        params: [
          [consolidateAddr], // recipients
          null, // conf_target
          "conservative", // estimate_mode
          null, // fee_rate
          { inputs }, // options: restrict to this batch's inputs
        ],
        id: 1,
      }),
    });
    const sendData = await sendRes.json();
    if (sendData?.result?.txid) {
      consolidated += batch.length;
    } else {
      console.warn(`[global-setup] Batch ${Math.floor(i / BATCH_SIZE) + 1} consolidation failed:`, sendData?.error?.message);
    }
  }

  if (consolidated > 0) {
    console.log(`[global-setup] Consolidated ${consolidated} UTXOs. Mining block...`);
    await btc.mineBlocks(1);
    await new Promise((r) => setTimeout(r, 3_000));
  }
  console.log("[global-setup] Consolidation complete.");
}

export default async function globalSetup(): Promise<void> {
  console.log("\n[global-setup] Waiting for jmwalletd...");
  await waitForJmwalletd();

  // Lock any currently open wallet so we can open ours.
  const session = await api.getSession();
  if (session.session && session.wallet_name && session.wallet_name !== WALLET_NAME) {
    console.log(`[global-setup] Locking existing wallet: ${session.wallet_name}`);
    // Lock requires auth — unlock first to get a valid token.
    try {
      const res = await api.unlockWallet(session.wallet_name, PASSWORD);
      await fetch(`${JMWALLETD_URL}/api/v1/wallet/${session.wallet_name}/lock`, {
        headers: { Authorization: `Bearer ${res.token}` },
      });
    } catch {
      // Unknown password — force-lock by restarting or just proceed.
    }
    await new Promise((r) => setTimeout(r, 1_000));
  }

  // Create or unlock our test wallet.
  // Unlocking imports descriptors into Bitcoin Core — must happen before funding.
  let token: string;
  const wallets = await api.listWallets();
  if (wallets.wallets.includes(WALLET_NAME)) {
    console.log("[global-setup] Unlocking existing wallet...");
    const res = await api.unlockWallet(WALLET_NAME, PASSWORD);
    token = res.token;
  } else {
    console.log("[global-setup] Creating wallet...");
    const res = await api.createWallet(WALLET_NAME, PASSWORD);
    token = res.token;
  }

  // The descriptor import may trigger a background Bitcoin Core rescan.
  // Wait for it to complete before checking/funding the balance.
  const DESCRIPTOR_WALLET_NAME = "jm_playwright_regtest";
  await waitForRescan(DESCRIPTOR_WALLET_NAME);

  // Consolidate UTXOs from prior test runs to avoid "tx-size" errors.
  await consolidateUtxos(token, DESCRIPTOR_WALLET_NAME);

  // Check current balance; skip funding if already sufficient.
  await triggerUtxoRefresh(token);
  const balance = await getBalance(token);
  console.log(`[global-setup] Current balance: ${balance} BTC`);

  if (balance < MIN_BALANCE_BTC) {
    console.log("[global-setup] Funding wallet...");

    // Get a fresh address from mixdepth 0.
    // Descriptors are now imported (wallet is unlocked), so jmwalletd will
    // track any funds sent to this address.
    const { address } = await api.getNewAddress(token, WALLET_NAME, 0);
    console.log(`[global-setup] Funding address: ${address}`);

    // Ensure fidelity_funder has enough balance. On repeated local runs it can
    // be drained; top it up by mining directly to it if needed.
    await btc.ensureFunderFunded(0.05);

    // Send 0.01 BTC from fidelity_funder — enough for all tests, well within
    // the wallet's available balance.
    await btc.sendToAddress(address, 0.01);

    // Mine 1 block to confirm the transaction.
    await btc.mineBlocks(1);

    // Trigger descriptor refresh and wait for balance to appear.
    await waitForBalance(token, MIN_BALANCE_BTC);
  }

  console.log("[global-setup] Wallet ready.");
  saveCredentials({ walletName: WALLET_NAME, password: PASSWORD, token });
}
