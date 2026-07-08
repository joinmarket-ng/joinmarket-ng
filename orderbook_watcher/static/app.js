let orderbookData = null;
let sortColumn = 'fidelity_bond_value';
let sortDirection = 'desc';

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
    'legacy': 'REF'
};

const FEATURE_COLORS = {
    'neutrino_compat': '#3fb950',
    'push_encrypted': '#a371f7',
    'peerlist_features': '#58a6ff',
    'legacy': '#6e7681'
};

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
        updateStats();
        updateDirectoryBreakdown();
        updateFeatureBreakdown();
        renderFeeQuantizationChart();
        updateDirectoryFilter();
        renderTable();
        updateLastUpdate();
    } catch (error) {
        console.error('Failed to fetch orderbook:', error);
    }
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
                    features.textContent = featureKeys.map(f =>
                        f.replace('_', '-').substring(0, 8)
                    ).join(', ');
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

function formatBtc(sats) {
    return (sats / 1e8).toFixed(4) + ' BTC';
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

function median(values) {
    if (values.length === 0) return null;
    const sorted = [...values].sort((a, b) => a - b);
    const mid = Math.floor(sorted.length / 2);
    return sorted.length % 2 === 1 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
}

// A maker counts as bonded when it advertises a fidelity bond, even if the bond
// value cannot be computed here (no blockchain/mempool backend): the advertised
// bond is still sybil-resistant. The value only feeds the value-weighted total.
function hasAdvertisedBond(offer) {
    return !!offer.fidelity_bond_data || (offer.fidelity_bond_value || 0) > 0;
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

    if (perMaker.size === 0) {
        container.innerHTML =
            '<p class="fee-quant-empty">No bonded makers in the orderbook yet.</p>';
        return;
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

    // Cumulative bond value reachable at each quantum: a taker that caps its fee
    // at grid entry i can select every maker in bands 0..i. Shown in the tooltip
    // only; a second visible data series proved confusing.
    const rows = [];
    let cumBond = 0;
    grid.forEach((g, i) => {
        cumBond += buckets[i].bond;
        rows.push({
            label: isAbs ? (g === 0 ? 'free' : g.toLocaleString()) : formatRelPct(quant.rel_grid[i]),
            raw: isAbs ? g : quant.rel_grid[i],
            cumBondPct: totalBond > 0 ? (cumBond / totalBond) * 100 : null,
            ...buckets[i],
        });
    });
    if (above.exact + above.near > 0) {
        rows.push({
            label: '> max',
            cumBondPct: null,
            ...above,
        });
    }

    const maxCount = Math.max(1, ...rows.map(r => r.exact + r.near));

    container.innerHTML = '';

    // Summary line: the practical nudge for makers.
    const summary = document.createElement('p');
    summary.className = 'fq-summary';
    summary.textContent =
        `${exactTotal} of ${perMaker.size} bonded makers advertise an exact grid fee.`;
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
        } else {
            lines.push(`${total} maker(s) above the largest quantum.`);
            lines.push('A quantizing taker cannot select these.');
        }
        lines.push(`${formatBtc(row.bond)} bonded in this band.`);
        const medMax = median(row.maxsizes);
        if (medMax !== null) {
            lines.push(`Median max size: ${formatBtc(medMax)}.`);
        }
        if (feeLabel !== null && row.cumBondPct !== null) {
            lines.push(
                `A taker capped at ${feeLabel} reaches ${row.cumBondPct.toFixed(0)}% of bonded value.`
            );
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
        badge.textContent = FEATURE_DISPLAY_NAMES[feature] || feature;
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
    const now = Date.now();
    if (cachedBlockHeight && (now - blockHeightFetchTime) < BLOCK_HEIGHT_CACHE_MS) {
        return cachedBlockHeight;
    }

    try {
        let mempoolApi = orderbookData.mempool_url || '';
        if (!mempoolApi) {
            return null;
        }
        const response = await fetch(`${mempoolApi}/api/blocks/tip/height`);
        if (response.ok) {
            cachedBlockHeight = parseInt(await response.text());
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
    if (currentBlockHeight) {
        if (currentBlockHeight >= certExpiryBlock) {
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

    if (certExpired) {
        summaryEl.classList.add('expired');
        iconEl.textContent = '!';
        textEl.textContent = 'Certificate expired - bond value will show as 0 in reference implementation';
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

function renderTable() {
    const tbody = document.getElementById('orderbook-tbody');
    const fragment = document.createDocumentFragment();

    const filtered = filterOffers();
    const sorted = sortOffers(filtered);

    sorted.forEach(offer => {
        const row = document.createElement('tr');

        const typeClass = offer.ordertype.startsWith('sw0') ? 'type-sw0' : 'type-swa';
        const feeClass = offer.ordertype.includes('absoffer') ? 'fee-absolute' : 'fee-relative';

        let hasBond = '';
        let bondValue;
        if (offer.fidelity_bond_value > 0) {
            hasBond = 'bond-value-clickable';
            bondValue = formatNumber(Math.round(offer.fidelity_bond_value));
        } else if (offer.fidelity_bond_data) {
            hasBond = 'bond-value-clickable';
            const bondAmount = orderbookData.fidelitybonds.find(
                b => b.counterparty === offer.counterparty &&
                     b.utxo.txid === offer.fidelity_bond_data.utxo_txid
            )?.amount || 0;
            bondValue = bondAmount > 0 ? '0' : 'Pending';
        } else {
            bondValue = 'No';
        }

        const directoryBadges = offer.directory_nodes.map(node => {
            const abbr = getDirectoryAbbreviation(node);
            const color = getDirectoryColor(node);
            return `<span class="dir-badge" style="background-color: ${color}" title="${node}">${abbr}</span>`;
        }).join('');

        // Generate feature badges
        const features = offer.features || {};
        const featureKeys = Object.keys(features).filter(k => features[k]);
        let featureBadges;
        if (featureKeys.length === 0) {
            featureBadges = `<span class="feature-badge feature-legacy" title="Reference implementation (no features)">Ref</span>`;
        } else {
            featureBadges = featureKeys.map(feature => {
                const displayName = FEATURE_DISPLAY_NAMES[feature] || feature.substring(0, 8);
                const color = FEATURE_COLORS[feature] || '#6e7681';
                return `<span class="feature-badge" style="background-color: ${color}" title="${feature}">${displayName}</span>`;
            }).join('');
        }

        // Direct connection indicator: badge appears when we have successfully
        // reached this maker's onion address directly (issue #105). null means
        // not yet checked; false means a check ran and failed.
        let directBadge = '';
        if (offer.directly_reachable === true) {
            directBadge = ' <span class="direct-badge direct-yes" title="Directly reachable via onion address">DIRECT</span>';
        } else if (offer.directly_reachable === false) {
            directBadge = ' <span class="direct-badge direct-no" title="Direct connection attempted and failed">NO DIRECT</span>';
        }

        row.innerHTML = `
            <td class="${typeClass}">${OFFER_TYPE_NAMES[offer.ordertype]}</td>
            <td class="counterparty">${offer.counterparty}${directBadge}</td>
            <td>${offer.oid}</td>
            <td class="${feeClass}">${formatFee(offer)}</td>
            <td>${formatNumber(offer.minsize)}</td>
            <td>${formatNumber(offer.maxsize)}</td>
            <td class="${hasBond}">${bondValue}</td>
            <td class="feature-badges">${featureBadges}</td>
            <td class="directory-badges">${directoryBadges}</td>
        `;

        if (offer.fidelity_bond_data) {
            const bondCell = row.querySelector('.bond-value-clickable');
            const bondAmount = orderbookData.fidelitybonds.find(
                b => b.counterparty === offer.counterparty &&
                     b.utxo.txid === offer.fidelity_bond_data.utxo_txid
            )?.amount || 0;
            const bondVal = offer.fidelity_bond_value || 0;
            bondCell.addEventListener('click', () => showBondModal(offer.fidelity_bond_data, bondAmount, bondVal));
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

    setupFeeQuantToggle();

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
    });
}

setupEventListeners();
fetchOrderbook();
