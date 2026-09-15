let orderbookData = null;
let sortColumn = 'fidelity_bond_value';
let sortDirection = 'desc';
let offerSelectionProbabilities = new Map();
let activeSelectionProbabilityTrigger = null;

const OFFER_TAB_STORAGE_KEY = 'joinmarket-orderbook-offer-tab';
const OFFER_TAB_NAMES = ['coinjoin', 'podle', 'bond'];
const CREDENTIAL_MARKETS = {
    podle: {
        collection: 'podle_offers',
        label: 'PoDLE',
    },
    bond: {
        collection: 'bond_offers',
        label: 'Fidelity Bond',
    },
};
const credentialMarketState = {
    podle: { searchText: '', sortColumn: 'price_sats', sortDirection: 'asc' },
    bond: { searchText: '', sortColumn: 'price_sats', sortDirection: 'asc' },
};
let activeOfferTab = getPersistedOfferTab();

const DEFAULT_MAKER_COUNT = 9;
const BONDLESS_ALLOWANCE = 0.05;
const SELECTION_SIMULATION_ROUNDS = 200000;
const SELECTABLE_OFFER_TYPES = new Set(['sw0absoffer', 'sw0reloffer']);

const OFFER_TYPE_NAMES = {
    'sw0absoffer': 'SW0 Absolute',
    'sw0reloffer': 'SW0 Relative',
    'swabsoffer': 'SWA Absolute',
    'swreloffer': 'SWA Relative'
};

const FEATURE_DISPLAY_NAMES = {
    'neutrino_compat': 'NEU',
    'push_encrypted': 'PEN',
    'peerlist_features': 'PLF',
    'nick_auth': 'NAU',
    'ping': 'PNG',
    'legacy': 'REF'
};

const FEATURE_COLORS = {
    'neutrino_compat': '#3fb950',
    'push_encrypted': '#a371f7',
    'peerlist_features': '#58a6ff',
    'nick_auth': '#f85149',
    'ping': '#d29922',
    'legacy': '#6e7681'
};

function getFeatureDisplayName(feature) {
    return FEATURE_DISPLAY_NAMES[feature] || feature.replaceAll('_', '-').substring(0, 8);
}

const DIRECTORY_COLORS = [
    '#3498db', '#e74c3c', '#2ecc71', '#f39c12', '#9b59b6',
    '#1abc9c', '#e67e22', '#34495e', '#16a085', '#c0392b',
    '#8e44ad', '#d35400', '#27ae60', '#2980b9', '#f1c40f'
];

function getDirectoryAbbreviation(node) {
    const parts = node.split(':')[0].split('.');
    if (parts.length > 1) {
        return parts[0].substring(0, 3).toUpperCase();
    }
    return node.substring(0, 3).toUpperCase();
}

function getCompactDirectoryName(node) {
    const separator = node.lastIndexOf(':');
    const host = separator === -1 ? node : node.slice(0, separator);
    const port = separator === -1 ? '' : node.slice(separator);
    if (host.length <= 24) return node;
    return `${host.slice(0, 10)}...${host.slice(-8)}${port}`;
}

function getDirectoryColor(node) {
    let hash = 0;
    for (let i = 0; i < node.length; i++) {
        hash = ((hash << 5) - hash) + node.charCodeAt(i);
        hash = hash & hash;
    }
    return DIRECTORY_COLORS[Math.abs(hash) % DIRECTORY_COLORS.length];
}

async function fetchOrderbook() {
    try {
        const response = await fetch('/orderbook.json');
        orderbookData = await response.json();
        offerSelectionProbabilities = calculateOfferSelectionProbabilities(orderbookData.offers);
        updateStats();
        updateDirectoryBreakdown();
        updateFeatureBreakdown();
        renderFeeQuantizationChart();
        updateDirectoryFilter();
        renderTable();
        updateCredentialMarket();
        updateLastUpdate();
    } catch (error) {
        console.error('Failed to fetch orderbook:', error);
    }
}

function getPersistedOfferTab() {
    try {
        const tab = window.localStorage.getItem(OFFER_TAB_STORAGE_KEY);
        return OFFER_TAB_NAMES.includes(tab) ? tab : 'coinjoin';
    } catch (_error) {
        return 'coinjoin';
    }
}

function credentialText(value) {
    if (typeof value === 'string') return value;
    if (typeof value === 'number' && Number.isFinite(value)) return String(value);
    return '';
}

function credentialBody(observedOffer) {
    if (!observedOffer || typeof observedOffer !== 'object') return null;
    const listing = observedOffer.listing;
    if (!listing || typeof listing !== 'object' || !listing.body || typeof listing.body !== 'object') {
        return null;
    }
    return listing.body;
}

function getCredentialOffers(product) {
    if (!orderbookData) return { status: 'loading', offers: [] };

    const market = orderbookData.credential_market;
    const config = CREDENTIAL_MARKETS[product];
    if (!market || typeof market !== 'object' || !Array.isArray(market[config.collection])) {
        return { status: 'unavailable', offers: [] };
    }

    const now = Date.now() / 1000;
    const activeOffers = market[config.collection].filter(observedOffer => {
        const body = credentialBody(observedOffer);
        const expiresAt = Number(body?.expires_at);
        return Number.isFinite(expiresAt) && expiresAt > now;
    });
    return { status: 'ready', offers: activeOffers };
}

function credentialValue(observedOffer, column) {
    const body = credentialBody(observedOffer) || {};
    if (column === 'seller_nick') return credentialText(observedOffer.seller_nick).toLowerCase();
    return Number(body[column]) || 0;
}

function filterAndSortCredentialOffers(product, offers) {
    const state = credentialMarketState[product];
    const filtered = offers.filter(observedOffer => {
        const seller = credentialText(observedOffer.seller_nick).toLowerCase();
        return !state.searchText || seller.includes(state.searchText);
    });

    return filtered.sort((a, b) => {
        const aValue = credentialValue(a, state.sortColumn);
        const bValue = credentialValue(b, state.sortColumn);
        const comparison = typeof aValue === 'string'
            ? aValue.localeCompare(bValue)
            : aValue - bValue;
        return state.sortDirection === 'asc' ? comparison : -comparison;
    });
}

function formatCredentialPrice(value) {
    const price = Number(value);
    return Number.isFinite(price) ? Math.round(price).toLocaleString() : '-';
}

function formatCredentialExpiry(value) {
    const expiresAt = Number(value);
    if (!Number.isFinite(expiresAt)) return '-';
    return new Date(expiresAt * 1000).toLocaleString();
}

function appendCredentialDirectories(row, observedOffer) {
    const directoryValue = appendTableCell(row, 'Directories', '', 'credential-directories');
    const directories = Array.isArray(observedOffer.directory_nodes) ? observedOffer.directory_nodes : [];
    const directoryList = document.createElement('span');
    directoryList.className = 'credential-directory-list';

    for (const directory of directories) {
        const directoryText = credentialText(directory);
        if (!directoryText) continue;
        const directoryElement = document.createElement('span');
        directoryElement.className = 'credential-directory';
        directoryElement.textContent = directoryText;
        directoryElement.title = directoryText;
        directoryList.appendChild(directoryElement);
    }

    if (directoryList.childElementCount === 0) {
        directoryValue.textContent = '-';
    } else {
        directoryValue.appendChild(directoryList);
    }
}

function renderCredentialOfferRows(product, offers) {
    const tbody = document.getElementById(`${product}-offers-tbody`);
    const fragment = document.createDocumentFragment();

    for (const observedOffer of offers) {
        const body = credentialBody(observedOffer) || {};
        const row = document.createElement('tr');
        const seller = credentialText(observedOffer.seller_nick);
        const sellerKey = credentialText(body.seller_pubkey);

        const sellerValue = appendTableCell(row, 'Seller', seller, 'credential-seller');
        sellerValue.title = seller;
        const keyValue = appendTableCell(row, 'Seller Key', sellerKey, 'credential-key');
        keyValue.title = sellerKey;
        appendTableCell(row, 'Price (sats)', formatCredentialPrice(body.price_sats));
        appendTableCell(row, 'Period', credentialText(body.period));
        const expiryValue = appendTableCell(row, 'Expires', formatCredentialExpiry(body.expires_at));
        expiryValue.title = credentialText(body.expires_at);
        appendCredentialDirectories(row, observedOffer);
        fragment.appendChild(row);
    }

    tbody.replaceChildren(fragment);
}

function updateCredentialMarket() {
    Object.keys(CREDENTIAL_MARKETS).forEach(product => {
        const config = CREDENTIAL_MARKETS[product];
        const status = document.getElementById(`${product}-market-status`);
        const count = document.getElementById(`${product}-offer-count`);
        const { status: marketStatus, offers } = getCredentialOffers(product);

        if (marketStatus === 'loading') {
            count.textContent = '-';
            status.textContent = `Loading ${config.label} offers...`;
            renderCredentialOfferRows(product, []);
            return;
        }
        if (marketStatus === 'unavailable') {
            count.textContent = '-';
            status.textContent = `${config.label} offer data is unavailable from this orderbook.`;
            renderCredentialOfferRows(product, []);
            return;
        }

        count.textContent = String(offers.length);
        const visibleOffers = filterAndSortCredentialOffers(product, offers);
        if (offers.length === 0) {
            status.textContent = `No active ${config.label} offers.`;
        } else if (visibleOffers.length === 0) {
            status.textContent = `No ${config.label} offers match this search.`;
        } else {
            status.textContent = `${offers.length} active ${config.label} offer${offers.length === 1 ? '' : 's'}.`;
        }
        renderCredentialOfferRows(product, visibleOffers);
    });
}

function updateStats() {
    if (!orderbookData) return;

    const bondsCount = (orderbookData.fidelitybonds || []).length;
    const uniqueMakers = new Set(orderbookData.offers.map(o => o.counterparty)).size;
    const directMakers = new Set(
        orderbookData.offers
            .filter(o => o.directly_reachable === true)
            .map(o => o.counterparty),
    ).size;

    document.getElementById('total-offers').textContent = orderbookData.offers.length;
    document.getElementById('coinjoin-offer-count').textContent = orderbookData.offers.length;
    document.getElementById('directory-nodes').textContent = orderbookData.directory_nodes.length;
    document.getElementById('fidelity-bonds').textContent = bondsCount;
    document.getElementById('unique-makers').textContent = uniqueMakers;
    const directEl = document.getElementById('direct-makers');
    if (directEl) {
        directEl.textContent = `${directMakers} / ${uniqueMakers}`;
    }
}

function updateDirectoryBreakdown() {
    if (!orderbookData) return;

    const breakdown = document.getElementById('directory-breakdown');
    breakdown.innerHTML = '';

    const stats = orderbookData.directory_stats || {};

    // Sort by: 1) bond_offer_count (desc), 2) uptime_percentage (desc), 3) offer_count (desc)
    const sortedEntries = Object.entries(stats).sort((a, b) => {
        const [, aData] = a;
        const [, bData] = b;

        // Primary: bond offers (descending)
        const bondDiff = (bData.bond_offer_count || 0) - (aData.bond_offer_count || 0);
        if (bondDiff !== 0) return bondDiff;

        // Secondary: uptime percentage (descending)
        const uptimeDiff = (bData.uptime_percentage || 0) - (aData.uptime_percentage || 0);
        if (uptimeDiff !== 0) return uptimeDiff;

        // Tertiary: total offers (descending)
        return (bData.offer_count || 0) - (aData.offer_count || 0);
    });

    sortedEntries.forEach(([node, data]) => {
        const item = document.createElement('div');
        item.className = 'directory-item';

        const nameContainer = document.createElement('div');
        nameContainer.className = 'directory-name-container';

        const abbr = getDirectoryAbbreviation(node);
        const color = getDirectoryColor(node);
        const badge = document.createElement('span');
        badge.className = 'dir-badge';
        badge.style.backgroundColor = color;
        badge.textContent = abbr;
        badge.title = node;

        const statusIcon = document.createElement('span');
        statusIcon.className = 'status-icon';
        if (data.connected) {
            statusIcon.className = 'status-icon status-connected';
            statusIcon.textContent = '●';
            statusIcon.title = 'Connected';
        } else if (data.connection_attempts > 0) {
            statusIcon.className = 'status-icon status-disconnected';
            statusIcon.textContent = '●';
            statusIcon.title = 'Disconnected';
        } else {
            statusIcon.className = 'status-icon status-not-attempted';
            statusIcon.textContent = '●';
            statusIcon.title = 'Not attempted';
        }

        const name = document.createElement('span');
        name.className = 'directory-name';
        name.textContent = node;
        name.dataset.compactName = getCompactDirectoryName(node);
        name.title = node;

        nameContainer.appendChild(statusIcon);
        nameContainer.appendChild(badge);
        nameContainer.appendChild(name);

        const infoContainer = document.createElement('div');
        infoContainer.className = 'directory-info';

        const count = document.createElement('span');
        count.className = 'directory-count';
        count.textContent = `${data.offer_count} offers`;
        infoContainer.appendChild(count);

        if (data.bond_offer_count !== undefined) {
            const bondCount = document.createElement('span');
            bondCount.className = 'directory-bond-count';
            bondCount.textContent = `${data.bond_offer_count} bonds`;
            infoContainer.appendChild(bondCount);
        }

        if (data.uptime_percentage !== undefined) {
            const uptime = document.createElement('span');
            uptime.className = 'directory-uptime';
            uptime.textContent = `${data.uptime_percentage}% uptime`;

            let tooltipText = `${data.successful_connections} successful connections`;
            if (data.tracking_started) {
                const trackingStart = new Date(data.tracking_started);
                tooltipText += `\nTracking since: ${trackingStart.toLocaleString()}`;
            }
            uptime.title = tooltipText;
            infoContainer.appendChild(uptime);
        }

        // Add directory metadata display (version, features, MOTD)
        if (data.proto_ver_min !== undefined || data.features || data.motd) {
            const metadataContainer = document.createElement('div');
            metadataContainer.className = 'directory-metadata';

            // Protocol version
            if (data.proto_ver_min !== undefined) {
                const version = document.createElement('span');
                version.className = 'directory-version';
                if (data.proto_ver_min === data.proto_ver_max) {
                    version.textContent = `v${data.proto_ver_min}`;
                } else {
                    version.textContent = `v${data.proto_ver_min}-${data.proto_ver_max}`;
                }
                version.title = 'Protocol version';
                metadataContainer.appendChild(version);
            }

            // Features
            if (data.features) {
                const featureKeys = Object.keys(data.features).filter(k => data.features[k]);
                if (featureKeys.length > 0) {
                    const features = document.createElement('span');
                    features.className = 'directory-features';
                    features.textContent = featureKeys.map(getFeatureDisplayName).join(', ');
                    features.title = `Directory features: ${featureKeys.join(', ')}`;
                    metadataContainer.appendChild(features);
                }
            }

            // MOTD (shortened, with full text in tooltip)
            if (data.motd) {
                const motd = document.createElement('span');
                motd.className = 'directory-motd';
                const shortMotd = data.motd.length > 30 ? data.motd.substring(0, 30) + '...' : data.motd;
                motd.textContent = shortMotd;
                motd.title = data.motd;
                metadataContainer.appendChild(motd);
            }

            infoContainer.appendChild(metadataContainer);
        }

        item.appendChild(nameContainer);
        item.appendChild(infoContainer);
        breakdown.appendChild(item);
    });
}

let feeQuantMode = 'rel';

const ABS_OFFER_TYPES = new Set(['sw0absoffer', 'swabsoffer']);

// Reference counterparty count for the "max coinjoin size" tooltip stat.
// Matches the common default taker counterparty count; purely informational.
const NEEDED_COUNTERPARTIES = 10;

function formatBtc(sats) {
    return (sats / 1e8).toFixed(4) + ' BTC';
}

// Satoshis are the practical unit for a taker sizing a coinjoin; BTC is shown
// alongside only as a human-scale cross-check.
function formatSats(sats) {
    return `${Math.round(sats).toLocaleString()} sats (${formatBtc(sats)})`;
}

function formatRelPct(relStr) {
    // relStr is a decimal fraction like "0.001" -> "0.1%". parseFloat drops
    // trailing decimal zeros without truncating integers ("10" stays 10%).
    return parseFloat((parseFloat(relStr) * 100).toPrecision(2)) + '%';
}

// Bucket a fee onto the smallest grid entry >= fee (round up). A maker is
// selectable by a taker only when the taker's quantized fee is >= the maker's
// advertised fee, so a maker lands in the smallest quantum that still covers it
// (its "hide set"). Returns the grid index, or -1 when the fee is above the
// largest grid entry (no quantum covers it; the maker is effectively
// unselectable by a quantizing taker).
function ceilGridIndex(value, grid) {
    for (let i = 0; i < grid.length; i++) {
        if (grid[i] >= value) {
            return i;
        }
    }
    return -1;
}

// The maximum coinjoin size achievable with `n` counterparties is bounded by
// the smallest maxsize among the `n` makers with the largest maxsize: picking
// any larger set only lowers that bound. So the practical "how big a coinjoin
// can I do with N makers" number is the Nth-largest maxsize in the pool (or,
// with fewer than N makers available, the smallest of whatever is available).
function maxsizeForCounterparties(values, n) {
    if (values.length === 0) return null;
    const sorted = [...values].sort((a, b) => b - a);
    const idx = Math.min(n, sorted.length) - 1;
    return { value: sorted[idx], available: sorted.length };
}

function hasActiveCertificate(bondData) {
    if (!bondData) return false;
    const currentBlockHeight = orderbookData.current_block_height;
    return Number.isSafeInteger(currentBlockHeight) &&
        Number.isSafeInteger(bondData.cert_expiry) &&
        currentBlockHeight <= bondData.cert_expiry;
}

function findMatchingBond(offer) {
    const bondData = offer.fidelity_bond_data;
    if (!bondData) return null;
    return orderbookData.fidelitybonds.find(
        bond => bond.counterparty === offer.counterparty &&
            bond.utxo.txid === bondData.utxo_txid &&
            bond.utxo.vout === bondData.utxo_vout &&
            bond.locktime === bondData.locktime &&
            bond.utxo_pub === bondData.utxo_pub
    ) || null;
}

function createBondStatusIndicator(className, symbol, warning) {
    const indicator = document.createElement('span');
    indicator.className = className;
    indicator.title = warning;
    indicator.setAttribute('aria-label', warning);
    indicator.textContent = symbol;
    return indicator;
}

function createExpiredBondIndicator() {
    const warning = 'Certificate expired; takers treat this maker as unbonded. ' +
        'Restart the maker to renew the certificate.';
    return createBondStatusIndicator('bond-expired-indicator', '!', warning);
}

function createUnverifiedCertificateIndicator() {
    const warning = 'Certificate expiry could not be verified; takers treat this maker as ' +
        'unbonded until block height is available.';
    return createBondStatusIndicator('bond-unverified-indicator', '?', warning);
}

// Advertised proof data counts only while its certificate is known to be active.
// Expired or height-unverified proofs remain visible in the table and modal but
// do not contribute to sybil-resistant bonded views.
function hasAdvertisedBond(offer) {
    const bondData = offer.fidelity_bond_data;
    if (!bondData) return (offer.fidelity_bond_value || 0) > 0;
    return offer.fidelity_bond_verified !== false &&
        offer.fidelity_bond_verification_stale !== true &&
        hasActiveCertificate(bondData);
}

function hasQuantizedFee(offer) {
    const quantization = orderbookData?.fee_quantization;
    if (!quantization) return true;

    const grid = ABS_OFFER_TYPES.has(offer.ordertype)
        ? quantization.abs_grid
        : quantization.rel_grid;
    if (!Array.isArray(grid)) return true;

    const fee = Number(offer.cjfee);
    return Number.isFinite(fee) && grid.some(quantum => Number(quantum) === fee);
}

function offerSelectionCategory(offer) {
    if (!SELECTABLE_OFFER_TYPES.has(offer.ordertype)) return null;
    if (!hasQuantizedFee(offer)) return null;
    if (hasAdvertisedBond(offer) && (offer.fidelity_bond_value || 0) > 0) return 'bonded';

    const fee = Number(offer.cjfee);
    return Number.isFinite(fee) && fee === 0 ? 'bondless' : null;
}

function getSelectionBondKey(offer) {
    const bondData = offer.fidelity_bond_data;
    if (!bondData || typeof bondData.utxo_txid !== 'string' ||
        !Number.isInteger(bondData.utxo_vout)) return null;
    return `${bondData.utxo_txid}:${bondData.utxo_vout}`;
}

function addFenwickValue(tree, index, delta) {
    for (let position = index + 1; position < tree.length; position += position & -position) {
        tree[position] += delta;
    }
}

function findFenwickIndex(tree, target) {
    const itemCount = tree.length - 1;
    let position = 0;
    let step = 1;
    while (step * 2 <= itemCount) step *= 2;
    for (; step > 0; step = Math.floor(step / 2)) {
        const next = position + step;
        if (next <= itemCount && tree[next] <= target) {
            position = next;
            target -= tree[next];
        }
    }
    return Math.min(position, itemCount - 1);
}

function createSelectionRandom(offers) {
    let state = 2166136261;
    for (const offer of offers) {
        const identity = `${offer.counterparty}:${offer.oid}:${offer.fidelity_bond_value || 0}`;
        for (let i = 0; i < identity.length; i++) {
            state ^= identity.charCodeAt(i);
            state = Math.imul(state, 16777619);
        }
    }
    return () => {
        state += 0x6D2B79F5;
        let value = state;
        value = Math.imul(value ^ value >>> 15, value | 1);
        value ^= value + Math.imul(value ^ value >>> 7, value | 61);
        return ((value ^ value >>> 14) >>> 0) / 4294967296;
    };
}

// Simulate the actual mixed chooser efficiently. Uniform picks use a swap-remove
// array and weighted picks use a Fenwick tree, so each nine-offer round can be
// reset by undoing only the nine removals instead of rebuilding the full pool.
function simulateBondedSelectionProbabilities(candidates) {
    const itemCount = candidates.length;
    const counts = new Uint32Array(itemCount);
    const active = Int32Array.from({ length: itemCount }, (_, index) => index);
    const positions = Int32Array.from({ length: itemCount }, (_, index) => index);
    const zeroFeeIndexes = candidates
        .map((candidate, index) => candidate.zeroFee ? index : -1)
        .filter(index => index >= 0);
    const activeZeroFee = Int32Array.from(zeroFeeIndexes);
    const zeroFeePositions = new Int32Array(itemCount).fill(-1);
    zeroFeeIndexes.forEach((index, position) => {
        zeroFeePositions[index] = position;
    });
    const weights = Float64Array.from(
        candidates,
        candidate => candidate.category === 'bonded'
            ? candidate.offer.fidelity_bond_value || 0
            : 0,
    );
    const tree = new Float64Array(itemCount + 1);
    let initialBondTotal = 0;
    weights.forEach((weight, index) => {
        if (weight > 0) {
            addFenwickValue(tree, index, weight);
            initialBondTotal += weight;
        }
    });
    const random = createSelectionRandom(candidates.map(candidate => candidate.offer));

    for (let round = 0; round < SELECTION_SIMULATION_ROUNDS; round++) {
        let activeCount = itemCount;
        let activeZeroFeeCount = zeroFeeIndexes.length;
        let bondTotal = initialBondTotal;
        const removals = [];

        for (let draw = 0; draw < DEFAULT_MAKER_COUNT; draw++) {
            let index;
            const allowanceSlot = random() < BONDLESS_ALLOWANCE;
            if (allowanceSlot && activeZeroFeeCount > 0) {
                index = activeZeroFee[Math.floor(random() * activeZeroFeeCount)];
            } else if (bondTotal > 0) {
                index = findFenwickIndex(tree, random() * bondTotal);
            } else if (activeZeroFeeCount > 0) {
                index = activeZeroFee[Math.floor(random() * activeZeroFeeCount)];
            } else {
                index = active[Math.floor(random() * activeCount)];
            }
            counts[index] += 1;

            const position = positions[index];
            const lastPosition = activeCount - 1;
            const swappedIndex = active[lastPosition];
            const zeroFeePosition = zeroFeePositions[index];
            const zeroFeeLastPosition = activeZeroFeeCount - 1;
            const swappedZeroFeeIndex = zeroFeePosition >= 0
                ? activeZeroFee[zeroFeeLastPosition]
                : -1;
            removals.push({
                index,
                position,
                lastPosition,
                swappedIndex,
                zeroFeePosition,
                zeroFeeLastPosition,
                swappedZeroFeeIndex,
            });
            active[position] = swappedIndex;
            positions[swappedIndex] = position;
            active[lastPosition] = index;
            positions[index] = lastPosition;
            activeCount -= 1;

            if (zeroFeePosition >= 0) {
                activeZeroFee[zeroFeePosition] = swappedZeroFeeIndex;
                zeroFeePositions[swappedZeroFeeIndex] = zeroFeePosition;
                activeZeroFee[zeroFeeLastPosition] = index;
                zeroFeePositions[index] = zeroFeeLastPosition;
                activeZeroFeeCount -= 1;
            }

            const weight = weights[index];
            if (weight > 0) {
                addFenwickValue(tree, index, -weight);
                bondTotal -= weight;
            }
        }

        for (let i = removals.length - 1; i >= 0; i--) {
            const {
                index,
                position,
                lastPosition,
                swappedIndex,
                zeroFeePosition,
                zeroFeeLastPosition,
                swappedZeroFeeIndex,
            } = removals[i];
            active[position] = index;
            positions[index] = position;
            active[lastPosition] = swappedIndex;
            positions[swappedIndex] = lastPosition;
            if (zeroFeePosition >= 0) {
                activeZeroFee[zeroFeePosition] = index;
                zeroFeePositions[index] = zeroFeePosition;
                activeZeroFee[zeroFeeLastPosition] = swappedZeroFeeIndex;
                zeroFeePositions[swappedZeroFeeIndex] = zeroFeeLastPosition;
            }
            const weight = weights[index];
            if (weight > 0) addFenwickValue(tree, index, weight);
        }
    }

    return counts;
}

// Calculate round-level inclusion probabilities. Symmetric categories are exact;
// unequal bonded weights or mixed bonded fee policies are simulated.
function calculateOfferSelectionProbabilities(offers) {
    const candidates = [];
    const candidatesByBond = new Map();

    for (const offer of offers) {
        const category = offerSelectionCategory(offer);
        if (category === null) continue;

        const bondKey = category === 'bonded' ? getSelectionBondKey(offer) : null;
        let candidate = bondKey === null ? null : candidatesByBond.get(bondKey);
        if (candidate === undefined || candidate === null) {
            candidate = {
                offer,
                offers: [],
                category,
                bondKey,
                zeroFee: Number(offer.cjfee) === 0,
            };
            candidates.push(candidate);
            if (bondKey !== null) candidatesByBond.set(bondKey, candidate);
        }
        candidate.offers.push(offer);
        if (Number(offer.cjfee) === 0) candidate.zeroFee = true;
    }

    const bondedCount = candidates.filter(candidate => candidate.category === 'bonded').length;
    const bondlessCount = candidates.length - bondedCount;
    const poolSize = bondedCount + bondlessCount;
    const zeroFeeBondedCount = candidates.filter(
        candidate => candidate.category === 'bonded' && candidate.zeroFee,
    ).length;
    const mixedBondedFeePolicy = zeroFeeBondedCount > 0 && zeroFeeBondedCount < bondedCount;
    let bondedProbability = null;
    let bondlessProbability = null;

    if (poolSize === DEFAULT_MAKER_COUNT) {
        bondedProbability = bondedCount > 0 ? 1 : null;
        bondlessProbability = bondlessCount > 0 ? 1 : null;
    } else if (poolSize > DEFAULT_MAKER_COUNT && !mixedBondedFeePolicy) {
        const rounds = DEFAULT_MAKER_COUNT;
        const allBondedZeroFee = zeroFeeBondedCount === bondedCount;
        let states = new Map([[`${bondedCount},${bondlessCount}`, 1]]);

        for (let draw = 0; draw < rounds; draw++) {
            const nextStates = new Map();
            for (const [key, stateProbability] of states) {
                const [bondedRemaining, bondlessRemaining] = key.split(',').map(Number);
                const remaining = bondedRemaining + bondlessRemaining;
                const uniformBondedChance = bondedRemaining / remaining;
                let bondedPickChance = 0;
                if (bondedRemaining > 0) {
                    if (bondlessRemaining === 0) {
                        bondedPickChance = 1;
                    } else if (allBondedZeroFee) {
                        bondedPickChance = (1 - BONDLESS_ALLOWANCE) +
                            BONDLESS_ALLOWANCE * uniformBondedChance;
                    } else {
                        bondedPickChance = 1 - BONDLESS_ALLOWANCE;
                    }
                }
                const bondlessPickChance = 1 - bondedPickChance;

                if (bondedPickChance > 0) {
                    const nextKey = `${bondedRemaining - 1},${bondlessRemaining}`;
                    nextStates.set(
                        nextKey,
                        (nextStates.get(nextKey) || 0) + stateProbability * bondedPickChance,
                    );
                }
                if (bondlessPickChance > 0) {
                    const nextKey = `${bondedRemaining},${bondlessRemaining - 1}`;
                    nextStates.set(
                        nextKey,
                        (nextStates.get(nextKey) || 0) + stateProbability * bondlessPickChance,
                    );
                }
            }
            states = nextStates;
        }

        let expectedBondedRemaining = 0;
        let expectedBondlessRemaining = 0;
        for (const [key, stateProbability] of states) {
            const [bondedRemaining, bondlessRemaining] = key.split(',').map(Number);
            expectedBondedRemaining += bondedRemaining * stateProbability;
            expectedBondlessRemaining += bondlessRemaining * stateProbability;
        }
        if (bondedCount > 0) {
            bondedProbability = (bondedCount - expectedBondedRemaining) / bondedCount;
        }
        if (bondlessCount > 0) {
            bondlessProbability = (bondlessCount - expectedBondlessRemaining) / bondlessCount;
        }
    }

    let simulatedCounts = null;
    const bondWeights = new Set(
        candidates
            .filter(candidate => candidate.category === 'bonded')
            .map(candidate => candidate.offer.fidelity_bond_value || 0),
    );
    if (poolSize > DEFAULT_MAKER_COUNT && (bondWeights.size > 1 || mixedBondedFeePolicy)) {
        simulatedCounts = simulateBondedSelectionProbabilities(candidates);
    }

    const probabilities = new Map(
        offers.map(offer => [offer, { probability: 0, sharedBond: false }]),
    );
    candidates.forEach((candidate, candidateIndex) => {
        let probability;
        if (simulatedCounts !== null) {
            probability = simulatedCounts[candidateIndex] / SELECTION_SIMULATION_ROUNDS;
        } else if (candidate.category === 'bonded') {
            probability = bondedProbability;
        } else {
            probability = bondlessProbability;
        }
        const sharedBond = candidate.bondKey !== null && candidate.offers.length > 1;
        candidate.offers.forEach(offer => probabilities.set(offer, { probability, sharedBond }));
    });
    return probabilities;
}

function getOfferSelectionProbability(offer) {
    const estimate = offerSelectionProbabilities.get(offer);
    return estimate === undefined ? 0 : estimate.probability;
}

function getSelectionExclusionReason(offer) {
    if (!SELECTABLE_OFFER_TYPES.has(offer.ordertype)) {
        const offerType = OFFER_TYPE_NAMES[offer.ordertype] || offer.ordertype;
        return `${offerType} offers are excluded; the estimate includes only SW0 offers.`;
    }

    if (!hasQuantizedFee(offer)) {
        return 'This offer fee is not on the public fee grid. Current default takers require ' +
            'quantized fees, so the offer is skipped.';
    }

    const bondData = offer.fidelity_bond_data;
    if (bondData) {
        const currentBlockHeight = orderbookData.current_block_height;
        if (!Number.isSafeInteger(currentBlockHeight) ||
            !Number.isSafeInteger(bondData.cert_expiry)) {
            return 'The fidelity bond certificate expiry cannot be verified because block ' +
                'height or certificate data is unavailable.';
        }
        if (currentBlockHeight > bondData.cert_expiry) {
            return `The fidelity bond certificate expired at block ${bondData.cert_expiry}; ` +
                `the current height is ${currentBlockHeight}.`;
        }
        if (offer.fidelity_bond_verified === false) {
            return 'The fidelity bond failed verification.';
        }
        if (offer.fidelity_bond_verification_stale === true) {
            return 'The fidelity bond verification is stale and must be refreshed.';
        }
        if ((offer.fidelity_bond_value || 0) <= 0) {
            if (offer.fidelity_bond_verified !== true) {
                return 'The fidelity bond is still pending verification.';
            }
            return 'The certificate is active, but the watcher has not established a positive ' +
                'fidelity bond value yet.';
        }
    }

    if (offerSelectionCategory(offer) === null) {
        return 'This is a nonzero-fee bondless offer; only zero-fee bondless offers are ' +
            'included in the estimate.';
    }
    return 'This eligible offer was not selected by the deterministic estimate.';
}

function formatSelectionProbability(offer) {
    const estimate = offerSelectionProbabilities.get(offer) ||
        { probability: 0, sharedBond: false };
    const { probability, sharedBond } = estimate;
    const suffix = sharedBond ? '*' : '';
    const sharedBondNote = sharedBond
        ? ' The asterisk marks a bond-level chance: this offer shares its fidelity bond ' +
            'UTXO with other offers, and takers keep at most one offer for that bond.'
        : '';
    if (probability === null) {
        return {
            text: `N/A${suffix}`,
            title: 'Fewer than 9 qualifying candidates are available, so a 9-maker estimate ' +
                `cannot be calculated.${sharedBondNote}`,
        };
    }
    if (probability <= 0) {
        return {
            text: `0${suffix}`,
            title: getSelectionExclusionReason(offer),
        };
    }

    const interval = 1 / probability;
    const denominator = interval < 10
        ? interval.toFixed(1).replace(/\.0$/, '')
        : Math.round(interval).toLocaleString();
    const percentage = probability < 0.001
        ? (probability * 100).toPrecision(2)
        : (probability * 100).toFixed(1).replace(/\.0$/, '');
    return {
        text: `1/${denominator}${suffix}`,
        title: `${percentage}% chance per 9-maker CoinJoin, assuming every counted offer ` +
            'passes the taker\'s fee and amount limits. Uses the 5% zero-fee allowance ' +
            'and bond-value-weighted selection for bonded slots. Non-quantized fee offers ' +
            'are skipped under current taker defaults.' +
            sharedBondNote,
    };
}

function renderFeeQuantizationChart() {
    const container = document.getElementById('fee-quant-chart');
    if (!container || !orderbookData) return;

    const quant = orderbookData.fee_quantization;
    if (!quant) {
        container.innerHTML = '<p class="fee-quant-empty">Fee grid unavailable.</p>';
        return;
    }

    const isAbs = feeQuantMode === 'abs';
    const grid = isAbs
        ? quant.abs_grid.map(Number)
        : quant.rel_grid.map(Number);

    // Dedupe to one offer per maker for the active offer family. A maker with
    // multiple offers contributes once (its cheapest offer of that family).
    // Only bonded makers are charted: bondless makers are sybil-cheap and would
    // let a single operator skew the bands arbitrarily.
    const perMaker = new Map();
    for (const offer of orderbookData.offers) {
        const offerIsAbs = ABS_OFFER_TYPES.has(offer.ordertype);
        if (offerIsAbs !== isAbs) continue;
        if (!hasAdvertisedBond(offer)) continue;
        const fee = parseFloat(offer.cjfee);
        if (!Number.isFinite(fee)) continue;
        const prev = perMaker.get(offer.counterparty);
        if (prev === undefined || fee < prev.fee) {
            perMaker.set(offer.counterparty, {
                fee,
                bond: offer.fidelity_bond_value || 0,
                maxsize: offer.maxsize || 0,
            });
        }
    }

    // Buckets: one per grid entry, plus an "above grid" overflow for makers
    // whose fee exceeds the largest quantum (unselectable by a quantizing taker).
    // Each band separates makers advertising *exactly* the quantum (they share
    // an anonymity set: their paid fees are identical and unlinkable) from
    // makers below it whose unique fee merely rounds up into the band (still
    // fingerprintable on-chain unless the taker homogenizes fees).
    const newBucket = () => ({ exact: 0, near: 0, bond: 0, maxsizes: [] });
    const buckets = grid.map(newBucket);
    const above = newBucket();
    let totalBond = 0;
    let exactTotal = 0;
    for (const { fee, bond, maxsize } of perMaker.values()) {
        const idx = ceilGridIndex(fee, grid);
        const target = idx === -1 ? above : buckets[idx];
        if (idx !== -1 && fee === grid[idx]) {
            target.exact += 1;
            exactTotal += 1;
        } else {
            target.near += 1;
        }
        target.bond += bond;
        target.maxsizes.push(maxsize);
        totalBond += bond;
    }

    // Cumulative stats reachable at each quantum: a taker that caps its fee at
    // grid entry i can select every maker in bands 0..i. Both the bond-value
    // share and the maxsize pool accumulate across bands for that reason, and
    // are shown in the tooltip only; a second visible data series proved
    // confusing.
    const rows = [];
    let cumBond = 0;
    let cumMaxsizes = [];
    grid.forEach((g, i) => {
        cumBond += buckets[i].bond;
        // concat() returns a fresh array each time, so this row keeps its own
        // stable snapshot even as later iterations keep extending the pool.
        cumMaxsizes = cumMaxsizes.concat(buckets[i].maxsizes);
        rows.push({
            label: isAbs ? (g === 0 ? 'free' : g.toLocaleString()) : formatRelPct(quant.rel_grid[i]),
            raw: isAbs ? g : quant.rel_grid[i],
            cumBondPct: totalBond > 0 ? (cumBond / totalBond) * 100 : null,
            cumMaxsizes,
            ...buckets[i],
        });
    });
    if (above.exact + above.near > 0) {
        rows.push({
            label: '> max',
            cumBondPct: null,
            cumMaxsizes: [],
            ...above,
        });
    }

    const maxCount = Math.max(1, ...rows.map(r => r.exact + r.near));

    container.innerHTML = '';

    // Summary line: the practical nudge for makers. With no data yet (startup,
    // or an empty orderbook) the grid still renders with zero bars so the
    // section never looks broken.
    const summary = document.createElement('p');
    summary.className = 'fq-summary';
    summary.textContent = perMaker.size === 0
        ? 'No bonded makers in the orderbook yet.'
        : `${exactTotal} of ${perMaker.size} bonded makers advertise an exact grid fee.`;
    container.appendChild(summary);

    const chart = document.createElement('div');
    chart.className = 'fq-bars';

    rows.forEach(row => {
        const col = document.createElement('div');
        col.className = 'fq-col';

        const total = row.exact + row.near;

        // Tooltip: band composition, liquidity, and what a taker capped here reaches.
        const feeLabel = row.raw === undefined
            ? null
            : (isAbs ? `${row.raw} sats` : formatRelPct(row.raw));
        const lines = [];
        if (feeLabel !== null) {
            lines.push(`${row.exact} maker(s) exactly at ${feeLabel} (shared anonymity set).`);
            lines.push(`${row.near} maker(s) below it with a unique fee.`);
            if (row.cumBondPct !== null) {
                lines.push(
                    `${row.cumBondPct.toFixed(0)}% of total bonded value is reachable at or under this fee.`
                );
            }
            // Practical for takers: how large a coinjoin they could actually
            // join with NEEDED_COUNTERPARTIES makers if they cap their fee here.
            const mc = maxsizeForCounterparties(row.cumMaxsizes, NEEDED_COUNTERPARTIES);
            if (mc !== null) {
                const avail = mc.available < NEEDED_COUNTERPARTIES
                    ? ` (only ${mc.available} maker(s) at or under this fee)`
                    : '';
                lines.push(
                    `Max coinjoin size with ${NEEDED_COUNTERPARTIES} makers at or under this fee: `
                    + `${formatSats(mc.value)}${avail}.`
                );
            }
        } else {
            lines.push(`${total} maker(s) above the largest quantum.`);
            lines.push('A quantizing taker cannot select these.');
        }
        const tooltip = lines.join('\n');

        const bar = document.createElement('div');
        bar.className = 'fq-bar' + (total === 0 ? ' fq-bar-empty' : '');
        bar.style.height = (total / maxCount * 100) + '%';
        bar.title = tooltip;

        // Stacked segments: solid (exact) at the bottom, faded (near) on top.
        if (total > 0) {
            const nearSeg = document.createElement('div');
            nearSeg.className = 'fq-seg fq-seg-near';
            nearSeg.style.height = (row.near / total * 100) + '%';
            const exactSeg = document.createElement('div');
            exactSeg.className = 'fq-seg fq-seg-exact';
            exactSeg.style.height = (row.exact / total * 100) + '%';
            bar.appendChild(nearSeg);
            bar.appendChild(exactSeg);
        }

        const countLabel = document.createElement('div');
        countLabel.className = 'fq-count';
        countLabel.textContent = total;
        countLabel.title = tooltip;

        const barWrap = document.createElement('div');
        barWrap.className = 'fq-bar-wrap';
        barWrap.appendChild(countLabel);
        barWrap.appendChild(bar);

        const tick = document.createElement('div');
        tick.className = 'fq-tick';
        tick.textContent = row.label;

        col.appendChild(barWrap);
        col.appendChild(tick);
        chart.appendChild(col);
    });

    container.appendChild(chart);

    // Unit caption: makes the active mode's unit explicit on the axis itself.
    const caption = document.createElement('p');
    caption.className = 'fq-axis-caption';
    caption.textContent = isAbs
        ? 'Advertised absolute fee (satoshis per coinjoin)'
        : 'Advertised relative fee (% of coinjoin amount)';
    container.appendChild(caption);

    // Legend: names every visual element so the chart is self-explaining.
    const legend = document.createElement('div');
    legend.className = 'fq-legend';
    const legendItems = [
        ['fq-swatch-exact', 'exactly at the quantum (shared anonymity set)'],
        ['fq-swatch-near', 'below the quantum (unique fee, rounds up into this band)'],
    ];
    for (const [swatchClass, text] of legendItems) {
        const item = document.createElement('span');
        item.className = 'fq-legend-item';
        const swatch = document.createElement('span');
        swatch.className = 'fq-swatch ' + swatchClass;
        item.appendChild(swatch);
        item.appendChild(document.createTextNode(text));
        legend.appendChild(item);
    }
    container.appendChild(legend);
}

function setupFeeQuantToggle() {
    const relBtn = document.getElementById('fee-quant-rel-btn');
    const absBtn = document.getElementById('fee-quant-abs-btn');
    if (!relBtn || !absBtn) return;
    relBtn.addEventListener('click', () => {
        feeQuantMode = 'rel';
        relBtn.classList.add('active');
        absBtn.classList.remove('active');
        renderFeeQuantizationChart();
    });
    absBtn.addEventListener('click', () => {
        feeQuantMode = 'abs';
        absBtn.classList.add('active');
        relBtn.classList.remove('active');
        renderFeeQuantizationChart();
    });
}

function updateFeatureBreakdown() {
    if (!orderbookData) return;

    const breakdown = document.getElementById('feature-breakdown');
    breakdown.innerHTML = '';

    const featureStats = orderbookData.feature_stats || {};
    // Feature share is computed over bonded makers only (issue #483):
    // sybil-cheap bondless makers would otherwise let a single operator
    // skew the percentages arbitrarily. Backend supplies the matching
    // denominator; fall back to counting bonded offers locally for
    // backwards compatibility with older payloads.
    const uniqueMakers = (typeof orderbookData.feature_stats_denominator === 'number')
        ? orderbookData.feature_stats_denominator
        : new Set(
            orderbookData.offers
                .filter(o => (o.fidelity_bond_value || 0) > 0)
                .map(o => o.counterparty)
        ).size;

    // Sort features: legacy first, then by count descending
    const sortedFeatures = Object.entries(featureStats).sort((a, b) => {
        if (a[0] === 'legacy') return -1;
        if (b[0] === 'legacy') return 1;
        return b[1] - a[1];
    });

    sortedFeatures.forEach(([feature, count]) => {
        const item = document.createElement('div');
        item.className = 'feature-item';

        const nameContainer = document.createElement('div');
        nameContainer.className = 'feature-name-container';

        const badge = document.createElement('span');
        badge.className = 'feature-badge';
        badge.style.backgroundColor = FEATURE_COLORS[feature] || '#6e7681';
        badge.textContent = getFeatureDisplayName(feature);
        badge.title = feature;

        nameContainer.appendChild(badge);

        const infoContainer = document.createElement('div');
        infoContainer.className = 'feature-info';

        const countSpan = document.createElement('span');
        countSpan.className = 'feature-count';
        countSpan.textContent = `${count} maker${count !== 1 ? 's' : ''}`;
        infoContainer.appendChild(countSpan);

        if (uniqueMakers > 0) {
            const percentage = document.createElement('span');
            percentage.className = 'feature-percentage';
            percentage.textContent = `${Math.round((count / uniqueMakers) * 100)}%`;
            infoContainer.appendChild(percentage);
        }

        item.appendChild(nameContainer);
        item.appendChild(infoContainer);
        breakdown.appendChild(item);
    });

    // If no features at all, show a message
    if (sortedFeatures.length === 0 && uniqueMakers > 0) {
        const noFeatures = document.createElement('div');
        noFeatures.className = 'feature-item';
        noFeatures.innerHTML = '<span class="feature-no-data">No feature data available yet</span>';
        breakdown.appendChild(noFeatures);
    } else if (uniqueMakers === 0) {
        const noBonded = document.createElement('div');
        noBonded.className = 'feature-item';
        noBonded.innerHTML = '<span class="feature-no-data">No bonded makers in the orderbook yet</span>';
        breakdown.appendChild(noBonded);
    }
}

function updateDirectoryFilter() {
    if (!orderbookData) return;

    const select = document.getElementById('filter-directory');
    const currentValue = select.value;

    select.innerHTML = '<option value="">All</option>';

    orderbookData.directory_nodes.forEach(node => {
        const option = document.createElement('option');
        option.value = node;
        option.textContent = node;
        select.appendChild(option);
    });

    select.value = currentValue;
}

function updateLastUpdate() {
    if (!orderbookData) return;

    const timestamp = new Date(orderbookData.timestamp);
    const formatted = timestamp.toLocaleString();
    document.getElementById('last-update').textContent = `Last update: ${formatted}`;
}

function filterOffers() {
    if (!orderbookData) return [];

    const filterDirectory = document.getElementById('filter-directory').value;
    const searchText = document.getElementById('search-counterparty').value.toLowerCase();

    return orderbookData.offers.filter(offer => {
        if (filterDirectory && !offer.directory_nodes.includes(filterDirectory)) return false;

        if (searchText && !offer.counterparty.toLowerCase().includes(searchText)) return false;

        return true;
    });
}

function sortOffers(offers) {
    const sorted = [...offers];

    sorted.sort((a, b) => {
        let aVal = a[sortColumn];
        let bVal = b[sortColumn];

        if (sortColumn === 'fidelity_bond_value') {
            const aHasBondData = a.fidelity_bond_data ? true : false;
            const bHasBondData = b.fidelity_bond_data ? true : false;
            const aHasValue = a.fidelity_bond_value > 0;
            const bHasValue = b.fidelity_bond_value > 0;

            const aCategory = aHasValue ? 0 : (aHasBondData ? 1 : 2);
            const bCategory = bHasValue ? 0 : (bHasBondData ? 1 : 2);

            if (aCategory !== bCategory) {
                return sortDirection === 'asc'
                    ? bCategory - aCategory
                    : aCategory - bCategory;
            }

            if (aCategory === 0) {
                aVal = a.fidelity_bond_value;
                bVal = b.fidelity_bond_value;
            } else {
                return 0;
            }
        } else if (sortColumn === 'cjfee') {
            const aIsAbsolute = a.ordertype.includes('absoffer');
            const bIsAbsolute = b.ordertype.includes('absoffer');

            if (aIsAbsolute !== bIsAbsolute) {
                return sortDirection === 'asc'
                    ? (aIsAbsolute ? -1 : 1)
                    : (aIsAbsolute ? 1 : -1);
            }

            aVal = parseFloat(aVal);
            bVal = parseFloat(bVal);
        } else if (sortColumn === 'selection_probability') {
            aVal = getOfferSelectionProbability(a) ?? -1;
            bVal = getOfferSelectionProbability(b) ?? -1;
        } else if (typeof aVal === 'string') {
            aVal = aVal.toLowerCase();
            bVal = bVal.toLowerCase();
        }

        if (sortDirection === 'asc') {
            return aVal > bVal ? 1 : aVal < bVal ? -1 : 0;
        } else {
            return aVal < bVal ? 1 : aVal > bVal ? -1 : 0;
        }
    });

    return sorted;
}

function formatFee(offer) {
    const isAbsolute = offer.ordertype.includes('absoffer');

    if (isAbsolute) {
        return `${offer.cjfee} sats`;
    } else {
        const percentage = (parseFloat(offer.cjfee) * 100).toFixed(4);
        return `${percentage}%`;
    }
}

function formatNumber(num) {
    return num.toLocaleString();
}

// Global cache for current block height
let cachedBlockHeight = null;
let blockHeightFetchTime = 0;
const BLOCK_HEIGHT_CACHE_MS = 60000; // Cache for 1 minute

async function fetchCurrentBlockHeight() {
    const serverHeight = orderbookData.current_block_height;
    if (Number.isSafeInteger(serverHeight) && serverHeight >= 0) {
        return serverHeight;
    }
    if (Object.prototype.hasOwnProperty.call(orderbookData, 'current_block_height')) {
        return null;
    }

    const now = Date.now();
    if (cachedBlockHeight !== null && (now - blockHeightFetchTime) < BLOCK_HEIGHT_CACHE_MS) {
        return cachedBlockHeight;
    }

    try {
        let mempoolApi = orderbookData.mempool_url || '';
        if (!mempoolApi) {
            return null;
        }
        const response = await fetch(`${mempoolApi}/api/blocks/tip/height`);
        if (response.ok) {
            const heightText = (await response.text()).trim();
            if (!/^\d+$/.test(heightText)) {
                throw new Error(`Invalid block height response: ${heightText}`);
            }
            const height = Number(heightText);
            if (!Number.isSafeInteger(height)) {
                throw new Error(`Invalid block height response: ${heightText}`);
            }
            cachedBlockHeight = height;
            blockHeightFetchTime = now;
            return cachedBlockHeight;
        }
    } catch (e) {
        console.warn('Failed to fetch block height:', e);
    }
    return null;
}

async function showBondModal(bondData, bondAmount, bondValue) {
    const modal = document.getElementById('bond-modal');
    if (!modal) return;

    // Fetch current block height for validation
    const currentBlockHeight = await fetchCurrentBlockHeight();

    document.getElementById('bond-maker-nick').textContent = bondData.maker_nick;

    let mempoolUrl = orderbookData.mempool_url || '';
    if (window.location.hostname.endsWith('.onion') && orderbookData.mempool_onion_url) {
        mempoolUrl = orderbookData.mempool_onion_url;
    }

    const txidElement = document.getElementById('bond-txid');
    txidElement.innerHTML = `<a href="${mempoolUrl}/tx/${bondData.utxo_txid}" target="_blank">${bondData.utxo_txid}</a>`;

    document.getElementById('bond-vout').textContent = bondData.utxo_vout;

    if (bondAmount > 0) {
        const btcAmount = (bondAmount / 100000000).toFixed(8);
        document.getElementById('bond-amount').textContent = `${formatNumber(bondAmount)} sats (${btcAmount} BTC)`;
    } else {
        document.getElementById('bond-amount').textContent = 'Pending verification...';
    }

    // Format locktime with human-readable date
    const locktimeDate = new Date(bondData.locktime * 1000);
    const now = new Date();
    const isExpired = locktimeDate <= now;
    const locktimeStr = locktimeDate.toISOString().split('T')[0];
    const locktimeStatus = isExpired ? ' (unlockable)' : ` (locked for ${formatTimeUntil(locktimeDate)})`;
    document.getElementById('bond-locktime').textContent = `${locktimeStr}${locktimeStatus}`;

    // UTXO and Certificate public keys
    document.getElementById('bond-utxo-pub').textContent = bondData.utxo_pub;
    document.getElementById('bond-cert-pub').textContent = bondData.cert_pub || 'N/A';

    // Certificate type
    // All implementations (both reference and ours) use delegated certificates
    // with ephemeral cert keypairs, so utxo_pub != cert_pub is the norm.
    // Cold vs hot storage cannot be determined from the wire format alone.
    const certTypeEl = document.getElementById('bond-cert-type');
    certTypeEl.textContent = 'Delegated certificate';

    // Certificate expiry with validation
    const certExpiryBlock = bondData.cert_expiry; // Already in blocks (period * 2016)
    const certExpiryPeriod = Math.floor(certExpiryBlock / 2016);
    let certExpiryStr = `Block ${formatNumber(certExpiryBlock)} (period ${certExpiryPeriod})`;

    let certExpired = false;
    if (currentBlockHeight !== null) {
        if (currentBlockHeight > certExpiryBlock) {
            certExpired = true;
            const blocksAgo = currentBlockHeight - certExpiryBlock;
            certExpiryStr += ` - EXPIRED ${formatNumber(blocksAgo)} blocks ago`;
        } else {
            const blocksRemaining = certExpiryBlock - currentBlockHeight;
            const weeksRemaining = Math.floor(blocksRemaining / 2016) * 2;
            certExpiryStr += ` - ~${weeksRemaining} weeks remaining`;
        }
    }
    document.getElementById('bond-cert-expiry').textContent = certExpiryStr;

    // Scripts
    document.getElementById('bond-redeem-script').textContent = bondData.redeem_script || 'N/A';
    document.getElementById('bond-p2wsh-script').textContent = bondData.p2wsh_script || 'N/A';

    // Verification commands
    document.getElementById('rpc-decodescript').textContent =
        `bitcoin-cli decodescript ${bondData.redeem_script || '<redeem_script>'}`;
    document.getElementById('rpc-gettxout').textContent =
        `bitcoin-cli gettxout ${bondData.utxo_txid} ${bondData.utxo_vout}`;

    // Update verification summary banner
    const summaryEl = document.getElementById('bond-verification-summary');
    const iconEl = document.getElementById('bond-verification-icon');
    const textEl = document.getElementById('bond-verification-text');

    // Remove all status classes
    summaryEl.classList.remove('valid', 'expired', 'invalid', 'pending');

    if (currentBlockHeight === null) {
        summaryEl.classList.add('pending');
        iconEl.textContent = '?';
        const economicValue = bondValue > 0
            ? ` Underlying bond value: ${formatNumber(Math.round(bondValue))}.`
            : '';
        textEl.textContent = 'Certificate expiry could not be verified; takers treat this ' +
            `maker as unbonded until block height is available.${economicValue}`;
    } else if (certExpired) {
        summaryEl.classList.add('expired');
        iconEl.textContent = '!';
        const economicValue = bondValue > 0
            ? ` Underlying bond value: ${formatNumber(Math.round(bondValue))}.`
            : '';
        textEl.textContent = 'Certificate expired; takers treat this maker as unbonded.' +
            `${economicValue} Restart the maker to renew the certificate.`;
    } else if (bondValue > 0) {
        summaryEl.classList.add('valid');
        iconEl.textContent = '\u2713'; // checkmark
        textEl.textContent = `Valid fidelity bond with value ${formatNumber(Math.round(bondValue))}`;
    } else if (bondAmount > 0) {
        summaryEl.classList.add('pending');
        iconEl.textContent = '?';
        textEl.textContent = 'Bond UTXO found but value calculation pending';
    } else {
        summaryEl.classList.add('pending');
        iconEl.textContent = '...';
        textEl.textContent = 'Awaiting UTXO verification from blockchain';
    }

    modal.style.display = 'block';
}

function formatTimeUntil(date) {
    const now = new Date();
    const diffMs = date - now;
    const diffDays = Math.floor(diffMs / (1000 * 60 * 60 * 24));

    if (diffDays > 365) {
        const years = Math.floor(diffDays / 365);
        return `~${years} year${years > 1 ? 's' : ''}`;
    } else if (diffDays > 30) {
        const months = Math.floor(diffDays / 30);
        return `~${months} month${months > 1 ? 's' : ''}`;
    } else {
        return `${diffDays} day${diffDays !== 1 ? 's' : ''}`;
    }
}

function appendTableCell(row, label, text, className = '') {
    const cell = document.createElement('td');
    cell.dataset.label = label;
    if (className) {
        cell.className = className;
    }

    const value = document.createElement('span');
    value.className = 'cell-value';
    value.textContent = String(text);
    cell.appendChild(value);
    row.appendChild(cell);
    return value;
}

function hideSelectionProbabilityTooltip() {
    const tooltip = document.getElementById('selection-probability-tooltip');
    if (tooltip) tooltip.hidden = true;
    if (activeSelectionProbabilityTrigger) {
        activeSelectionProbabilityTrigger.setAttribute('aria-expanded', 'false');
        activeSelectionProbabilityTrigger = null;
    }
}

function showSelectionProbabilityTooltip(cell, trigger, text) {
    const tooltip = document.getElementById('selection-probability-tooltip');
    if (!tooltip) return;

    if (activeSelectionProbabilityTrigger === trigger && !tooltip.hidden) {
        hideSelectionProbabilityTooltip();
        return;
    }

    hideSelectionProbabilityTooltip();
    activeSelectionProbabilityTrigger = trigger;
    trigger.setAttribute('aria-expanded', 'true');
    tooltip.textContent = text;
    tooltip.hidden = false;

    const cellRect = cell.getBoundingClientRect();
    const tooltipRect = tooltip.getBoundingClientRect();
    const margin = 12;
    const gap = 8;
    const left = Math.min(
        Math.max(cellRect.left + cellRect.width / 2 - tooltipRect.width / 2, margin),
        window.innerWidth - tooltipRect.width - margin,
    );
    const spaceBelow = window.innerHeight - cellRect.bottom;
    const top = spaceBelow >= tooltipRect.height + gap + margin
        ? cellRect.bottom + gap
        : Math.max(margin, cellRect.top - tooltipRect.height - gap);
    tooltip.style.left = `${left}px`;
    tooltip.style.top = `${top}px`;
}

function makeSelectionProbabilityInteractive(cell, selectionProbability) {
    const trigger = cell.querySelector('.cell-value');
    const showDetails = () => showSelectionProbabilityTooltip(
        cell,
        trigger,
        selectionProbability.title,
    );
    trigger.tabIndex = 0;
    trigger.setAttribute('role', 'button');
    trigger.setAttribute('aria-controls', 'selection-probability-tooltip');
    trigger.setAttribute('aria-expanded', 'false');
    trigger.setAttribute(
        'aria-label',
        `Pick chance ${selectionProbability.text}. ${selectionProbability.title}`,
    );
    cell.addEventListener('click', event => {
        event.stopPropagation();
        showDetails();
    });
    trigger.addEventListener('keydown', event => {
        if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault();
            showDetails();
        }
    });
}

function renderTable() {
    const tbody = document.getElementById('orderbook-tbody');
    const fragment = document.createDocumentFragment();

    hideSelectionProbabilityTooltip();

    const filtered = filterOffers();
    const sorted = sortOffers(filtered);

    sorted.forEach(offer => {
        const row = document.createElement('tr');

        const typeClass = offer.ordertype.startsWith('sw0') ? 'type-sw0' : 'type-swa';
        const feeClass = offer.ordertype.includes('absoffer') ? 'fee-absolute' : 'fee-relative';
        const selectionProbability = formatSelectionProbability(offer);

        let hasBond = '';
        let bondValue;
        let bondIndicator = null;
        const matchingBond = findMatchingBond(offer);
        const economicBondValue = offer.fidelity_bond_value || matchingBond?.bond_value || 0;
        if (economicBondValue > 0 &&
            (!offer.fidelity_bond_data || hasActiveCertificate(offer.fidelity_bond_data))) {
            hasBond = 'bond-value-clickable';
            bondValue = formatNumber(Math.round(economicBondValue));
        } else if (offer.fidelity_bond_data) {
            hasBond = 'bond-value-clickable';
            const bondAmount = matchingBond?.amount || 0;
            if (!hasActiveCertificate(offer.fidelity_bond_data)) {
                if (Number.isSafeInteger(orderbookData.current_block_height)) {
                    bondValue = economicBondValue > 0
                        ? formatNumber(Math.round(economicBondValue))
                        : 'Expired';
                    if (economicBondValue > 0) {
                        bondIndicator = createExpiredBondIndicator();
                    }
                } else {
                    bondValue = economicBondValue > 0
                        ? formatNumber(Math.round(economicBondValue))
                        : 'Pending';
                    if (economicBondValue > 0) {
                        bondIndicator = createUnverifiedCertificateIndicator();
                    }
                }
            } else {
                bondValue = bondAmount > 0 ? '0' : 'Pending';
            }
        } else {
            bondValue = 'No';
        }

        const directoryBadges = offer.directory_nodes.map(node => {
            const abbr = getDirectoryAbbreviation(node);
            const color = getDirectoryColor(node);
            const badge = document.createElement('span');
            badge.className = 'dir-badge';
            badge.style.backgroundColor = color;
            badge.title = node;
            badge.textContent = abbr;
            return badge;
        });

        // Generate feature badges
        const features = offer.features || {};
        const featureKeys = Object.keys(features).filter(k => features[k]);
        const featureBadges = [];
        if (featureKeys.length === 0) {
            const badge = document.createElement('span');
            badge.className = 'feature-badge feature-legacy';
            badge.title = 'Reference implementation (no features)';
            badge.textContent = 'Ref';
            featureBadges.push(badge);
        } else {
            featureKeys.forEach(feature => {
                const displayName = getFeatureDisplayName(feature);
                const color = FEATURE_COLORS[feature] || '#6e7681';
                const badge = document.createElement('span');
                badge.className = 'feature-badge';
                badge.style.backgroundColor = color;
                badge.title = feature;
                badge.textContent = displayName;
                featureBadges.push(badge);
            });
        }

        // Direct connection indicator: badge appears when we have successfully
        // reached this maker's onion address directly (issue #105). null means
        // not yet checked; false means a check ran and failed.
        let directBadge = null;
        if (offer.directly_reachable === true) {
            directBadge = document.createElement('span');
            directBadge.className = 'direct-badge direct-yes';
            directBadge.title = 'Directly reachable via onion address';
            directBadge.textContent = 'DIRECT';
        } else if (offer.directly_reachable === false) {
            directBadge = document.createElement('span');
            directBadge.className = 'direct-badge direct-no';
            directBadge.title = 'Direct connection attempted and failed';
            directBadge.textContent = 'NO DIRECT';
        }

        appendTableCell(row, 'Type', OFFER_TYPE_NAMES[offer.ordertype], typeClass);
        const counterpartyValue = appendTableCell(
            row, 'Counterparty', offer.counterparty, 'counterparty'
        );
        if (directBadge) {
            counterpartyValue.appendChild(document.createTextNode(' '));
            counterpartyValue.appendChild(directBadge);
        }
        appendTableCell(row, 'Order ID', offer.oid);
        appendTableCell(row, 'Fee', formatFee(offer), feeClass);
        appendTableCell(row, 'Min Size', formatNumber(offer.minsize));
        appendTableCell(row, 'Max Size', formatNumber(offer.maxsize));
        const probabilityValue = appendTableCell(
            row, 'Pick Chance', selectionProbability.text, 'selection-probability'
        );
        const probabilityCell = probabilityValue.parentElement;
        probabilityCell.title = selectionProbability.title;
        makeSelectionProbabilityInteractive(probabilityCell, selectionProbability);
        const bondValueElement = appendTableCell(row, 'Bond Value', bondValue, hasBond);
        if (bondIndicator) {
            bondValueElement.appendChild(document.createTextNode(' '));
            bondValueElement.appendChild(bondIndicator);
        }

        const featureValue = appendTableCell(row, 'Features', '', 'feature-badges');
        featureBadges.forEach(badge => featureValue.appendChild(badge));
        const directoryValue = appendTableCell(row, 'Directories', '', 'directory-badges');
        directoryBadges.forEach(badge => directoryValue.appendChild(badge));

        if (offer.fidelity_bond_data) {
            const bondCell = row.querySelector('.bond-value-clickable');
            const matchingBond = findMatchingBond(offer);
            const bondAmount = matchingBond?.amount || 0;
            const bondVal = offer.fidelity_bond_value || matchingBond?.bond_value || 0;
            const showDetails = () => showBondModal(
                offer.fidelity_bond_data, bondAmount, bondVal
            );
            bondCell.tabIndex = 0;
            bondCell.setAttribute('role', 'button');
            bondCell.setAttribute('aria-label', 'Show fidelity bond details');
            bondCell.addEventListener('click', showDetails);
            bondCell.addEventListener('keydown', event => {
                if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault();
                    showDetails();
                }
            });
        }

        fragment.appendChild(row);
    });

    tbody.innerHTML = '';
    tbody.appendChild(fragment);

    updateSortIndicators();
}

function updateSortIndicators() {
    document.querySelectorAll('th.sortable').forEach(th => {
        th.classList.remove('asc', 'desc');

        if (th.dataset.sort === sortColumn) {
            th.classList.add(sortDirection);
        }
    });

    const mobileSortColumn = document.getElementById('mobile-sort-column');
    const mobileSortDirection = document.getElementById('mobile-sort-direction');
    if (mobileSortColumn) mobileSortColumn.value = sortColumn;
    if (mobileSortDirection) {
        const descending = sortDirection === 'desc';
        mobileSortDirection.textContent = descending ? '↓' : '↑';
        mobileSortDirection.title = descending ? 'Descending' : 'Ascending';
        mobileSortDirection.setAttribute(
            'aria-label',
            descending ? 'Sort descending' : 'Sort ascending',
        );
    }
}

function activateOfferTab(tabName, focusTab = false) {
    const selectedTab = OFFER_TAB_NAMES.includes(tabName) ? tabName : 'coinjoin';
    activeOfferTab = selectedTab;

    OFFER_TAB_NAMES.forEach(name => {
        const tab = document.getElementById(`${name}-offers-tab`);
        const panel = document.getElementById(`${name}-offers-panel`);
        const active = name === selectedTab;
        tab.classList.toggle('active', active);
        tab.setAttribute('aria-selected', String(active));
        tab.tabIndex = active ? 0 : -1;
        panel.hidden = !active;
        if (active && focusTab) tab.focus();
    });

    try {
        window.localStorage.setItem(OFFER_TAB_STORAGE_KEY, selectedTab);
    } catch (_error) {
        // Private browsing and locked-down contexts can deny persistent storage.
    }
}

function setupOfferTabs() {
    const tabs = Array.from(document.querySelectorAll('.offer-tab'));
    tabs.forEach(tab => {
        tab.addEventListener('click', () => activateOfferTab(tab.dataset.offerTab));
        tab.addEventListener('keydown', event => {
            const currentIndex = tabs.indexOf(tab);
            let targetIndex = null;
            if (event.key === 'ArrowRight') targetIndex = (currentIndex + 1) % tabs.length;
            if (event.key === 'ArrowLeft') targetIndex = (currentIndex - 1 + tabs.length) % tabs.length;
            if (event.key === 'Home') targetIndex = 0;
            if (event.key === 'End') targetIndex = tabs.length - 1;
            if (targetIndex === null) return;

            event.preventDefault();
            activateOfferTab(tabs[targetIndex].dataset.offerTab, true);
        });
    });
    activateOfferTab(activeOfferTab);
}

function setupCredentialMarketControls() {
    Object.keys(CREDENTIAL_MARKETS).forEach(product => {
        const search = document.getElementById(`${product}-search`);
        const sort = document.getElementById(`${product}-sort`);
        const direction = document.getElementById(`${product}-sort-direction`);
        const state = credentialMarketState[product];

        search.addEventListener('input', () => {
            state.searchText = search.value.trim().toLowerCase();
            updateCredentialMarket();
        });
        sort.addEventListener('change', () => {
            state.sortColumn = sort.value;
            updateCredentialMarket();
        });
        direction.addEventListener('click', () => {
            state.sortDirection = state.sortDirection === 'asc' ? 'desc' : 'asc';
            const ascending = state.sortDirection === 'asc';
            direction.textContent = ascending ? '\u2191' : '\u2193';
            direction.title = ascending ? 'Sort ascending' : 'Sort descending';
            direction.setAttribute('aria-label', direction.title);
            updateCredentialMarket();
        });
    });
}

function setupEventListeners() {
    document.querySelectorAll('th.sortable').forEach(th => {
        th.addEventListener('click', () => {
            const column = th.dataset.sort;

            if (sortColumn === column) {
                sortDirection = sortDirection === 'asc' ? 'desc' : 'asc';
            } else {
                sortColumn = column;
                sortDirection = 'desc';
            }

            renderTable();
        });
    });

    document.getElementById('filter-directory').addEventListener('change', renderTable);
    document.getElementById('search-counterparty').addEventListener('input', renderTable);

    const mobileSortColumn = document.getElementById('mobile-sort-column');
    const mobileSortDirection = document.getElementById('mobile-sort-direction');
    mobileSortColumn.addEventListener('change', () => {
        sortColumn = mobileSortColumn.value;
        sortDirection = 'desc';
        renderTable();
    });
    mobileSortDirection.addEventListener('click', () => {
        sortDirection = sortDirection === 'asc' ? 'desc' : 'asc';
        renderTable();
    });

    setupFeeQuantToggle();
    setupOfferTabs();
    setupCredentialMarketControls();
    window.setInterval(updateCredentialMarket, 30000);

    const closeModal = document.querySelector('.close-modal');
    if (closeModal) {
        closeModal.addEventListener('click', () => {
            document.getElementById('bond-modal').style.display = 'none';
        });
    }

    window.addEventListener('click', (event) => {
        const modal = document.getElementById('bond-modal');
        if (event.target === modal) {
            modal.style.display = 'none';
        }
        if (activeSelectionProbabilityTrigger &&
            !activeSelectionProbabilityTrigger.parentElement.contains(event.target)) {
            hideSelectionProbabilityTooltip();
        }
    });
    window.addEventListener('keydown', event => {
        if (event.key === 'Escape') hideSelectionProbabilityTooltip();
    });
    window.addEventListener('resize', hideSelectionProbabilityTooltip);
}

setupEventListeners();
fetchOrderbook();
