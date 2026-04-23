/**
 * JoinMarket walletd API helper for Playwright E2E tests.
 *
 * Used ONLY for test setup/teardown (creating/unlocking a wallet, mining,
 * checking session state). All user interactions must go through the browser.
 *
 * jmwalletd presents a self-signed TLS cert in the e2e stack. The playwright
 * runner relaxes Node's TLS verification via ``NODE_TLS_REJECT_UNAUTHORIZED=0``
 * (set by ``run-local.sh`` and the CI workflow) so the global ``fetch`` works
 * against the self-signed endpoint without additional dispatcher plumbing.
 */

const JMWALLETD_URL = process.env.JMWALLETD_URL || "https://localhost:29183";

interface CreateWalletResponse {
  walletname: string;
  token: string;
  refresh_token: string;
  seedphrase: string;
}

interface UnlockWalletResponse {
  walletname: string;
  token: string;
  refresh_token: string;
}

interface SessionResponse {
  session: boolean;
  maker_running: boolean;
  coinjoin_in_process: boolean;
  wallet_name: string;
  offer_list: Array<Record<string, string | number>> | null;
  nickname: string | null;
  rescanning: boolean;
  block_height: number | null;
}

async function apiFetch(
  path: string,
  options: RequestInit & { token?: string } = {},
): Promise<Response> {
  const { token, ...fetchOptions } = options;
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...((fetchOptions.headers as Record<string, string>) || {}),
  };
  return fetch(`${JMWALLETD_URL}${path}`, { ...fetchOptions, headers });
}

async function api<T = unknown>(
  path: string,
  options: RequestInit & { token?: string } = {},
): Promise<T> {
  const res = await apiFetch(path, options);
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(
      `jmwalletd ${options.method || "GET"} ${path} → ${res.status}: ${body}`,
    );
  }
  const text = await res.text();
  return text ? JSON.parse(text) : ({} as T);
}

/** Create a new wallet. Fails with 401 if another wallet is already open. */
export async function createWallet(
  walletName: string,
  password: string,
): Promise<CreateWalletResponse> {
  return api<CreateWalletResponse>("/api/v1/wallet/create", {
    method: "POST",
    body: JSON.stringify({ walletname: walletName, password, wallettype: "sw-fb" }),
  });
}

/** Unlock an existing wallet. */
export async function unlockWallet(
  walletName: string,
  password: string,
): Promise<UnlockWalletResponse> {
  return api<UnlockWalletResponse>(`/api/v1/wallet/${walletName}/unlock`, {
    method: "POST",
    body: JSON.stringify({ password }),
  });
}

/**
 * Force-unlock a wallet even if another wallet is currently open.
 * If a different wallet is open, it will be locked first.
 */
export async function forceUnlock(
  walletName: string,
  password: string,
): Promise<UnlockWalletResponse> {
  // First attempt a normal unlock.
  try {
    return await unlockWallet(walletName, password);
  } catch {
    // If that failed, a different wallet may be open. Lock it first.
    const session = await getSession();
    if (session.wallet_name) {
      // Lock by calling the lock endpoint. Lock requires a token but we may
      // not have one — use the known walletname from session without a token.
      // If lock returns 401, fall through; the wallet may have been
      // concurrently closed.
      const lockUrl = `${JMWALLETD_URL}/api/v1/wallet/${session.wallet_name}/lock`;
      await fetch(lockUrl, { method: "GET" }).catch(() => null);
    }
    // Retry unlock.
    return unlockWallet(walletName, password);
  }
}

/** Lock the current wallet. Accepts (token, walletName) or just (token) when wallet is known from session. */
export async function lockWallet(token: string, walletName?: string): Promise<void> {
  let name = walletName;
  if (!name) {
    const session = await getSession(token);
    name = session.wallet_name;
    if (!name) throw new Error("lockWallet: no wallet currently open");
  }
  await api(`/api/v1/wallet/${name}/lock`, { token });
}

/** Get session info (no auth required). */
export async function getSession(token?: string): Promise<SessionResponse> {
  return api<SessionResponse>("/api/v1/session", { token });
}

/** List available wallets. */
export async function listWallets(): Promise<{ wallets: string[] }> {
  return api<{ wallets: string[] }>("/api/v1/wallet/all");
}

/** Get a new receive address for the given mixdepth.
 *  Accepts (token, walletName, mixdepth) or (token, mixdepth) — walletName resolved from session when omitted. */
export async function getNewAddress(
  token: string,
  walletNameOrMixdepth: string | number,
  mixdepthArg?: number,
): Promise<{ address: string }> {
  let walletName: string;
  let mixdepth: number;

  if (typeof walletNameOrMixdepth === "number") {
    mixdepth = walletNameOrMixdepth;
    const session = await getSession(token);
    if (!session.wallet_name) throw new Error("getNewAddress: no wallet currently open");
    walletName = session.wallet_name;
  } else {
    walletName = walletNameOrMixdepth;
    mixdepth = mixdepthArg!;
  }

  return api<{ address: string }>(
    `/api/v1/wallet/${walletName}/address/new/${mixdepth}`,
    { token },
  );
}

interface DirectSendResponse {
  txinfo: Record<string, unknown>;
  txid: string;
}

interface CoinjoinRequest {
  mixdepth: number;
  amount_sats: number;
  counterparties: number;
  destination: string;
  txfee?: number;
}

interface WalletDisplayEntry {
  hd_path: string;
  address: string;
  amount: string;
  status: string;
  label: string;
}


interface WalletDisplayBranch {
  branch: string;
  balance: string;
  entries: WalletDisplayEntry[];
}

interface WalletDisplayAccount {
  account: string;
  account_balance: string;
  branches: WalletDisplayBranch[];
}

interface WalletDisplayResponse {
  walletinfo: {
    wallet_name: string;
    total_balance: string;
    accounts: WalletDisplayAccount[];
  };
}

/**
 * Poll the session endpoint until a predicate is satisfied.
 * Signature: (token, predicate, timeoutMs?, intervalMs?) for use in specs.
 * Returns the matching session.
 */
export async function waitForSession(
  tokenOrPredicate: string | ((s: SessionResponse) => boolean),
  predicateOrTimeout?: ((s: SessionResponse) => boolean) | number,
  timeoutMs = 60_000,
  intervalMs = 2_000,
): Promise<SessionResponse> {
  let token: string | undefined;
  let predicate: (s: SessionResponse) => boolean;

  if (typeof tokenOrPredicate === "string") {
    token = tokenOrPredicate;
    predicate = predicateOrTimeout as (s: SessionResponse) => boolean;
    if (typeof predicateOrTimeout === "number") {
      // Called as (token, timeoutMs) — not supported, treat as no predicate
      throw new Error("waitForSession: second argument must be a predicate function");
    }
  } else {
    predicate = tokenOrPredicate;
    timeoutMs = typeof predicateOrTimeout === "number" ? predicateOrTimeout : timeoutMs;
  }

  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    try {
      const session = await getSession(token);
      if (predicate(session)) return session;
    } catch {
      // not ready yet
    }
    await new Promise((r) => setTimeout(r, intervalMs));
  }
  throw new Error("Timed out waiting for session condition");
}

/** Get wallet display (balances and address info). */
export async function getWalletDisplay(
  token: string,
  walletName?: string,
): Promise<WalletDisplayResponse> {
  let name = walletName;
  if (!name) {
    const session = await getSession(token);
    name = session.wallet_name;
    if (!name) throw new Error("getWalletDisplay: no wallet currently open");
  }
  console.log(`[jmwalletd-api] getWalletDisplay for wallet: ${name}`);
  const res = await api<WalletDisplayResponse>(`/api/v1/wallet/${name}/display`, { token });
  console.log(`[jmwalletd-api] getWalletDisplay balance: ${res.walletinfo?.total_balance}`);
  return res;
}

/** Get UTXOs (triggers descriptor refresh). */
export async function getUtxos(token: string, walletName?: string): Promise<unknown> {
  let name = walletName;
  if (!name) {
    const session = await getSession(token);
    name = session.wallet_name;
    if (!name) throw new Error("getUtxos: no wallet currently open");
  }
  console.log(`[jmwalletd-api] getUtxos for wallet: ${name}`);
  return api(`/api/v1/wallet/${name}/utxos`, { token });
}

/**
 * Wait until the wallet reports a non-zero balance (descriptor scan may lag).
 * Calls getUtxos first to trigger the scan, then polls getWalletDisplay.
 */
export async function waitForBalance(
  token: string,
  minBalanceBtc = 0.001,
  timeoutMs = 120_000,
): Promise<void> {
  const start = Date.now();
  await getUtxos(token);
  while (Date.now() - start < timeoutMs) {
    try {
      const display = await getWalletDisplay(token);
      const balance = parseFloat(display.walletinfo.total_balance);
      if (balance >= minBalanceBtc) {
        console.log(`[jmwalletd-api] balance ready: ${balance} BTC`);
        return;
      }
      console.log(`[jmwalletd-api] waiting for balance (${balance} < ${minBalanceBtc}), retrying...`);
    } catch {
      // not ready yet
    }
    await new Promise((r) => setTimeout(r, 3_000));
    await getUtxos(token);
  }
  throw new Error(`Timed out waiting for balance >= ${minBalanceBtc} BTC`);
}

/** Send bitcoin directly (non-collaborative). */
export async function directSend(
  token: string,
  mixdepth: number,
  address: string,
  amount: number,
  walletName?: string,
): Promise<DirectSendResponse> {
  let name = walletName;
  if (!name) {
    const session = await getSession(token);
    name = session.wallet_name;
    if (!name) throw new Error("directSend: no wallet currently open");
  }
  const res = await api<any>(`/api/v1/wallet/${name}/taker/direct-send`, {
    method: "POST",
    token,
    body: JSON.stringify({ mixdepth, destination: address, amount_sats: amount }),
  });

  // Handle both { txid: "..." } and { txinfo: { txid: "..." } } formats
  const txid = res.txid || res.txinfo?.txid;
  return { txid, txinfo: res.txinfo || {} };
}

/** Start a collaborative send (coinjoin taker flow). */
export async function startCoinjoin(
  token: string,
  req: CoinjoinRequest,
  walletName?: string,
): Promise<void> {
  let name = walletName;
  if (!name) {
    const session = await getSession(token);
    name = session.wallet_name;
    if (!name) throw new Error("startCoinjoin: no wallet currently open");
  }
  await api(`/api/v1/wallet/${name}/taker/coinjoin`, {
    method: "POST",
    token,
    body: JSON.stringify(req),
  });
}

/** Stop a running collaborative send/tumbler. */
export async function stopCoinjoin(token: string, walletName?: string): Promise<void> {
  let name = walletName;
  if (!name) {
    const session = await getSession(token);
    name = session.wallet_name;
    if (!name) throw new Error("stopCoinjoin: no wallet currently open");
  }
  await api(`/api/v1/wallet/${name}/taker/stop`, { token });
}

/** Start the maker (earn) bot. All numeric fields must be passed as strings per the API spec. */
export async function startMaker(
  token: string,
  orderConfig: {
    ordertype: string;
    cjfee_a: number | string;
    cjfee_r?: number | string;
    txfee?: number | string;
    minsize: number | string;
  },
  walletName?: string,
): Promise<void> {
  let name = walletName;
  if (!name) {
    const session = await getSession(token);
    name = session.wallet_name;
    if (!name) throw new Error("startMaker: no wallet currently open");
  }
  // API requires all numeric fields as strings
  const body = {
    ordertype: orderConfig.ordertype,
    cjfee_a: String(orderConfig.cjfee_a),
    cjfee_r: String(orderConfig.cjfee_r ?? "0.0002"),
    txfee: String(orderConfig.txfee ?? "0"),
    minsize: String(orderConfig.minsize),
  };
  await api(`/api/v1/wallet/${name}/maker/start`, {
    method: "POST",
    token,
    body: JSON.stringify(body),
  });
}

/** Stop the maker (earn) bot. */
export async function stopMaker(token: string, walletName?: string): Promise<void> {
  let name = walletName;
  if (!name) {
    const session = await getSession(token);
    name = session.wallet_name;
    if (!name) throw new Error("stopMaker: no wallet currently open");
  }
  await api(`/api/v1/wallet/${name}/maker/stop`, { token });
}

export type {
  CreateWalletResponse,
  UnlockWalletResponse,
  SessionResponse,
  WalletDisplayResponse,
  DirectSendResponse,
};
