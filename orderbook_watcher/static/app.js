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

    document.getElementById('total-offers').textContent = orderbookData.offers.length;
    document.getElementById('directory-nodes').textContent = orderbookData.directory_nodes.length;
    document.getElementById('fidelity-bonds').textContent = bondsCount;
    document.getElementById('unique-makers').textContent = uniqueMakers;
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

function updateFeatureBreakdown() {
    if (!orderbookData) return;

    const breakdown = document.getElementById('feature-breakdown');
    breakdown.innerHTML = '';

    const featureStats = orderbookData.feature_stats || {};
    const uniqueMakers = new Set(orderbookData.offers.map(o => o.counterparty)).size;

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

    // Certificate type (self-signed vs cold storage)
    const isSelfSigned = bondData.utxo_pub === bondData.cert_pub;
    const certTypeEl = document.getElementById('bond-cert-type');
    if (isSelfSigned) {
        certTypeEl.textContent = 'Self-signed (hot wallet)';
    } else {
        certTypeEl.textContent = 'Delegated certificate (cold storage)';
    }

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

        row.innerHTML = `
            <td class="${typeClass}">${OFFER_TYPE_NAMES[offer.ordertype]}</td>
            <td class="counterparty">${offer.counterparty}</td>
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
