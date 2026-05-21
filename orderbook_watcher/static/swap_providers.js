const NOSTR_KIND_OFFER = 30315;
const NOSTR_D_TAG = "electrum-swapserver-5";
const QUERY_WINDOW_SECONDS = 3600;
const RELAY_TIMEOUT_MS = 6500;

const DEFAULT_RELAYS = [
    "wss://relay.getalby.com/v1",
    "wss://nos.lol",
    "wss://relay.damus.io",
    "wss://brb.io",
    "wss://relay.primal.net",
    "wss://ftp.halifax.rwth-aachen.de/nostr",
    "wss://eu.purplerelay.com",
    "wss://nostr.einundzwanzig.space",
    "wss://nostr.mom"
];

let currentData = {
    mainnet: [],
    signet: [],
    connectedRelays: 0,
    attemptedRelays: 0,
    updatedAt: null
};

function computePowBits(pubkeyHex, nonceHex) {
    try {
        const cleaned = (nonceHex || "").replace(/^0x/i, "");
        if (!cleaned) {
            return 0;
        }

        const padded = cleaned.length % 2 ? `0${cleaned}` : cleaned;
        const combinedHex = `${pubkeyHex}${padded}`;
        const bytes = new Uint8Array(combinedHex.match(/.{1,2}/g).map((b) => parseInt(b, 16)));

        return crypto.subtle.digest("SHA-256", bytes).then((digest) => {
            const digestBytes = new Uint8Array(digest);
            let bits = 0;
            for (const byte of digestBytes) {
                if (byte === 0) {
                    bits += 8;
                    continue;
                }
                let mask = 0x80;
                while ((byte & mask) === 0) {
                    bits += 1;
                    mask >>= 1;
                }
                break;
            }
            return bits;
        });
    } catch (_e) {
        return Promise.resolve(0);
    }
}

function formatNumber(value) {
    return Number(value || 0).toLocaleString();
}

function shortPubkey(pubkey) {
    if (!pubkey || pubkey.length < 16) {
        return pubkey || "-";
    }
    return `${pubkey.slice(0, 12)}...${pubkey.slice(-8)}`;
}

function formatTimestamp(unixTs) {
    if (!unixTs) {
        return "-";
    }
    return new Date(unixTs * 1000).toLocaleString();
}

function getTagValue(tags, key) {
    if (!Array.isArray(tags)) {
        return null;
    }
    for (const tag of tags) {
        if (Array.isArray(tag) && tag[0] === key && typeof tag[1] === "string") {
            return tag[1];
        }
    }
    return null;
}

function parseOfferContent(content) {
    try {
        const parsed = JSON.parse(content || "{}");
        const relays = String(parsed.relays || "")
            .split(",")
            .map((relay) => relay.trim())
            .filter(Boolean);

        return {
            percentageFee: Number(parsed.percentage_fee || 0),
            miningFee: Number(parsed.mining_fee || 0),
            minAmount: Number(parsed.min_amount || 0),
            maxReverseAmount: Number(parsed.max_reverse_amount || 0),
            relays,
            powNonce: String(parsed.pow_nonce || "")
        };
    } catch (_e) {
        return null;
    }
}

async function queryRelayForNetwork(relayUrl, network) {
    const now = Math.floor(Date.now() / 1000);
    const filter = {
        kinds: [NOSTR_KIND_OFFER],
        limit: 50,
        "#d": [NOSTR_D_TAG],
        "#r": [`net:${network}`],
        since: now - QUERY_WINDOW_SECONDS,
        until: now + QUERY_WINDOW_SECONDS
    };

    return new Promise((resolve) => {
        const offers = [];
        const pendingParses = [];
        const subId = `swap-${network}-${Math.random().toString(16).slice(2)}`;
        let resolved = false;
        let ws;

        const finish = (connected) => {
            if (resolved) {
                return;
            }
            resolved = true;
            try {
                if (ws && ws.readyState === WebSocket.OPEN) {
                    ws.send(JSON.stringify(["CLOSE", subId]));
                    ws.close();
                }
            } catch (_e) {
                // no-op
            }
            resolve({
                relay: relayUrl,
                connected,
                offers
            });
        };

        const timer = setTimeout(() => finish(false), RELAY_TIMEOUT_MS);

        try {
            ws = new WebSocket(relayUrl);
        } catch (_e) {
            clearTimeout(timer);
            finish(false);
            return;
        }

        ws.onopen = () => {
            ws.send(JSON.stringify(["REQ", subId, filter]));
        };

        ws.onmessage = (event) => {
            let message;
            try {
                message = JSON.parse(event.data);
            } catch (_e) {
                return;
            }

            if (!Array.isArray(message) || message.length < 2) {
                return;
            }

            if (message[0] === "EVENT" && message[1] === subId && message[2]) {
                const ev = message[2];
                if (ev.kind !== NOSTR_KIND_OFFER || typeof ev.pubkey !== "string") {
                    return;
                }

                if (getTagValue(ev.tags, "d") !== NOSTR_D_TAG) {
                    return;
                }

                if (getTagValue(ev.tags, "r") !== `net:${network}`) {
                    return;
                }

                const parsed = parseOfferContent(ev.content);
                if (!parsed) {
                    return;
                }

                const parsePromise = computePowBits(ev.pubkey, parsed.powNonce).then((powBits) => {
                    offers.push({
                        pubkey: ev.pubkey,
                        createdAt: Number(ev.created_at || 0),
                        powBits,
                        percentageFee: parsed.percentageFee,
                        miningFee: parsed.miningFee,
                        minAmount: parsed.minAmount,
                        maxReverseAmount: parsed.maxReverseAmount,
                        relays: parsed.relays
                    });
                });
                pendingParses.push(parsePromise);
                return;
            }

            if (message[0] === "EOSE" && message[1] === subId) {
                clearTimeout(timer);
                Promise.allSettled(pendingParses).then(() => finish(true));
            }
        };

        ws.onerror = () => {
            clearTimeout(timer);
            finish(false);
        };

        ws.onclose = () => {
            if (!resolved) {
                clearTimeout(timer);
                finish(false);
            }
        };
    });
}

function deduplicateOffers(offers) {
    const byPubkey = new Map();
    for (const offer of offers) {
        const existing = byPubkey.get(offer.pubkey);
        if (!existing) {
            byPubkey.set(offer.pubkey, offer);
            continue;
        }

        if (offer.createdAt > existing.createdAt) {
            byPubkey.set(offer.pubkey, offer);
        } else if (offer.createdAt === existing.createdAt && offer.powBits > existing.powBits) {
            byPubkey.set(offer.pubkey, offer);
        }
    }

    return Array.from(byPubkey.values()).sort((a, b) => {
        if (b.powBits !== a.powBits) {
            return b.powBits - a.powBits;
        }
        if (a.percentageFee !== b.percentageFee) {
            return a.percentageFee - b.percentageFee;
        }
        return a.miningFee - b.miningFee;
    });
}

function getFilteredOffers(offers) {
    const search = document.getElementById("provider-search").value.trim().toLowerCase();
    const minPow = Number(document.getElementById("min-pow").value || 0);

    return offers.filter((offer) => {
        if (offer.powBits < minPow) {
            return false;
        }
        if (!search) {
            return true;
        }
        if (offer.pubkey.toLowerCase().includes(search)) {
            return true;
        }
        return offer.relays.some((relay) => relay.toLowerCase().includes(search));
    });
}

function renderOffersTable(tbodyId, offers) {
    const tbody = document.getElementById(tbodyId);
    tbody.innerHTML = "";

    if (offers.length === 0) {
        const row = document.createElement("tr");
        row.className = "empty-row";
        const cell = document.createElement("td");
        cell.colSpan = 8;
        cell.textContent = "No offers match current filters.";
        row.appendChild(cell);
        tbody.appendChild(row);
        return;
    }

    for (const offer of offers) {
        const row = document.createElement("tr");

        const pubkeyCell = document.createElement("td");
        pubkeyCell.className = "provider-pubkey";
        pubkeyCell.title = offer.pubkey;
        pubkeyCell.textContent = shortPubkey(offer.pubkey);

        const powCell = document.createElement("td");
        powCell.className = "provider-pow";
        powCell.textContent = String(offer.powBits);

        const feeCell = document.createElement("td");
        feeCell.className = "provider-fee";
        feeCell.textContent = `${offer.percentageFee.toFixed(2)}%`;

        const miningFeeCell = document.createElement("td");
        miningFeeCell.textContent = `${formatNumber(offer.miningFee)} sats`;

        const minAmountCell = document.createElement("td");
        minAmountCell.textContent = formatNumber(offer.minAmount);

        const maxReverseCell = document.createElement("td");
        maxReverseCell.textContent = formatNumber(offer.maxReverseAmount);

        const relaysCell = document.createElement("td");
        relaysCell.className = "provider-relays";
        const relayList = document.createElement("div");
        relayList.className = "relay-list";
        if (offer.relays.length === 0) {
            const relayPill = document.createElement("span");
            relayPill.className = "relay-pill";
            relayPill.textContent = "(none advertised)";
            relayList.appendChild(relayPill);
        } else {
            for (const relay of offer.relays.slice(0, 4)) {
                const relayPill = document.createElement("span");
                relayPill.className = "relay-pill";
                relayPill.title = relay;
                relayPill.textContent = relay;
                relayList.appendChild(relayPill);
            }
        }
        relaysCell.appendChild(relayList);

        const timeCell = document.createElement("td");
        timeCell.className = "time-col";
        timeCell.textContent = formatTimestamp(offer.createdAt);

        row.appendChild(pubkeyCell);
        row.appendChild(powCell);
        row.appendChild(feeCell);
        row.appendChild(miningFeeCell);
        row.appendChild(minAmountCell);
        row.appendChild(maxReverseCell);
        row.appendChild(relaysCell);
        row.appendChild(timeCell);

        tbody.appendChild(row);
    }
}

function renderStatus(network, filteredCount, totalCount) {
    const statusElement = document.getElementById(`${network}-status`);
    statusElement.textContent = `${filteredCount} shown, ${totalCount} discovered across ${currentData.connectedRelays}/${currentData.attemptedRelays} connected relays.`;
}

function renderAll() {
    const filteredMainnet = getFilteredOffers(currentData.mainnet);
    const filteredSignet = getFilteredOffers(currentData.signet);

    renderOffersTable("mainnet-tbody", filteredMainnet);
    renderOffersTable("signet-tbody", filteredSignet);

    renderStatus("mainnet", filteredMainnet.length, currentData.mainnet.length);
    renderStatus("signet", filteredSignet.length, currentData.signet.length);

    document.getElementById("mainnet-count").textContent = filteredMainnet.length;
    document.getElementById("signet-count").textContent = filteredSignet.length;
    document.getElementById("relay-count").textContent = `${currentData.connectedRelays}/${currentData.attemptedRelays}`;

    if (currentData.updatedAt) {
        document.getElementById("updated-at").textContent = `Updated: ${currentData.updatedAt.toLocaleString()}`;
    }
}

async function refreshOffers() {
    const refreshButton = document.getElementById("refresh-offers");
    refreshButton.disabled = true;
    refreshButton.textContent = "Refreshing...";

    const networks = ["mainnet", "signet"];
    const allResults = await Promise.all(
        networks.map(async (network) => {
            const results = await Promise.all(
                DEFAULT_RELAYS.map((relay) => queryRelayForNetwork(relay, network))
            );
            return { network, results };
        })
    );

    let connectedRelays = 0;
    let attemptedRelays = 0;
    const nextData = {
        mainnet: [],
        signet: [],
        connectedRelays: 0,
        attemptedRelays: 0,
        updatedAt: new Date()
    };

    for (const networkResult of allResults) {
        const networkOffers = [];
        for (const relayResult of networkResult.results) {
            attemptedRelays += 1;
            if (relayResult.connected) {
                connectedRelays += 1;
            }
            networkOffers.push(...relayResult.offers);
        }
        nextData[networkResult.network] = deduplicateOffers(networkOffers);
    }

    nextData.connectedRelays = connectedRelays;
    nextData.attemptedRelays = attemptedRelays;
    currentData = nextData;

    renderAll();

    refreshButton.disabled = false;
    refreshButton.textContent = "Refresh";
}

function setupListeners() {
    document.getElementById("refresh-offers").addEventListener("click", () => {
        refreshOffers();
    });

    document.getElementById("provider-search").addEventListener("input", renderAll);
    document.getElementById("min-pow").addEventListener("input", renderAll);
}

setupListeners();
refreshOffers();
