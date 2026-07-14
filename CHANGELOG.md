# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.34.0] - 2026-07-14

Automatic history import for existing wallets, fee quantization defaults for better privacy, coin control labels, local mempool API support, bug fixes, and security hardening.

### Added

- Detect CoinJoin transactions from their output structure ([3ec85e3d](../../commit/3ec85e3d5f0ad18c210e6fae367c7cf13f7402f9))
- Show cj-out/cj-change for imported wallets by reconstructing CoinJoin labels from on-chain data ([ea805be6](../../commit/ea805be6cc4b4d56e2672b636ef0c138aa1a52db))
- Show fidelity bond addresses that hold more than one UTXO in list-bonds, including the extra locked coins and the total locked at the address ([63ef4778](../../commit/63ef4778e51cae2fec0b468fbba3c6b9cbf18c67))
- Add shared fee-quantization grid primitives in jmcore ([52702271](../../commit/52702271eaa907c1c79dcd02f2a8c94b0291e6c4))
- Show a fee-quantization bands chart in the orderbook watcher ([d60612a7](../../commit/d60612a7446b7119501549fc8795df122dac064c))
- Add a zero (free-maker) band to the absolute fee quantization grid ([527329c2](../../commit/527329c28213172a36a3b361805ef0c4496d575f))
- Count advertised bonds without a backend and chart makers in their rounded-up fee band ([276c7c03](../../commit/276c7c03c2ac4a1106414d2b4e7a0ea050b6a652))
- Fee quantization chart shows cumulative reachable bond share per band ([864e93e1](../../commit/864e93e1dcdb7b1bbfb51b3d3b355c473850c3d6))
- Default maker fee sits on a quantization quantum with randomization disabled ([9134c0c6](../../commit/9134c0c6f489e7c3845104dceed8e00c3b33a8fd))
- Redesign the fee quantization chart to show which makers share an exact-fee anonymity set, with a legend, unit captions, and per-band median max size ([8a1afa30](../../commit/8a1afa30773317fe5ef2755e6816cc152550c949))
- Fee quantization chart tooltip now shows cumulative bond share and max coinjoin size for 10 makers, in sats ([939fd698](../../commit/939fd698ea7b50e34b219a4378ffc83dd89f2de8))
- Persist handed-out deposit addresses so /address/new never reissues one after a restart ([dc5ae6c9](../../commit/dc5ae6c95fed98f61ad3c0d5e078446bbac7f3f4))
- Let deposit addresses be set aside with a label, shown in jm-wallet info --extended ([dc5ae6c9](../../commit/dc5ae6c95fed98f61ad3c0d5e078446bbac7f3f4))
- Add 'jm-wallet address' commands to reserve, label, release, and list deposit addresses ([d5a736db](../../commit/d5a736dbdd4bc2ce4fcfdd46f7fde86e960730ce))
- Add incremental wallet transaction enumeration for descriptor and Neutrino backends ([9500de04](../../commit/9500de04f57276bb0e3a58062e5ba12a37e0169e))
- Notify websocket clients about deposits, CoinJoins, sends, and later confirmations for descriptor and Neutrino wallets ([e7cbccd3](../../commit/e7cbccd3084cd8d0abe5c75060ac4be8a296ba1f))
- Allow explicit direct access to a configured mempool API ([602be73d](../../commit/602be73df41f6caa7ca37dc5e5105c9db674bb70))

### Fixed

- All maker settings are now configurable from config.toml, including dual_offers, directory reconnection tuning, and orderbook rate limiter knobs. ([af8fd720](../../commit/af8fd720aafab45c4fcc4f5874cf2fae45b83c4c))
- Existing coins on reused addresses are no longer auto-frozen on the first sync after a wallet restart or unlock. ([84ab95b0](../../commit/84ab95b042db8d47b942f52deab91b41488ea57f))
- Accept a single-string notifications url and show a clear message for invalid config values instead of crashing at startup ([64767960](../../commit/6476796078f238c3a69817f4e0124eb53693041d))
- Verify the appended nick signature on relayed maker responses and attribute them to the signed sender instead of matching nicks by substring, preventing a malicious maker from hijacking another maker's encrypted session or forging error responses ([e0af9a15](../../commit/e0af9a15ba38da2ce2cd4f6faac5140fafd26662))
- Validate a peer-supplied relative cjfee before fixed-point formatting so a crafted offer can no longer expand to gigabytes and exhaust memory in every peer parsing the orderbook ([f2efe3c7](../../commit/f2efe3c7717c83a4fa109d5301355ebb68097fe2))
- Stop auto-freezing legitimate first-use coins discovered by a ([29595ef0](../../commit/29595ef0903202b06371d878b3ce3ec61bf8ca8b))
- Wait out Bitcoin Core's transient wallet-loading state on all wallet load paths instead of triggering a redundant rescan or failing ([45346eaa](../../commit/45346eaa852efd0a2e189fa017e386342d654395))
- Stop surfacing the expected neutrino 501 from /v1/tx as an error during wallet info ([8bb4a7ba](../../commit/8bb4a7ba4bf3d88e60702bf56ca256ac7dbaf739))
- Detect confirmation of neutrino pending coinjoins via chain lookups so they are no longer marked failed once they confirm ([006593a8](../../commit/006593a8ed6abce53ae2a551b6857f74ff2f1a7f))
- Fix fidelity bond UTXOs disappearing from wallet balance when the local bond registry has no matching entry but Bitcoin Core already tracks the bond address ([b7030c7d](../../commit/b7030c7d3fcb293636dc0b70dd3bb571c93f7867))
- Fix fidelity bond registry migration failing to claim legacy entries with a stale or inconsistent path/pubkey ([edb1fd52](../../commit/edb1fd526fa05208b1130d725491a7eabc62fb9c))
- Fix wallet-info reporting fidelity bonds as found when the bond-aware sync would not actually register them ([2379a9c1](../../commit/2379a9c17f441e7dce7eb7d39d785b00fbf33f16))
- Fix fidelity bond sync listing all 960 timenumber addresses and registering only one bond at the wrong value ([4877e743](../../commit/4877e74370c84c464947558e130f7565da74dd75))
- Fix fidelity bond value in list-bonds not refreshing after a plain wallet sync when the bond address has multiple UTXOs ([d2d1b1b2](../../commit/d2d1b1b28a50f60cfc983724f5f49f3f4b470ef0))
- Starting a wallet rescan while Bitcoin Core is already rescanning no longer fails with RPC error -4; the existing scan is tracked instead ([7537e7e7](../../commit/7537e7e7053199126fed4bbfdfd0548498600a73))
- getrescaninfo now reports real rescan progress from Bitcoin Core instead of always 0.0 ([7351e296](../../commit/7351e296cd72af9c295e279f12ecff4065869263))
- getrescaninfo and /session keep reporting rescanning=true while Bitcoin Core is still scanning ([7351e296](../../commit/7351e296cd72af9c295e279f12ecff4065869263))
- sign_bond_psbt.py now reports the installed HWI version and hints that HWI >= 3.1.0 is required to detect newer devices (Ledger Stax/Flex, Trezor Safe 3/5) when no hardware wallet is found. ([606841c0](../../commit/606841c044da93d77e74b39cb21a0228a370ff81))
- spend-bond guidance no longer claims current Ledger firmware can sign bond PSBTs via HWI; it explains the legacy-app caveat and points to the mnemonic and Specter DIY QR fallbacks. ([49f5b5a8](../../commit/49f5b5a8e87f0104a7b976714f34f3bc7fb47d97))
- Make the first wallet command after `jm-wallet generate` near-instant by recording the wallet creation height, and report scan progress during first-time wallet setup instead of appearing frozen ([0d341e59](../../commit/0d341e59d8b36dc949971809f55d7ae71ee8b649))
- Fix /address/new/{mixdepth} returning the same address on repeated calls ([bb4e8f1a](../../commit/bb4e8f1a188aad162fb5df84e412493bdcf434ed))
- Honor the documented orderbook_min_wait and orderbook_quiet_period config keys in the taker CLI instead of silently using defaults ([68483c64](../../commit/68483c644b87e580c2d016a7c76016cb20f2f08b))
- Honor orderbook_min_wait and orderbook_quiet_period for CoinJoins started through the jmwalletd API ([9ecda897](../../commit/9ecda897bebb985977ae3c85a4bc0f1393071d04))
- Honor the [tumbler] pacing settings (retry delay, confirmation poll interval, min confirmations) in the standalone tumbler CLI, not just jmwalletd ([6b001d03](../../commit/6b001d0375d3a3ef3d1f6e2d1dcd9ea3c0e5b09f))
- Honor all [taker] policy settings for tumbler phases and CoinJoins started through the jmwalletd API ([7e6a220b](../../commit/7e6a220bfbf0a9fd1f2a300207311d714597f95f))
- Prevent background work (commitment broadcasts, wallet resyncs, notifications) from being garbage-collected mid-flight and log its failures ([e2b0e370](../../commit/e2b0e3706f16968a7ed151a89941e9b85e564b92))
- Fix the session endpoint always reporting a null schedule while a tumbler is running, so JAM can render scheduler progress again ([8fb33ad6](../../commit/8fb33ad640d661bf255ee0906342ce4bab9aa433))
- Fix the fee quantization chart labeling the 10% relative quantum as 1% ([880d3e40](../../commit/880d3e40be6198aa2a0112a6fd827fe9bcfaf349))
- Fee quantization chart renders the grid even while the orderbook is still empty ([66d5b730](../../commit/66d5b73090c7a57816f415ba07e0c2574ea58430))
- Browsers no longer serve a stale web UI after the orderbook watcher is upgraded ([16d47fc3](../../commit/16d47fc3b69dfce83ebf3666ddfd8811643fbc21))
- Per-connection Tor/TCP dial logs moved from INFO to DEBUG ([bda6ddd3](../../commit/bda6ddd3d8e036ef088fdd3e8f44e2373617d73d))
- Reduce orderbook watcher log noise from bond deduplication and maker health checks ([faf643a4](../../commit/faf643a4108b80c489203981fc852ed5cb062369))
- Verify that a maker's signing pubkey owns the UTXO it signs before accepting the signature, so a maker can no longer pass verification with a key it does not control and make the taker assemble and broadcast a consensus-invalid coinjoin ([5591ccca](../../commit/5591ccca9d0a530279d4c7dc7157227faa52df9b))
- Require the maker's !ioauth btc_sig to verify and bind the auth pubkey to one of its declared UTXOs, dropping the maker otherwise, so a malicious directory can no longer substitute a maker's encryption key to MITM the session and a maker can no longer authenticate with a UTXO it does not control ([80d60717](../../commit/80d6071799229693f731e0aabbeb252046d295d3))
- Accept honest makers whose declared UTXO scriptPubKey hex is uppercase ([6a354843](../../commit/6a354843c4d4c7cd730cd35f04e4149cb275eefa))
- Rescans requested above the chain tip (e.g. JAM's default mainnet SegWit height on signet/regtest) are clamped to the tip instead of failing server-side after a 200 OK response ([c1384d3a](../../commit/c1384d3acc35e766c82fb156e15a1b22341c57f9))
- Stop peerlist-fetch log spam and abort immediately when the directory connection drops ([04477a3a](../../commit/04477a3ab2f9683e6d43b4a67bf9266e635dc033))
- Tumbler stop now terminates a stuck phase after a grace period instead of hanging ([f251697f](../../commit/f251697f4382bca547882717c7dea67af025ed58))
- Show a 'reused' privacy warning for deposit addresses paid to more than once ([56fbedf6](../../commit/56fbedf6e7a647567f63f59b1c703566b36131b6))
- Fix backup detection on Raspiblitz by using portable bash glob ([b9b9a221](../../commit/b9b9a221f0beabb61f49acddd3c30f02a361ba6d))
- Fixed exit behavior when pressing 'B' while already in JM-NG shell on RaspiBlitz ([c9054d99](../../commit/c9054d9954e07e25dfda23c63814e393030924c1))
- Abort in-flight peerlist requests immediately when their continuous listener disconnects ([5d98fa14](../../commit/5d98fa14ec98e8e8c58382c9769c3f56b39e4329))
- Keep tumbler shutdown bounded while recording cancellation only after the runner actually stops ([d6bc39e7](../../commit/d6bc39e7675f5d0f6b864f23ccd5a9acd8c7a81b))
- Make wallet discovery portable on BusyBox and include hidden wallet files without changing caller shell options ([75a6e9e7](../../commit/75a6e9e720ea7db30fdea7bdd03b451703f83795))
- Preserve forced-address-reuse protection and explicit metadata changes across restarts and concurrent wallet processes ([a7443611](../../commit/a7443611249940f4f9680ecf1203dd6d3bdcfcb3))
- Verify CI-first release signatures without requiring local manifests ([a0f2979f](../../commit/a0f2979f27fbce3a446f40a38731d9f9d96b3abe))
- Install the tumbler with complete maker and taker profiles ([648768d0](../../commit/648768d0c20fd46d285d044de82e2f0182a5b9e4))
- Prevent wallet notifications from being missed immediately after WebSocket connection ([28d3a84e](../../commit/28d3a84ef3b4c5d1b4aa956af1bd77443f913196))

## [0.33.0] - 2026-06-24

### Added

- TUI: Send dialog validates all inputs with retry loops. ([0cd043aa](../../commit/0cd043aa7b854399d30bbf2d842bb774d3756e0f))
- New Config Center consolidates all config operations. ([4820d95b](../../commit/4820d95b42d883193c5bf25c81c68499d49626f2))
- Auto-freeze coins sent to already-used addresses (forced address-reuse defense) ([03a5cef7](../../commit/03a5cef73533004f422e9b527844efaab8439d57))
- Auto-freeze forced address reuse only on already-spent (empty) addresses ([703f327c](../../commit/703f327c30591472cf8a2737dcb5c2ff7660be08))
- Add a wallet history API endpoint to jmwalletd ([ea80be1d](../../commit/ea80be1de4bc2035bf57102d9ade0e54c2bd13cf))
- Add address column to freeze TUI with FB truncation and ([ff721955](../../commit/ff721955cccd7c8409b2de6414da6baa47ce3075))
- Add a `--config-file` option (and honor `JOINMARKET_CONFIG_FILE` ([80b31f3d](../../commit/80b31f3dd786ddfd283c6177d7d758ceb106b84e))
- Extended wallet info now shows colored headers, better visual structure, and clearer balance breakdown. ([ef2b8240](../../commit/ef2b8240bc3e0d08f2c738bf285be959fc6069c0))
- Extended wallet info now shows individual UTXOs per address ([9866afa0](../../commit/9866afa0cb3055572b45fae8e0f32c4a35f4ae88))

### Fixed

- Rescans and fidelity-bond recovery now start from the wallet creation height instead of always scanning from genesis ([788e334b](../../commit/788e334ba72fc45e099f2c0aa9dc685ea22975e0))
- import-bond now explains that bonds funded in older blocks need a rescan to be detected ([bd3000d3](../../commit/bd3000d301ce0c9b7ed1d4101f968f9841a55a9b))
- Fix funded fidelity bonds being missing from wallet UTXOs ([4db9b1e3](../../commit/4db9b1e3506b6264a65e56710fd5ec16ffa8487c))
- Surface funded fidelity bonds in the jmwalletd UTXO API ([c76c1275](../../commit/c76c1275ae0a68ccf08295e4163b086a085d0917))
- Show funded fidelity bonds in jm-wallet info/sync-bonds instead of locked with 0 sats ([461fd66a](../../commit/461fd66add5673e624aa4a42020db9fd7d9141d5))
- Honor taker policy settings (minimum_makers, bondless fee policy) for CoinJoins started via jmwalletd ([f46905f7](../../commit/f46905f7e5f9489515133f2dbc184ed888ee25d0))
- Show maker earnings in the yield generator report (Earn) again ([a6f23c15](../../commit/a6f23c15cb5b6a2e8e4c9f1bf375a0fb4a6f6005))
- Fix fidelity bond freeze/unfreeze handling in the jm-wallet freeze manager ([6841a371](../../commit/6841a3716986e038d5b23d32e596370ad5e184ec))
- Validate taker UTXO eligibility before connecting so ineligible coins fail fast ([4c7d9af1](../../commit/4c7d9af1fc5d4635ca512f28a6e53c6039959767))
- Surface taker failure reason and used makers so the tumbler can retry intelligently ([ce4409c7](../../commit/ce4409c7eb04b7a52ba54a1a2a22a83076c6760b))
- Fix maker UTXO auto-freeze false positives after container restart ([9644d8a2](../../commit/9644d8a28943459201b6687e9b3e68f96253d1b2))
- Fix wrong hidden-address count for internal addresses in ([35aed040](../../commit/35aed040696ef1ac6953cfd727b1426932f807fb))
- Honor the maker `onion_host` config key so a configured static ([ae022c39](../../commit/ae022c39f1a272a8db09a68afda5cee005d51481))
- Expand `~` in the `data_dir` config key so JoinMarket-NG no ([abfe57ce](../../commit/abfe57cef957a848f9689427d272b22011089284))
- TUI: maker asks for the wallet password only once on Raspiblitz ([cc42290a](../../commit/cc42290aecbef3db3314f219c70e1d355af95a38))
- TUI: do not write the wallet password to config.toml when declined ([cc42290a](../../commit/cc42290aecbef3db3314f219c70e1d355af95a38))
- jm-wallet history now shows only the active wallet's CoinJoins (#523) ([08fd5ade](../../commit/08fd5adeff61d17b01eb7c9aa0318c3cc16ea910))
- Tumbler now resolves the internal next-mixdepth using the wallet's configured mixdepth_count instead of assuming 5, fixing stuck or misrouted tumbles on wallets with a mixdepth_count other than 5. ([75a4658b](../../commit/75a4658ba2be17556ccd4b3a664e5152a0a69a24))
- Show frozen fidelity bonds in the basic 'jm-wallet info' view again ([17b8625f](../../commit/17b8625f42c9abaec8c7eee7422c9912b109f979))
- Freeze manager now correctly handles fidelity bonds as separate ([a3a3d7e3](../../commit/a3a3d7e38af891d01100f1779b7e3f62ba4a5d90))
- Suppress ANSI colors in 'jm-wallet info --extended' when output is piped or redirected ([90e51786](../../commit/90e51786c1c86746ecf42d78b8ff45b4b7c5f204))
- Freeze manager no longer starts the cursor on a fidelity bond and reports skipped bonds accurately ([45f90582](../../commit/45f90582684bdb189efd724916f2277d7c5bd076))

## [0.32.0] - 2026-06-03

Neutrino backend now has mempool support. Along with other improvements and bug fixes.

### Added

- Takers now switch to a direct maker connection mid-session once ([a03e2ac5](../../commit/a03e2ac5aa35b0f403608e29a7d2faac6240e005))
- Show registered but unfunded fidelity bonds in list-bonds ([d193bb15](../../commit/d193bb1599b9743acaa4f1d803c42d7ac2abded7))
- CLI commands now print help when invoked without required arguments ([e5a23af5](../../commit/e5a23af590cdfe3fc787f80793d0c464b7b9ab5e))
- Add 'jm-wallet sync-bonds' to quickly refresh funded status of registered fidelity bonds ([ecf396e9](../../commit/ecf396e92d226babac158207df871f5334279fc3))
- Use the neutrino-api watched mempool tracker when available so neutrino takers verify maker broadcasts and follow the configured broadcast policy, with a neutrino_include_mempool opt-out for chain-only behaviour ([6b913cae](../../commit/6b913caec176a17683eb9b187ccf36d840198e2a))

### Fixed

- Exclude UTXOs locked by other in-flight CoinJoins from ([ee23530f](../../commit/ee23530f7582d27ef9b5fea05510eaa42f393233))
- Fix jm-wallet info mislabeling plain wallet sends as CoinJoin outputs/change ([6904da14](../../commit/6904da1404814b5f4bacf8a8fc725b40ea07e0fc))
- Fix fidelity bond registry copying other wallets' bonds into a wallet's own per-wallet registry file ([601ed69b](../../commit/601ed69bf0cfe1ac3def7afe88e54f6ac73484ba))
- Make offline list-bonds and registry-show use the configured wallet when no flags are given ([691e11ad](../../commit/691e11ada051757c702ea3421814dbbcad8512d7))
- list-bonds is now offline-only; use recover-bonds to scan the blockchain ([9ef23258](../../commit/9ef23258b9cb85420b2de9ef0317c62bf9e2f573))
- Fix history CSV corruption from a reordered column header that hid CoinJoin fingerprints and left confirmed rounds stuck pending ([93e8953a](../../commit/93e8953a91ace06726efec3e55bce557dcad2af5))
- Notifications now include the full txid and an optional block explorer link ([5032750f](../../commit/5032750fa5ccadb1f39bd49d6ee22f217c6378a2))
- Maker now sends notifications when a CoinJoin enters the mempool and when it confirms ([e4442fc8](../../commit/e4442fc875bb7e8cc51822a013928da2f39f5126))
- Secure the neutrino backend out-of-the-box with TLS trust-on-first-use pinning, default cert/token paths, automatic HTTPS, and a hard error on network mismatch ([fd3e4d8c](../../commit/fd3e4d8ccc35a03c53ef5e832c011ad2fd921971))

## [0.31.1] - 2026-05-30

### Fixed

- Install whiptail in maker and taker images so the jm-ng TUI works out of the box ([72ea101e](../../commit/72ea101ecf7f8665cbbd7e7f2124d61a7fb5138a))
- Fix jmwalletd crash on startup in Flatpak (ModuleNotFoundError: No module named 'tumbler') ([b4df30ff](../../commit/b4df30ffa5f12bc04e617da540c3f4a7f13c926a))
- Prevent a Sybil DoS where relayed !hp2 floods starved the maker's own post-ioauth commitment broadcasts and let the burned PoDLE commitment be reused against peers ([0db9050a](../../commit/0db9050a3617ad5b8cd78f740206da2b6a73d028))
- Clamp descriptor scan ranges to Bitcoin Core's 1,000,000 limit instead of failing the whole import with "Range is too large" ([236ed773](../../commit/236ed773cf10272d8185815da0ac1e0a1ffd6145))
- Honor --start-height when combined with --scan-depth in jm-wallet rescan ([236ed773](../../commit/236ed773cf10272d8185815da0ac1e0a1ffd6145))
- Stop aborting CoinJoin sessions when a taker switches between ([220e9c57](../../commit/220e9c579b16a86d529cc8ece8f07c34e4810585))

## [0.31.0] - 2026-05-29

More improvements about deep wallet scanning. Also security hardening, TUI improvements, and bug fixes. Automated Win/Mac/Linux install tests.

### Added

- Disable core dumps and ptrace in long-running daemons to keep wallet secrets out of crash dumps ([252e7146](../../commit/252e7146c4ec11a5fcdf5ee571c3498976378bff))
- Warn when a CoinJoin destination address script type does not match the wallet's native segwit (p2wpkh) script type ([87c57214](../../commit/87c572149bf50823321b3607999b0dc21f07c1af))
- Add DirectoryClientPool base class in jmcore to share directory connection-management code between maker and taker ([ef861d92](../../commit/ef861d927f44eb52839169f981cdcaafaa7356f7))
- TUI: Harmonize wallet selection and password storage across all operations ([d27800bd](../../commit/d27800bdc1ed177b21256804985b35c27871be71))
- Per-wallet fidelity bond registry; legacy fidelity_bonds.json is migrated automatically per wallet, cold-wallet bond commands now take --wallet-fingerprint, and 'jm-wallet info' prints the wallet fingerprint. ([5bd4eaa7](../../commit/5bd4eaa7d9ac716c9fd9ea3877be490f6befece6))
- Make container images runtime-user flexible by installing Python packages under /app ([3f85c061](../../commit/3f85c061d6fdffb7c5add79dbd53e55d513f073f))
- jm-wallet history/list-bonds/registry-show now accept a BIP39 passphrase and auto-pick the wallet when only one is present ([08e97c6a](../../commit/08e97c6a453b9f22f526acf91ea1673a3860a83c))
- Hash-check installer dependencies by default ([5a200a15](../../commit/5a200a1501c4e14c12931b9e045d2800da2da2dd))

### Fixed

- Verify GPG signatures of tagged releases in install.sh and pin the install to the verified commit hash ([8c36a98f](../../commit/8c36a98f56f9718a524a6addec7736cc2fce9ea4))
- Fix BIP39 NFKD normalization in mnemonic-to-seed derivation, aligning with the spec and the legacy joinmarket-clientserver ([6761ce07](../../commit/6761ce07402894f4eef6c069e0779f70a5d8e800))
- Fix SEND password handling and SEED abort behavior. ([c5ecf491](../../commit/c5ecf491f3e089e932558b1c90c061c6e412c4eb))
- Fix MNEMONIC_PASSWORD leak in SEND and SEED. ([362ed3a6](../../commit/362ed3a658d9090224c103308d28a1bf653262fc))
- Detect Bitcoin Core nodes started with '-disablewallet' and fail with a clear, actionable message instead of a raw 'Method not found' RPC error. ([436166db](../../commit/436166db40416f14f6649dc2b111c36f9599caf5))
- Fixed terminal flashes between menu operations and unified ([329d9ac3](../../commit/329d9ac398d1e15cfbd1b05b0ff2f8c7eaed97f4))
- Fix orderbook_watcher container crash-loop caused by a broken PYTHONPATH ([1d524b7b](../../commit/1d524b7b1974c27847b581f85f183ed11a9f3a92))
- Installer surfaces missing curl/gnupg/sudo with actionable ([80458c3e](../../commit/80458c3ec4232981483bd05615a4588c4961566c))
- Fix installer no-op when invoked via 'curl ... | bash' ([979245a5](../../commit/979245a5ec72c29c4a797928267a037a1692d77e))
- install.sh no longer fails when the GitHub API rate-limits the version lookup ([fb1d3a0c](../../commit/fb1d3a0ca5edaef1aa211e2f62970f216230c878))
- Replace libnacl with PyNaCl so install.sh works on Windows ([783a24fb](../../commit/783a24fb6ea3044bb2f247a7b501b1359f5ebce7))
- Fix inconsistent file existence handling in NEW wallet creation. ([e1f6a757](../../commit/e1f6a757b8cad5a630c0337fa639c52ddd8b2faa))
- Fix missing pause in Maker START/RESTART and Wallet NEW/IMP error handling, allowing users to read error messages before screen clear. ([ce68c6e6](../../commit/ce68c6e6c0f24bc29abae7b6ee6f641642247f06))
- Fix RESTART error path clearing terminal before user can read the error. ([27312fa0](../../commit/27312fa09ee21eeec1b2dba23a250bf3ecb6fd60))
- Fix ModuleNotFoundError 'nacl' after updating by resolving and installing changed dependencies (PyNaCl) during install.sh --update ([a49ef88a](../../commit/a49ef88a0c9b37033c082f58a1e9e663ca44a23c))
- Wire [wallet].gap_limit into descriptor range auto-expansion (was hardcoded 100) ([557ed4ea](../../commit/557ed4eaa92afb40438e372e3a832867d836cfc8))
- Replace `jm-wallet info --scan-depth` with `jm-wallet rescan --scan-depth N` ([557ed4ea](../../commit/557ed4eaa92afb40438e372e3a832867d836cfc8))

## [0.30.0] - 2026-05-20

Major improvements for wallet performance, specially for exiting old wallets with lots of transactions. Also security hardening, TUI improvements, and bug fixes.

### Added

- Add JSON-RPC batch helper to DescriptorWalletBackend to enable single-round-trip address lookups. ([7dc89c31](../../commit/7dc89c312e52cdf7d3c4a22ee7bf5ac38b42a1ef))
- New wallet files are now encrypted with Argon2id (OWASP 2024 baseline parameters) instead of PBKDF2-HMAC-SHA256. Existing PBKDF2 wallet files continue to load unchanged. ([5e6641de](../../commit/5e6641de3e3aafa5681b77db27ae39e5c3348810))
- Cap per-tx fee rate at 1000 sat/vB by default (configurable via wallet.max_fee_rate_sat_vb) to prevent silent fee blowups from misconfigured estimators or oversized manual overrides. ([041de9c0](../../commit/041de9c072c1cf3c7d48e8886f75476a82363d14))
- Auto-expand descriptor scan_range on wallet setup when used addresses sit near the lookahead boundary ([76cbece3](../../commit/76cbece31dfc4a3f53f0010b692debe1ad730dad))
- Replace the auto-expand descriptor scan_range probe and the --rescan-deep flag with a single 'jmwallet info --scan-depth N' one-shot recovery path that re-imports descriptors and rescans from genesis. The probe was unreliable for the migrated-wallet case it was meant to solve. ([5984ef60](../../commit/5984ef604640e61e7642a4ba51a7b2b435f4249a))
- Added Raspiblitz exit handler with B menu option and dynamic ESC behavior ([8308f4f1](../../commit/8308f4f16493cbd391b9d6d2e787f9ae513ff953))
- TUI: Add configurable log level via [tui] log_level in config.toml ([6780bc3f](../../commit/6780bc3f36d1e09817d6729b22562677c91fd96a))
- Add jm-wallet info --scan-status and jm-wallet rescan to ([c340b993](../../commit/c340b99383af68de9070752150cdaa6367bc36bb))
- Orderbook watcher web UI now signals which makers are ([461ef3a9](../../commit/461ef3a97bc3cc2889640b054660e1418cb332e8))
- Redesign the jm-wallet send confirmation summary for clearer field ordering and labels ([21effc40](../../commit/21effc40eca649b93045a4c9204ab860a555694e))

### Fixed

- Speed up descriptor-wallet sync by replacing listaddressgroupings with listreceivedbyaddress, eliminating multi-minute hangs on wallets with many transactions or CoinJoin co-spends ([11b118bd](../../commit/11b118bd1b2412e25fe248f2d993aa883af35f1b))
- Validate destination address checksum and network in jmwallet direct_send (fixes regression of #196) ([eb970809](../../commit/eb970809bcddc7840a80dac470cab9d06e25a6fd))
- Require authentication on GET /api/v1/wallet/yieldgen/report so the maker earnings report is no longer readable without a bearer token. ([e4a1a5e7](../../commit/e4a1a5e71ffa5bd2dc0613d31a9433d499dca7d3))
- Stop including fidelity bond proof in public reconnect announcements; bonds are now only sent via privmsg in response to !orderbook, matching the reference protocol. ([519ffed2](../../commit/519ffed2c99ee87d8e3c9120899fd521b323c150))
- Fix maker incorrectly rejecting offers with 'Insufficient balance' when the dual-offer fee intersection falls in the dust band; the dominated offer is now suppressed cleanly with a clear log message ([add613fd](../../commit/add613fd9005d24c29aea2468399781bbd2f35bd))
- Fixed history header duplication and minor TUI issues ([08230473](../../commit/0823047317455881d33e122cf68f1aa74b2be620))
- Fixed maker menu TUI feedback and wallet info navigation ([be50f914](../../commit/be50f91478bd7ac5804a80c2643e1370b8f409cb))
- TUI: Capitalize "Fidelity Bonds" consistently across maker bond menus, prompts, and help text ([c35df0db](../../commit/c35df0dbe7339f680c4c6d93b22ba7ba08699559))
- TUI: Show full testnet/signet/regtest fidelity bond addresses instead of dropping the network prefix ([9ca785e7](../../commit/9ca785e7f33cf2941c5e405ea78ce720f2b84ab6))
- Restore bonds create improvements and add multi-network regex ([94748205](../../commit/94748205c99176e4fb4ceee0a68d8f3a32461e20))
- Fix fidelity-bond forgery where verify_bonds() did not bind the UTXO to the bond scriptPubKey ([bdbac766](../../commit/bdbac7663cbd9a1d41073c11ec24bbd0b75d76b1))
- Honor wallet.mnemonic_password from config when loading mnemonics via --mnemonic-file or MNEMONIC_FILE env (issue #498) ([67043636](../../commit/6704363602332badae2bf4c15c1ca6b98a4bbea4))
- Reject negative or out-of-range cjfee values in maker offers ([009e0394](../../commit/009e0394bbecfb0073a13fe5cb02b80edd50aaed))
- Allow disabling broadcast on jm-wallet send via --no-broadcast ([55a33edb](../../commit/55a33edbefa70c47d58f225eddd7c9fa75287a02))
- Reject malformed amounts, mixdepths and counterparty counts on the wallet daemon HTTP API ([9470db49](../../commit/9470db49c175740529b84e9a78e079e282279f6d))
- Tighten JWT scope verification to reject cross-wallet tokens ([f5371de6](../../commit/f5371de6913c16b2ad086f64661cf776efde09b0))
- Fix tumbler runner crashing as failed instead of cancelled when stopped during a phase retry back-off ([e6af7263](../../commit/e6af72631574925c401c26499730b3f666a57b32))
- Fix periodic summary notification reporting inflated earnings ([42d2eeb9](../../commit/42d2eeb97e639821151975002653bc4d96a668ff))
- jm-wallet send now persists its change address in ([b399a814](../../commit/b399a814989d55f38cd21072866c13cb371b7ce6))
- TUI: Offer password storage for encrypted wallets in maker ([36eb8df0](../../commit/36eb8df00ca78c16f31553a992285a118fa6fd64))
- Fix jam-ng and jmwalletd image builds broken by Debian trixie package version pins in the bookworm-based jam-builder stage ([77c2ea5d](../../commit/77c2ea5dcc8aeeeedf494ea015dfa0fedef25f16))
- Persist used addresses to prevent reissuing a previously funded deposit address after the UTXO is spent. ([24e3e2f4](../../commit/24e3e2f4ca293cb7efe1f62036f8046784632c5e))
- Discover all wallet-owned addresses (including change) via paginated listtransactions to prevent missing them in deposit-address selection. ([7d2cc00d](../../commit/7d2cc00dba20ce70ec32fe6c297235407408f7e7))
- Persist spent input addresses on transaction history rows to prevent deposit-address reuse across restarts ([f98ffc5b](../../commit/f98ffc5b8f64f296bd5456b3258e65a280843690))
- TUI: Check for duplicate wallet name before import prompts ([50d84ec1](../../commit/50d84ec1671aa385c26fadaae14ad2c4f7e1edc0))

## [0.29.0] - 2026-05-12

### Added

- Orderbook watcher web UI now computes the protocol-feature share over bonded makers only, making it sybil-resistant ([c3fa164d](../../commit/c3fa164dcc7521ffab259f0a556fbfc44d5b6ab5))
- Add --resume flag to jm-tumbler run to retry failed plans without losing completed progress ([898cd33d](../../commit/898cd33d1e27c1eb64844e66c14168a5bcf51670))
- Dual offers are now split at the randomized rel/abs fee intersection so each offer covers a non-overlapping size range without leaking the unrandomized fee configuration ([1c2a17a7](../../commit/1c2a17a7f2b5d73ae34252b136dc900df8e9a3aa))
- Show UTXO confirmation count (capped at 5+) per address in jm-wallet info --extended output ([b152d246](../../commit/b152d24647ca0425e9cdb96020274a1e0514d93c))
- Default maker min_confirmations to 0 so makers offer unconfirmed UTXOs ([bf5d7ed7](../../commit/bf5d7ed78e67fde42320ab8c245af25a2a0a365b))

### Fixed

- Fix verify-release.sh --reproduce so all release images verify against CI digests ([27584fac](../../commit/27584fac79292ed2b16b2c5f132ca45755227492))
- Fix periodic maker summary reporting zero successful coinjoins after upgrading from 0.27.x ([5a168435](../../commit/5a168435081a335137457f66cb412620a0920ac9))
- Stop polling already-confirmed pending CoinJoin transactions ([21be6c66](../../commit/21be6c66110fa17060b03e9499d0f4633ed0c3f3))
- Stop privacy-leaking self-broadcast fallback on Neutrino takers: broadcast !push to all session makers simultaneously when backend has no mempool access ([401bbacd](../../commit/401bbacdb641db4075a4f7c10be8e6c2185f0aaa))
- Fix cjfee_factor, size_factor, txfee_contribution_factor, and offer_reannounce_delay_max settings being silently ignored ([2bdbdade](../../commit/2bdbdade217e564559e2d7c75c9402a6dcc90227))
- Quieter maker logs: routine wallet rescans and healthy directory connection checks now log at DEBUG instead of INFO. ([e2347e4c](../../commit/e2347e4c83b9ec0a41c7f33235f833e78c9c635a))
- Fixed seed words being hidden by post_wallet_create dialogs during wallet creation. ([fc7fa17e](../../commit/fc7fa17ee37742bd2a8c00f39a88040743424d50))
- Fixed unnecessary password storage prompt for unencrypted wallets during wallet creation/import. ([e81505ea](../../commit/e81505ea0eb09acb3cd37ea1bdb8769227a62d3f))
- Fix TypeError when counterparty_count is not set in config (randomised mode) ([05fe7b5d](../../commit/05fe7b5db5e6bf320c0a0b9693d05b56521f6b76))
- Ignored/soft-excluded makers no longer block a CoinJoin when the eligible pool is too small ([ef7dc1c2](../../commit/ef7dc1c2cab86a02bbf0ffdd2c831dfc3534a3bc))
- Count silent-timeout makers as blacklisted when any explicit blacklist rejection occurs in the same fill phase ([3a237977](../../commit/3a237977d81d19c9a8a5aaf966b141c6cdbe538b))
- Fix taker proceeding to transaction build after a maker's UTXO fails Neutrino spent-check ([d7666d37](../../commit/d7666d37180f12362587305d9492abcc4465ad84))
- Unconfirmed CoinJoin transactions are now marked as abandoned after pending_tx_abandon_hours (default 24 h) instead of being monitored indefinitely ([3acfcc71](../../commit/3acfcc714ed36614a33846ed5e65b6253c67f1be))
- Fix maker rejecting reference-implementation peers on signet due to testnet/signet network name mismatch ([cb05727f](../../commit/cb05727fcaa13b1383de9fc761af68ccd797df79))
- TUI coinjoin send now shows progress notes, explains automatic maker replacement, and displays a clear success or failure result ([12d8bcac](../../commit/12d8bcac3036c6129b9051ff7eddd9e9217140b5))
- CoinJoin confirmation prompts now have distinct headers for the maker-selection estimate and the final pre-broadcast confirmation, eliminating the confusing 'second identical prompt' ([a2b9dc77](../../commit/a2b9dc7799f9738a939a380af288faf83f813b64))
- Fixed self-CoinJoin where the taker selected its own running maker as counterparty when the maker was started after the taker (common in tumbler runs) ([e365a109](../../commit/e365a109eec588dc2d6e32fbb8523e79ad9d9278))
- Fix coinjoin_history.csv entries being written out of chronological order after confirmation or signing updates ([829eef67](../../commit/829eef67ec8068ba5083617220573df043d3384a))
- jmwalletd now writes and removes the maker nick state file so that jm-taker correctly excludes its own maker from CoinJoin selection ([096a9ace](../../commit/096a9ace5525dfc903b3c7c0c4dc5ba0e97521ed))
- Fix spurious signature-verification warning caused by the same ([5187186d](../../commit/5187186d4b74b56fb1f4fce8c595fdc53ed51404))
- fix double-enter after password failure; standardize CLI output ([a69ea862](../../commit/a69ea862e716ba8318b9deb823bbeedb0088c313))
- Fix installer continuing silently after apt install failure, which caused a broken setup with missing activate.sh ([30914180](../../commit/309141806169167e88fb5f52cf73b473b6d13719))
- Avoid "new range must include current range" failures when re-importing descriptors after a previous partial-failure import. ([47866e47](../../commit/47866e4723a0058c960aa52a00598c62ce5368fb))
- Render 'jm-wallet history' table chronologically with the most recent entry at the bottom ([508ced81](../../commit/508ced81b385a722a8d0643697aac7c953f61cec))
- Honour taker_utxo_age, taker_utxo_retries, and taker_utxo_amtpercent from config.toml ([bcf5dce9](../../commit/bcf5dce90ac80c3081b6633888a4e0f62ec9b6ac))
- Auto-initialize descriptor wallet on first sync ([e6aa6b89](../../commit/e6aa6b8962285ab446f82fcd2efb10b413fdb82e))
- Fix spurious 30s RPC timeouts on busy Bitcoin Core nodes that caused descriptor re-imports to fail with "new range must include current range" ([57f7a0f1](../../commit/57f7a0f15a9cb2a29fac000b7b64b4b6c0c89372))
- Avoid multi-second descriptor wallet sync delays when listaddressgroupings returns addresses imported as non-ranged descriptors (e.g., addr() imports for fidelity bonds), unblocking MakerBot startup. ([c71977ad](../../commit/c71977ad8bedf4b97100d41dbd835faa34764855))
- Fix MakerBot startup hang caused by BIP32 derivation scan when Bitcoin Core's getaddressinfo returns ismine without a descriptor ([6dd2ba09](../../commit/6dd2ba09cd28ce19599283dd1c61c43f11e3306f))

## [0.28.1] - 2026-05-01

### Fixed

- Fix orderbook watcher reporting stale offers from disconnected makers ([57d41a35](../../commit/57d41a35d391002d415c0d1cc9f1d702dcbc6fbf))
- Fix local release builds to match CI digests for reproducibility verification ([3d95088b](../../commit/3d95088bcf4ca1e15b8f33a80030fd578342089b))

## [0.28.0] - 2026-04-30

### Added

- Add new mainnet directory server nok55gjlqw6h76zi6gigukoztpx7xgo5r3w5csu362nys5yukzrxpgad.onion ([c595bf92](../../commit/c595bf92858bf7e5fec251cde5e28b48c0d6b686))
- Show git commit hash in debug-info for easier troubleshooting ([33f622d7](../../commit/33f622d7e7a2ffb8b6d467d80bf9dd009eab6c90))
- Add in-TUI update option with stable, dev, and version channels ([e36baa5d](../../commit/e36baa5dfff668c44fe6d505e39ccea415632bfb))
- Enable per-slot probabilistic bondless maker selection with 20% default allowance ([8f55b902](../../commit/8f55b902c58df654336c8884faa1be667b6f6bf8))
- Add jm-wallet verify-password subcommand for scripted ([92c36f06](../../commit/92c36f06eb1a82a4e2e65ac939e985d35b5b6fb1))
- TUI now lets you choose 12- or 24-word seeds when creating a new wallet ([f5363e0a](../../commit/f5363e0ad0747a9a58d62a647d0fd1ac94415299))
- jm-wallet info --extended now hides zero-balance addresses by default; use --show-empty to restore the old output ([ad1e9199](../../commit/ad1e9199f794c580819d9a644eafc16ed6d4e4aa))
- Per-wallet CoinJoin history isolation; jm-wallet history gains --mnemonic-file and --all-wallets ([bc416fcb](../../commit/bc416fcb339eb8643b17a343f9762b077caa246b))
- New jm-wallet showseed command to display BIP39 seed words from an encrypted mnemonic file ([943b27b7](../../commit/943b27b7047c4c193cb596d6ea1f1dcae13cf41a))
- TUI update menu now shows the running commit on non-editable installs (pip git+, Docker, release wheels) ([42d38d3e](../../commit/42d38d3e36cf85236e2eff3b122d2a43546c4679))
- Randomize CoinJoin counterparty count in [8, 10] per request when not configured, matching the upstream JoinMarket reference and avoiding fingerprinting via a fixed value. ([330202b9](../../commit/330202b9b3e1d06932a2594f891fd004f32497b6))
- Bump default minimum_makers from 1 to 4 to match the upstream JoinMarket POLICY default and avoid fingerprinting. ([4fd34ff1](../../commit/4fd34ff17d2fea7d085a916de554f49ee510431b))
- Align maker default cj_fee_relative (0.00002) and min_size (100000) with the upstream JoinMarket reference to avoid making jm-ng makers fingerprintable on the orderbook. ([23fa076a](../../commit/23fa076a5bbd37849adc6aa7ed727107db087dd9))
- Randomize maker-advertised cjfee, txfee contribution, and minsize per offer announcement (defaults match upstream yg-privacyenhanced) so jm-ng makers do not stand out on the orderbook. ([4779f54b](../../commit/4779f54b7c3a2a6c5b340b4e3e4f29ffdf00e70f))
- Add jm-tumbler package introducing a role-mixing tumbler with a human-readable YAML schedule state file ([4716fe2d](../../commit/4716fe2d822bb6ff64618821141637bd2c85fb99))
- Added tumbler endpoints driven by jm_tumbler with persistent ([66e59e53](../../commit/66e59e531afc098720b6105c575d5335728d2e03))
- Added ([a08e9f13](../../commit/a08e9f13cff2706538a3e4ea30f294d84975222a))
- Pin stable OpenAPI operationIds across the wallet daemon ([5ea878c0](../../commit/5ea878c00c289e5d8015ab8e53e7bb862aeb79f3))
- Added standalone jm-tumbler CLI for building and running tumbler schedules outside jmwalletd. ([073ba977](../../commit/073ba977028d3475b5b4ea74a9ec4f426227fc80))
- Tumbler maker phases can now exit via an idle-timeout fallback when no CoinJoin is served in time. ([7c85d7da](../../commit/7c85d7da3d40c3f42a77a2f8ad862caea8a923e3))
- Tumbler plan requests can now set a maker-session idle timeout that exits the maker phase when the wallet is never selected as a counterparty. ([3b95dc66](../../commit/3b95dc66a77fa29deaf4a604b41cdad9683f3808))
- jmwalletd now serves TLS by default in the docker-compose ([9de0d758](../../commit/9de0d758c2750088b9630e43817a308416406972))
- jm-tumbler plan now defaults maker_count_min/max to ([fb2a09e6](../../commit/fb2a09e6b37f15312bc582d3260d8e19f0343bdc))
- Add /api/v1/logs for recent jmwalletd output ([6ace5185](../../commit/6ace5185f039bb05febbd7852ae6c11fff46faac))
- Record peer handshake features on OnionPeer so taker-side ([b1c5afdc](../../commit/b1c5afdce515f55b81131824d71cd7d171cd0ad0))
- Filter out makers whose handshake explicitly lacks ([a9e75455](../../commit/a9e754559802764b1988058dbd248405eb326dca))
- ``POST /tumbler/plan`` now accepts legacy JAM parameter names. ([770ea0f6](../../commit/770ea0f6ae3ce2986198a2f5527d42d090b3bc16))
- Drop the bondless-taker burst phase from the tumbler ([4e514a7b](../../commit/4e514a7b3255d64bac25867fa37a43abc68f31bb))
- Tumbler retries failed coinjoin phases with progressively ([8bb3fe33](../../commit/8bb3fe3346b9b8bd27eb36cc66a1545fd566cda8))
- Tumbler CLI now requires at least three destinations by ([73b0cfd1](../../commit/73b0cfd1bfe59a09556bc6403edbe5bfbd9d138b))
- Tumbler maker sessions now run as 0-sat absolute-fee offers with no fidelity bond ([82cc92c0](../../commit/82cc92c01a9e1292a05d2962a8470934ce97bae0))
- Show fee and duration estimates plus active taker config ([3c0156eb](../../commit/3c0156ebe13fa14e445ebf5933ba3f3b531c57dc))
- jm-tumbler plan now prints wallet balance, fee percentages, and live fee-rate estimates ([9ce44bb1](../../commit/9ce44bb13b078734ebbfa4c4a5c53bd6c0e3efab))
- Tumbler now avoids re-using the previous phase's makers in the next coinjoin to improve cross-phase unlinkability ([f0889213](../../commit/f08892134f264a04619f9edbdbe2d244fa7d9fd4))
- Tumbler now obfuscates non-sweep CoinJoin amounts via ([73c83594](../../commit/73c835945b379b90b7466914fcf50dcbe4f7757b))
- Tumbler runner pacing (retry delay, confirmation poll ([221ea5f3](../../commit/221ea5f37c7eae19e79ce6f8da4e22f74be604c8))

### Fixed

- Fix directory server Docker healthcheck using wrong CLI command ([9ffead35](../../commit/9ffead35c90afa6f1a78a26116dc3e8406f6a56b))
- Check for duplicate wallet name before generating seed phrase ([e964898e](../../commit/e964898e242c05d021b348d44e6cf3b69a1d07df))
- Clear stored password when switching active wallet to prevent decryption errors ([edc2cc45](../../commit/edc2cc4529e11efe715d75721db383c7ad919e21))
- Improve import wallet UX with empty default name, go-back support, and no auto-advance ([7cc7829d](../../commit/7cc7829d3d29fef107c97de59ead36171e1949f2))
- Support pasting full seed phrase with space, comma, or semicolon separators ([fa6c0081](../../commit/fa6c0081e8805f5320bcd78b736ea5c1870cfcf6))
- Allow blank encryption password during wallet creation with a plaintext warning ([8ff0ca08](../../commit/8ff0ca088f9a463eaba2e3dd0a15f80bcbee7047))
- Fix heartbeat orderbook probes for legacy JoinMarket makers ([eb812d4c](../../commit/eb812d4c3cd23f0fceac9fb83bebb72734c3cc76))
- Show wallet filename in the mnemonic password prompt ([d8fd8afc](../../commit/d8fd8afc93f0a2e16aa99131402acc9ff4f0eea1))
- Validate wallet password before storing it in config.toml ([1db9131c](../../commit/1db9131cfd1344bcbb05448c612324e8f6264493))
- Keep mnemonic_file and mnemonic_password in sync after ([1db9131c](../../commit/1db9131cfd1344bcbb05448c612324e8f6264493))
- Offer wallet selection before starting the maker bot when ([bfb80897](../../commit/bfb80897ef90c77fb4e14b4a32ce36027163c8a1))
- Retry wallet mnemonic password prompt on wrong input in the CLI/TUI ([10d036ff](../../commit/10d036ff7394451a2420bd68b6b866acfa6dac55))
- TUI now offers to store the password after selecting a different active wallet ([dd1625be](../../commit/dd1625bea29cdc6f72dc049a981ce75b21195cc8))
- TUI update menu shows current and target versions, detects already-installed versions, and returns to the update submenu on cancel. ([24213128](../../commit/24213128b5245d534e47e73b72f8750955ea2441))
- Fix spurious PEERLIST timeout warnings when background peerlist refreshes raced with the directory client's receive loop ([d51f7d64](../../commit/d51f7d646ed5c6d9b9e36c94ab41a2e1f98331e2))
- Display CoinJoin change outputs as 'cj-change' instead of the misleading 'non-cj-change' in wallet info and the walletd display API ([05227245](../../commit/0522724554fd3cef2aa5e67fc363d967f9373316))
- Fix TUI update menu showing 'vunknown' on standalone pip installs ([cb6497dd](../../commit/cb6497dd6017c8d396dc3e18eac3f1dd0b723c3f))
- Reduce blackout between main menu and update menu in the TUI ([7c2f71bb](../../commit/7c2f71bb1b9ac301bd8cab8bfbd42ac3f9d72928))
- Fix TUI update confirmation dialog showing literal '\\t' characters ([038416eb](../../commit/038416eb500d4f8558dc8bf0f36144cf03f84979))
- Prompt wallet password via whiptail instead of dropping to CLI prompt ([f66246c9](../../commit/f66246c9d743b548f0247736e05773d2956d0fe0))
- Do not report 'Update complete' when the TUI update step actually failed ([0d371390](../../commit/0d371390ec87365a1cadb15561be27629947921e))
- respect taker.fee_rate from config.toml and fix priorities so ([7d0464cf](../../commit/7d0464cf044bb7edd919b1b498ac4ef7c4f5f42f))
- TUI Wallet Management now advertises 12- or 24-word wallet creation support ([e5ad6171](../../commit/e5ad6171bcbc57317561d785a673401bb3986107))
- Returning from the Fidelity Bonds submenu now stays in Maker Bot Control ([12778194](../../commit/12778194a9f710dcc72e0466e16c8eb51b3a23bd))
- Maker Bot Control now refreshes its displayed service status after each action ([9870bef2](../../commit/9870bef2d81c0c366960a28be2b3c732036f6e96))
- Restore JoinMarket-style 0.2 taker fee randomization default and clarify tx_fee_factor documentation ([75f819fa](../../commit/75f819fa12ed91de10a030a5dca20f30537105c1))
- Quiet TUI wallet logs, clear stale wallet config, reuse captured wallet passwords, and keep wallet info focused on fresh receive addresses ([3c8a47e8](../../commit/3c8a47e8ad79d0c10443a2a561d8ed36afc87f34))
- Retry transient Bitcoin Core wallet-loading conflicts instead of failing immediately ([2a0b2a85](../../commit/2a0b2a859577d85e01ace651f8539a5c46bd0bbf))
- Show fidelity bonds list in a clean TUI message box ([3fd47beb](../../commit/3fd47bebbba534479e124dccf9fbecbe26d5ebf7))
- Maker auto-detects the standard Tor cookie file when no path is configured ([4e7d4ce0](../../commit/4e7d4ce068dccc224a4826521bf2829686096709))
- Stop crashing with httpx.ReadTimeout on first wallet command when Bitcoin Core's importdescriptors rescan exceeds two minutes. ([ca8c9e5c](../../commit/ca8c9e5c2ac850fe6706d133292b9b0b7a40f81c))
- Update menu correctly identifies development builds and lets dev users switch back to the stable release without a misleading 'already up to date' prompt ([7fed650b](../../commit/7fed650bec220f5c08725ab2cf7c186834684358))
- Make CLI data-dir overrides apply consistently to wallets, config, and neutrino TLS/auth paths ([92ed0b2e](../../commit/92ed0b2ec7a291d05a7241672c8f0d134f9667e2))
- Label pending CoinJoin outputs as cj-out/cj-change instead of deposit/non-cj-change ([8bc69335](../../commit/8bc69335efc1b69a118d71579a4b8ce3e53a87a0))
- Prevent directory-client leak when a coinjoin or tumbler task finishes ([43524a72](../../commit/43524a7273425e979702ff16275c3e30834f24ab))
- fixed ([935469af](../../commit/935469af5f08d33bbbb2f6128660379789a5c6a2))
- Fixed ([8fa5af6d](../../commit/8fa5af6d5b56d19ef1391c12250a10c8c0680943))
- Raise docker-compose directory healthcheck timeout to 15s to ([891295e5](../../commit/891295e5bc419c61732eccdf4ba05cc3c8676f41))
- Taker now logs the randomized fee rate at INFO. ([1d4f5d00](../../commit/1d4f5d00293c12b5e8506811dac1a74927ef36d0))
- Taker now handles blacklisted PoDLE commitments more robustly ([29347a3c](../../commit/29347a3cfbf73130a8559cdba68e12f6731b470c))
- Treat clean EOFs on incoming direct connections as normal ([248e6a09](../../commit/248e6a097ac9ab000a1d8f1a5e5d2ee3d1ff85d6))
- Service-state 401 responses are no longer advertised as ``invalid_token``. ([60e7408f](../../commit/60e7408f026d9677488c50cd0c28045a11014f81))
- Default tumbler inter-phase wait raised to 60 minutes; stage-1 multiplier now configurable (default 3x) ([46c59a3b](../../commit/46c59a3b60418f1ff9a25aa28707bb9540ceb006))
- Improve tumbler retries for low-confirmation and no-eligible-UTXO failures ([58201903](../../commit/58201903cf32ff6c39965106bd55bbaceef2e40b))
- Increase default tumbler waits to match the reference timing profile ([2e1cdb12](../../commit/2e1cdb1255e7d2291dec72ac6150b64bf086b067))
- Tumbler runner no longer stalls forever between phases when the wallet backend cannot resolve the broadcast txid by id ([0c5d0555](../../commit/0c5d0555c091d82f23b2dbecd6bfc7f73a5b14d0))
- Tumbler now resolves inter-phase confirmations via watched addresses, fixing stalls on neutrino / BIP158 light-client backends ([31532b3f](../../commit/31532b3fd2f374ef0ad559c59e33333a033f840f))
- Tumbler now logs the inter-phase delay duration, ETA, and periodic progress so operators can see the runner is sleeping on schedule rather than stuck ([9803a8f7](../../commit/9803a8f79db0615de5686a24a19e15fac361e60d))
- Make tumbler commands respect the configured JoinMarket data directory ([c02ed030](../../commit/c02ed030d1386387fe256d2847a45b644ff26435))
- Reduce premature tumbler retries between phases ([fdf1d698](../../commit/fdf1d6988ac97139ac8e9effe7907a73f331a152))
- Expose descriptor_wallet_name on /api/v1/session for clients ([7a13ac60](../../commit/7a13ac60f31989a272118b31e6dfa94cab97fdd8))
- Fix tumbler sweep failing with 'Not enough makers' when the ([ebd542d5](../../commit/ebd542d5568e608c70595b15ed8a2ee54ce8e9b8))
- Fix slow `jm-wallet info` on full-node wallets caused by extended-range scans for CoinJoin counterparty addresses ([ad7c5170](../../commit/ad7c5170aefdb6a2c9aa8b87cc4623b444d5968b))
- Deprecate the scantxoutset (BitcoinCoreBackend) full-node backend; use descriptor_wallet instead ([64ace9d8](../../commit/64ace9d8dc1ef8a2f6c6706baf2e70f248720e3d))

## [0.27.0] - 2026-04-17

### Added

- Neutrino takers now fail fast when not enough compatible makers exist in the orderbook ([a5b8e34f](../../commit/a5b8e34f513fc4b3f73681a25f13f9d78e436c17))
- Add automatic config.toml new settings diff during updates ([f8a5e65d](../../commit/f8a5e65d67058972f906045ce3fc59e7b97f9581), [ff6cf963](../../commit/ff6cf963f0c0d8f5ad7587cb9640014328552ff7))

### Fixed

- Installer script now checks for cmake and ca-certificates as ([64748a0a](../../commit/64748a0a817b2edc6b36ba604d5e566bc802d196))
- Prevent installer updates from using Neutrino TLS cert as the global CA trust store ([05258a02](../../commit/05258a025774e3645162eaa09bc33d6cb23c526e))
- Reassign PING message type from 797 to 798 per JMP-0004 ([a6753278](../../commit/a675327891442ae7bd83fbef4a3b73a584174e0f))
- Make Flatpak neutrino startup honor prefetch settings from config.toml for faster bond verification lookups ([1928c1fc](../../commit/1928c1fcbafe3266da4d3b1c35995700f16d3d27))
- Fix CoinJoin failure with very small maker fee rates causing "Fee rate must be decimal string or integer" error ([0f834535](../../commit/0f834535b57bb6b302d2c321d3fa27a769c531c4))
- Normalize scientific notation in taker max CoinJoin fee settings ([9264a4fa](../../commit/9264a4fa9b4d3dac7844765144fd3c28dc6f44f9))
- Abort CoinJoin if destination or change addresses cannot be successfully persisted to history ([479aab62](../../commit/479aab62f4ae1d6a723c49b5e27b9ac97d2972d7))
- Fix fidelity bond recovery by deriving bond addresses from timenumber locktime paths and add manual import support. ([2c0a9b63](../../commit/2c0a9b63a49957e3dcecd215b113cb513122128c))

## [0.26.1] - 2026-04-12

## [0.26.0] - 2026-04-11

### Added

- The TUI menu is now available as jm-ng console command after ([d03d3809](../../commit/d03d380956b8ac0df50a570cdd4d81c9b27746ae))
- Reject malformed PoDLE commitments at protocol boundary ([6041ef4a](../../commit/6041ef4a3503f3025c1982681c2ab53ac741f148))
- Make shell command autocomplete near-instant with static completion scripts and regeneration tooling ([6c507084](../../commit/6c507084698287e6a070e2f07338222189f16d2e))
- Add local-first release workflow for faster build and sign ([a1a0ab1c](../../commit/a1a0ab1cd608b5cb658808d7b66443bf981a736b))

### Fixed

- Fix commitment blacklist bypass when entries are stored with mixed case ([1b176873](../../commit/1b17687309a9b1487886e116e94745fc5a7e831a))
- Deduplicate UTXOs in summary disclosure count so repeated disclosures of the same UTXO are only counted once ([b7fd0a5a](../../commit/b7fd0a5a2efad4c3e753c588e67787e24608d891))
- Fix release reproduction by removing Dockerfile overlay that broke pinned base-image digests ([aeec3752](../../commit/aeec3752ea89466dc3d98b2a75e7c191ec9ee2ff))

## [0.25.0] - 2026-04-09

### Changed

- Neutrino TLS migration guidance added: if upgrading from HTTP, switch `neutrino_url` to `https://` and set `neutrino_tls_cert` plus `neutrino_auth_token_file` (or `neutrino_auth_token`). Manual install docs now include where to copy `tls.cert` and `auth_token` from neutrino-api.

### Added

- Add directory heartbeat probes and idle peer eviction using PING/PONG with legacy maker fallback ([2a33f7f7](../../commit/2a33f7f7c1f465359f9d4cb11b04ad05e83619cb))

### Fixed

- Propagate neutrino TLS/auth settings across all CLI backend codepaths and reduce duplicate pinning logs ([e5d5ca1a](../../commit/e5d5ca1aa2aa676c22b2fa4075278c5f06768c94))
- Fix TLS hostname mismatch when connecting to neutrino via Docker service names ([31740b71](../../commit/31740b71a5176ee6da32752680b47adbd073707c))

## [0.24.0] - 2026-04-08

### Added

- With the Neutrino backend, sync block headersover ([ec627ba4](../../commit/ec627ba4ad057b56bf3677ec93ac6962ab85c477))
- With the Neutrino backend, prefetch only last ~2 years of ([ec627ba4](../../commit/ec627ba4ad057b56bf3677ec93ac6962ab85c477))
- Add a .meta file companion to mnemonic files to store wallet ([0fcffac5](../../commit/0fcffac5fbf175ad6f476208e62c13e30b7a239a))
- Skip redundant neutrino rescans using persisted server-side coverage metadata. ([d51d9de4](../../commit/d51d9de4ae6dee7f514bcf1e10cc7d796efe3a25))
- Detect and log neutrino-api server capabilities on connect ([4e4a4414](../../commit/4e4a44149dd9c52c3ec6ae0264ba81e4b31fea3a))
- Improve jm-wallet debug-info with neutrino server version and watch diagnostics ([861bf3db](../../commit/861bf3db00151d1f3ea4eb072fdea069f808e6e0))
- Support TLS and token auth for neutrino-api communication ([9688432d](../../commit/9688432d2e750c06856e2be6e64ff870f56a364a))
- Add https://github.com/joinmarket-rs/joinmarket-rs (Rust ([b468a601](../../commit/b468a601ef6ea02506e81f7949bdfcddfbc8b99f))

### Fixed

- fix a bug where the orderbook command was being sent as ([cad9c138](../../commit/cad9c138c64faf08240939db6ff14cae9c040588))
- Harden UTXO parsing in jmcore and refactor PoDLE revelation validation to use a single strict implementation matching the reference ([37e2bdab](../../commit/37e2bdab226aef96de09d56adae09e2c93c8a0c6))
- Restrict CORS origins to local hosts for security ([1e794fba](../../commit/1e794fba8cb1e0137da86080325f18e81797ebe8))
- Fix IDOR where any walletname in the URL would return real wallet data ([6bfd8442](../../commit/6bfd8442663bc27945b0e4435435f36830efc063))
- Stabilize neutrino bond verification by resolving scan start height lazily ([61a13903](../../commit/61a13903c7f4c5b8c5a42c51132f1e903dd2ecf5))
- Make Flatpak orderbook watcher honor dynamic Tor SOCKS and control ports ([19a6539e](../../commit/19a6539eae31acfc9fe83e06b29627eafa8b9912))
- Enable authenticated neutrino bond verification in orderbook watcher ([e09c9c03](../../commit/e09c9c032ea154c83f00fd8b0433a1caaa71f5db))
- Add Flatpak --log-level option and improve GUI log readability ([8d2130a8](../../commit/8d2130a85c26fb0c09a8e06c03ec956b48f95c2b))
- Neutrino makers now advertise the `neutrino_compat` feature ([c7993660](../../commit/c7993660fa7fa909b1cb97805d6987c3e75ff42f))
- Fixed error message when a Neutrino taker encounters a maker ([c7993660](../../commit/c7993660fa7fa909b1cb97805d6987c3e75ff42f))
- Mandate SegWit serialization and non-zero counts to prevent structural lensience bugs ([867871e3](../../commit/867871e344ff48d484350f7364a5daf63c862fda))
- Harden transaction parsing against truncated and oversized varint-driven payloads ([37cd42e0](../../commit/37cd42e0378ab737a78306cba273fcfacf97bdea))
- Accept TRUC-style version 3 transactions in maker tx verification parser ([742a498d](../../commit/742a498dc313d646c97a09547184b06739e0be1f))
- Prevent resource leak of StreamWriter objects during server shutdown ([6bf22694](../../commit/6bf22694da476ab439c067dd3241086c714dc20a))
- Remove incorrect cold storage labels from fidelity bond details in the orderbook ([d17ab75f](../../commit/d17ab75f6644e664903368c307ed44e7730de462))
- Use ephemeral cert keypairs for hot wallet fidelity bonds to match reference implementation ([a826959f](../../commit/a826959fe0d066916806caa2b44570f96305b04a))

## [0.23.1] - 2026-04-04

### Fixed

- Fix memory leak in MakerBot by bounding rate-limited log timestamps ([66cd3d59](../../commit/66cd3d59e5af27a10eb33f555d73199aa491134d))
- Fix allow_mixdepth_zero_merge config not being read from settings file ([912a6c49](../../commit/912a6c4955db7d8c45f31e34b7f0bd78bebe9fc8))

## [0.23.0] - 2026-04-04

### Added

- Exempt CoinJoin outputs from mixdepth 0 merge restriction, improving maker max offer size ([8e0d4964](../../commit/8e0d496454b7eaf753352bf4f4c908b493f7316c))
- Add allow_mixdepth_zero_merge config flag to disable md0 UTXO restriction ([8e0d4964](../../commit/8e0d496454b7eaf753352bf4f4c908b493f7316c))
- Add optional balance/UTXO count to periodic summary notifications (opt-in via notify_summary_balance) ([bef401ef](../../commit/bef401ef0c2a216f80ef824f8ee67142a24e8545))
- Scan bonded makers first and low-fee makers before spam during feature discovery ([7ede8d45](../../commit/7ede8d454e968138b279a46ea6ef2b722e6c7ab6))

### Fixed

- Make wallet history rewrites atomic and avoid 0-conf promotion to confirmed status ([01c73dae](../../commit/01c73dae85580c8cf945b4d01626072326862dbd))
- Protect bond registry writes with atomic replacement and 0600 permissions ([bf0e855a](../../commit/bf0e855ad6dcab723a4aab08242381f8629a1c5b))
- Prevent partial descriptor import failures from being treated as fully imported ([746a3a98](../../commit/746a3a98575497ca62b3fc33fca2736cb52c48c1))
- Correct mempool backend confirmation calculation for UTXO queries ([a09e4c41](../../commit/a09e4c4160493082b55f77faae8e4b5a4a8a17fd))
- Deduplicate fidelity bond cache entries and preserve fidelity bond freeze safety in bulk actions ([8c0321f9](../../commit/8c0321f98aa0d9057c4046c4b6aa65840088cac8))
- Add send CLI safeguards for manual fee rate and change key derivation ([e80ba7c6](../../commit/e80ba7c68c00b0c2b8852acb899c432f0e8b0ca0))
- Harden cold-wallet key handling and add offline current-block certificate workflows ([ef2a550a](../../commit/ef2a550a17d939ce3ee9f342f6bcee6978295177))
- Keep fidelity bond UTXOs frozen when bulk-unfreezing regular coins ([83a39ba2](../../commit/83a39ba2f0057cc1bb2b6b728ebb90ad978bd8b6))
- Reduce wallet service memory retention and keep reserved address tracking bounded by durable history. ([243c31cb](../../commit/243c31cbafb79abdf9ce4f3db375cc4c53becdd2))
- Enforce strict BIP32 and segwit signing invariants to prevent silent invalid transaction signatures. ([28027d83](../../commit/28027d834ef5b31f7e280a8fa64be9991bddec5a))
- Improve backend safety checks and lifecycle handling for RPC privacy, descriptor scans, and background rescans. ([0582ae82](../../commit/0582ae82100ab767f2b5a37805903499b777d3e5))
- Keep descriptor wallet path and address caches consistent after cache clears. ([b8d1908b](../../commit/b8d1908b9fda4186cc368cb609cd9409ac949974))
- Restore jmwalletd seed endpoint and service startup mnemonic wiring without reintroducing mnemonic storage on WalletService. ([772bafbb](../../commit/772bafbb7ecf88f304b4bebcaed37caccf03f397))
- Reject transactions with output values exceeding the total possible Bitcoin supply (2.1 quadrillion sats) ([de79981d](../../commit/de79981dd92edeb959ae31a78fcc1606cd1e85db))
- Enforce MAX_MONEY output value validation in shared transaction parser ([d2229365](../../commit/d22293658aef9629845eaf5d7cf299b1d2bf8abe))

## [0.22.0] - 2026-03-29

### Fixed

- **Maker `listen_tasks` unbounded growth on repeated directory reconnections**: Every successful reconnection in `_periodic_directory_reconnect` appended a new `asyncio.Task` to `self.listen_tasks` without removing the old, completed task for that node. Over many reconnection cycles on an unstable network, `listen_tasks` accumulated dead task references indefinitely, causing memory pressure and degraded `asyncio.gather` performance. Fixed by adding a `_prune_done_tasks()` helper to `BackgroundTasksMixin` that filters out completed tasks, called before each reconnect listener is appended.
- **`jmwalletd` coinjoin router settings consistency**: `taker/coinjoin` and `taker/schedule` now populate `TakerConfig` with network, directory server, and Tor stream-isolation settings from `JoinMarketSettings`, matching maker behavior and other modules.
- **`/address/new/{mixdepth}` returned the same address repeatedly**: `WalletService.get_new_address()` now tracks issued receive addresses in-memory and treats them as used when selecting the next external index, so repeated calls return fresh addresses even before on-chain history exists. Added coverage in `jmwallet` and `jmwalletd` router tests.
- Retry full initial neutrino rescan when completion status cannot be confirmed (82235622)
- Harden neutrino rescan/address handling and default Flatpak jmwalletd transport to TLS (010a7507)
- Improve neutrino peer handling and initial rescan reliability across wallet and daemon flows (5faed9c9)
- Fixed fidelity bond omission in offer re-announcements after directory reconnection.

### Changed

- **Release verification: skip `jam-ng` layer reproducibility check**: Updated `scripts/verify-release.sh --reproduce` to exclude `jam-ng` from layer digest comparison while still building it, matching the existing behavior in `scripts/sign-release.sh`. The `jam-ng` frontend bundle (react-scripts/webpack) remains non-deterministic across environments, so this avoids false reproducibility failures without skipping the image build.

### Added

- Add menu.joinmarket-ng.sh - an interactive text-based menu for joinmarket-ng operations, designed for Raspiblitz users. This script provides a user-friendly interface to manage wallets, send bitcoin (including CoinJoin), control the maker bot, and view information, all without needing to remember CLI commands. (2462834f)
- Add Jam-NG Flatpak packaging with GTK control panel, multi-network support, and managed service startup (48ca761b)
- Add neutrino_connect_peers configuration support across CLI tools and Flatpak wiring (525146b5)

## [0.21.0] - 2026-03-15

### Changed
- **Removed default `mempool.space` public API dependency**: The `MempoolBackend` and `MempoolAPI` no longer default to the public `mempool.space` API. Users must now explicitly configure a `mempool_api_url` in `config.toml` or environment variables to use a self-hosted mempool instance for fidelity bond verification and wallet synchronization. Affected modules include `jmwallet`, `orderbook_watcher`, `cold_wallet.py`, and `fidelity_bond_tool.py`.
- **`MempoolBackend` in `jmwallet` is opt-in only**: `jmwallet/backends/mempool.py` (`MempoolBackend`) remains available as an explicit opt-in wallet backend but is never instantiated by default. Wallet operations default to a local Bitcoin node via `BitcoinCoreBackend`, `DescriptorWalletBackend`, or `NeutrinoBackend`. The `orderbook_watcher` retains its optional mempool API fallback for fidelity bond observation only.
- **`cold_wallet.py` block height uses configured node backend by default**: The `prepare-certificate-message` and `import-certificate` commands now use the same backend resolution as all other CLI commands (`--backend`, `--rpc-url`, `--neutrino-url`, `--network`, and `config.toml`). The `--mempool-api` flag is retained as an explicit opt-in fallback only when no node backend is configured; it is no longer the primary or required source. Removed the old direct `urllib`/`requests` calls against a bare `--mempool-api` URL that bypassed Tor.

### Fixed

- **UTXO selection: missing "unconfirmed" hint in mixdepth 0 error**: When all UTXOs in mixdepth 0 failed the minimum-confirmations check, the error read "Insufficient funds: no eligible UTXOs in mixdepth 0" with no indication that unconfirmed funds were present. The message now mirrors the non-md0 branch: it reports the unconfirmed balance, the confirmed balance, and the required confirmation count so the user knows exactly why selection failed.
- **Neutrino backend: `_wait_for_rescan` hangs on unexpected exceptions**: `_wait_for_rescan()` only exited early on `httpx.HTTPStatusError` with status 404; any other exception (e.g., a plain `Exception("endpoint not found")`) was caught, logged, and the polling loop continued until the 300-second timeout expired. Changed the handler to return immediately on any exception, logging a distinct message for HTTP 404 vs. other errors.
- **Maker tracking via offer reannouncements**: After a coinjoin, makers re-announce offers with an exact `maxsize` reflecting their new balance, allowing observers to correlate balance changes with on-chain transactions (especially when combined with fidelity bond identity). Two mitigations: (1) `maxsize` is now rounded down to the nearest power of 2 so that small balance changes produce no visible offer update and (2) a configurable random delay (`offer_reannounce_delay_max`, default 600s) is applied before re-announcing to break timing correlation with block confirmations.
- **De-anonymization risk from mixdepth 0 UTXO merges**: Fixed a privacy issue where using multiple mixdepth 0 UTXOs in a single coinjoin could link a maker's fidelity bond to their regular deposits or change. The wallet now strictly restricts `mixdepth=0` to a single UTXO across all coin selection methods (`select_utxos`, `select_utxos_with_merge`), preventing merges. The `get_balance_for_offers` method now returns the largest single UTXO value for md0 instead of the sum, ensuring makers don't advertise offers they cannot fill. The maker bot also warns users on startup with actionable advice if they have both a fidelity bond and regular deposits in mixdepth 0.
- **Taker: confusing "have 0" error when funds are unconfirmed**: When UTXO selection failed because all funds lacked enough confirmations, the error logged "need X, have 0" even though the wallet had just reported a positive balance. The error message now distinguishes confirmed vs. unconfirmed funds and states how many confirmations are required. A follow-up log line also names the `taker_utxo_age` config setting and suggests remediation.
- **Neutrino maker silently drops reference taker sessions**: When a reference (legacy) taker selected a neutrino maker for a CoinJoin, the maker's `handle_auth()` fell through to `get_utxo()` which always returns `None` on neutrino backends, causing the session to fail silently. The taker would then wait for a 60-second timeout before proceeding with other makers. Now the neutrino maker explicitly detects the incompatibility (legacy PoDLE format without extended UTXO metadata) and returns a `neutrino_incompatible` error immediately. An `!error` message is also sent back to the taker via the directory server, giving immediate feedback rather than a silent timeout.
- **Neutrino backend: slow initial rescan from block 0**: The neutrino backend's `get_utxos()` hardcoded `start_height: 0` in the rescan request, causing it to scan every block from genesis. On signet (~295K blocks) this took ~17 minutes. Now uses the `scan_start_height` config option (defaulting to SegWit activation height per network) so the scan skips irrelevant pre-SegWit blocks.
- **Neutrino backend: fidelity bond verification always fails**: The Go neutrino-api server tracked `foundHeight` internally in `GetUTXO()` but did not include it in the `UTXOSpendReport` JSON response. Python always got `block_height: 0`, calculated 0 confirmations, and marked all bonds as "UTXO unconfirmed". Added `BlockHeight` field to the Go struct and populated it in the response. Bond verification also now uses `scan_start_height` instead of scanning from block 0.
- **Neutrino backend: silent fallback to 1 sat/vB fee rate**: When using the neutrino backend without `--fee-rate`, the taker silently fell back to 1 sat/vB with just a warning log. This could result in stuck transactions during fee spikes. Now raises a hard `ValueError` requiring `--fee-rate` to be specified explicitly.
- **Neutrino backend: HTTP timeout too short**: Increased the HTTP client timeout from 60s to 300s for neutrino API calls, which is needed for longer-running rescan operations.
- **Neutrino backend: zero balance after initial rescan on signet**: `get_utxos()` used a hardcoded `asyncio.sleep(10.0)` after triggering a rescan, which was sufficient for regtest (~3K blocks, 5-10s) but far too short for signet (~295K blocks, ~60s). The wallet would query `/v1/utxos` while the rescan was still running and always return empty results. Replaced the fixed sleep with a polling loop against the new `GET /v1/rescan/status` endpoint (see neutrino-api changelog), which returns `{"in_progress": bool}`. Python now waits up to 300s for the rescan to complete before querying UTXOs.
- **Neutrino backend: change outputs missing after CoinJoin** (`jm-wallet info --extended`): `sync_all()` triggered the initial neutrino rescan as soon as the first `get_utxos()` call fired — which happened during the external (change=0) branch scan of the first mixdepth. At that point only the external addresses had been registered with the backend; the internal (change=1) addresses were added later in the loop. Because `_initial_rescan_done` was set to `True` after that first rescan, the change addresses were never covered. Fixed by pre-registering all wallet addresses (all mixdepths × both branches × gap_limit) with the backend before the per-mixdepth scan loop begins.
- **Neutrino backend: spurious "Descriptor scan failed" warning on every sync**: `sync_all()` used `hasattr(self.backend, "scan_descriptors")` to detect descriptor-scan capability. Because `scan_descriptors` is defined as a no-op on the `BlockchainBackend` base class, `hasattr` returned `True` for every backend — including `NeutrinoBackend` — causing `_sync_all_with_descriptors()` to be attempted and to always fail, logging a confusing `WARNING` on every `jm-wallet info` run. Replaced the `hasattr` check with a new `supports_descriptor_scan: bool` capability flag (default `False` on the base class, overridden to `True` in `BitcoinCoreBackend` and `DescriptorWalletBackend`).

### Added

- **Adaptive orderbook listening**: `fetch_orderbooks()` no longer waits a fixed duration for offer responses. Instead, it listens in 1-second chunks and exits early when no new offers have arrived for a configurable quiet period, but only after a minimum wait. The hard ceiling is still respected. On responsive networks (e.g., regtest without Tor), this reduces orderbook fetch time from the full wait to just a few seconds. Three new config fields control the behaviour: `order_wait_time` (max/hard ceiling, default 120s), `orderbook_min_wait` (minimum wait before early exit is allowed, default 30s), `orderbook_quiet_period` (seconds of silence that triggers early exit, default 15s). The `order_wait_time` config is also now properly forwarded from `MultiDirectoryClient` to `DirectoryClient.fetch_orderbooks()`.

## [0.20.0] - 2026-03-11

### Fixed

- **Hardware wallet signature rejection in `import-certificate`**: The cold storage fidelity bond `import-certificate` command rejected signatures from segwit hardware wallets (via Sparrow). Hardware wallets encode the address type in the signature header byte per the extended Electrum format (35-38 for P2SH-P2WPKH, 39-42 for P2WPKH), but the verification code only handled legacy P2PKH header bytes (27-34). Extended `_verify_recoverable_signature()` to accept all four Electrum ranges (27-42).
- **Docker image reproducibility broken by setuptools version drift**: Fixed reproducibility of Docker images (`directory-server`, `maker`, `taker`, `orderbook-watcher`, `jmwalletd`) which broke when setuptools 82.0.1 was released on PyPI. The root cause: pip's `--constraint` flag only applies to install dependencies, not to PEP 517 build isolation environments. When building our packages from source, pip would download the latest setuptools, which stamps its version into `WHEEL` metadata (`Generator: setuptools (x.y.z)`), producing different layer digests. Fixed using `--build-constraint` on the relevant `pip install` steps. The `verify-release.sh` script now overlays current Dockerfiles onto the release worktree so reproducibility fixes apply retroactively to past releases.
- **armv7 build failure in `jmwalletd` Docker image**: The previous fix applied `--build-constraint` globally (via `ENV PIP_CONSTRAINT`) which conflicted with `httptools==0.7.1`, which pins `requires = ["setuptools==80.9.0"]` exactly in its `pyproject.toml` and has no pre-built wheel for `linux/arm/v7`. This caused a `ResolutionImpossible` error on armv7 when building httptools from source. Fixed by applying `--build-constraint` selectively: only on pip install steps where source-build packages use setuptools as their build backend (our own packages and `stem`), not on steps that install packages with their own exact setuptools pins (i.e., `jmwalletd/requirements.txt` which contains `httptools`).
- **Cold storage fidelity bond documentation rewrite**: Major rewrite of the cold wallet setup guide in `docs/technical/privacy.md`. Key additions: prominent hardware wallet limitation warning with per-device compatibility table (Ledger/Jade can sign CLTV bonds; Trezor, Coldcard, BitBox02, KeepKey cannot); a "test the full flow" step before funding; HWI version requirements (>= 3.1.0 for newer hardware); QR code display section for air-gapped PSBT transfer; dedicated mnemonic/passphrase strategies for reducing exposure risk; migration guide from the reference implementation using the new helper scripts. Consolidated from `docs/README-jmwallet.md` with cross-reference.
- **`create-bond-address` auto-strips Sparrow `wpkh()` wrapper**: Sparrow's "Copy Public Key" wraps the hex key as `wpkh(03abcd...)`. The `create-bond-address` command now automatically strips this wrapper, so users can paste directly without manual editing.
- **`spend-bond` and `sign_bond_psbt.py` warnings updated**: Both the CLI output and the HWI signing script now show accurate per-device guidance: Ledger and Jade can sign CLTV bonds; other devices should use `sign_bond_mnemonic.py` instead.

### Added

- **`scripts/sign_bond_cert_reference.py` — certificate signing for reference implementation migration**: Self-contained script that signs a joinmarket-ng fidelity bond certificate using a BIP39 mnemonic. Derives the private key at `m/84'/0'/0'/2/<timenumber>` (the reference implementation's fidelity bond path) and signs in Electrum recoverable format, producing a base64 signature directly accepted by `import-certificate`. Only depends on `coincurve`. This is necessary because the reference implementation's `BTC_Timelocked_P2WSH` has a bug where `sign_message()` receives a `(privkey, locktime)` tuple instead of raw bytes, making `wallet-tool.py signmessage` unusable for bond paths.

- **`scripts/derive_bond_pubkey.py` — pubkey extraction for reference implementation migration**: Self-contained script that derives fidelity bond public keys from the reference JoinMarket implementation's xpub (shown by `wallet-tool.py display`). Accepts the account xpub (`fbonds-mpk-` line) or the `/2` branch xpub and a locktime (YYYY-MM), then outputs the compressed public key ready for `create-bond-address`. Only depends on `coincurve`. Includes `--info` mode for timenumber/path lookup without an xpub.

### Changed

- **Ephemeral-identity PoDLE commitment broadcast**: Commitment broadcasts (`!hp2`) are now sent from a fresh random nick on a separate Tor circuit, rather than from the maker's long-lived identity. After verifying a taker's PoDLE proof, the maker opens ephemeral connections to all directory servers using unique SOCKS5 credentials (forcing stream isolation) and a random nick identity, broadcasts the commitment, then tears down the connections. This prevents any party from correlating the `!hp2` broadcast with the maker that participated in the CoinJoin. The same ephemeral approach is used when relaying `!hp2` requests from other makers. Concurrent ephemeral broadcasts are capped at 2 via a semaphore to prevent Sybil DoS attacks.

## [0.19.3] - 2026-03-05

### Added

- **Recovery notification after all directory servers reconnect**: After the critical "All Directories Disconnected" alert, a follow-up "RESOLVED: Directory Servers Reconnected" notification is now sent as soon as at least one directory server reconnects. This uses the same `notify_all_disconnect` toggle (enabled by default) so operators are automatically informed when the issue is resolved.

## [0.19.2] - 2026-03-04

### Fixed

- **`orderbook_watcher` Docker container fails to start**: The `orderbook_watcher` Dockerfile was missing the `jmwallet` installation step. Since `orderbook_watcher/main.py` imports `BitcoinCoreBackend` and `NeutrinoBackend` from `jmwallet`, the container would crash with `ModuleNotFoundError: No module named 'jmwallet'`. Added `jmwallet` requirements and package installation to the Dockerfile and declared `jmwallet` as a dependency in `pyproject.toml`.

## [0.19.1] - 2026-03-04

### Fixed

- **`jam-ng` armv7 Docker build**: Fixed two issues that prevented `linux/arm/v7` builds from succeeding.
  - `node:24-slim` does not publish a `linux/arm/v7` image. The `jam-builder` stage is now pinned to `--platform=linux/amd64` — the output is static browser JS so the build platform is irrelevant.
  - s6-overlay was hardcoded to the `x86_64` tarball with a pinned checksum. The install step now selects the correct arch-specific tarball (`x86_64`, `aarch64`, or `armhf`) and verifies its checksum at build time using `TARGETARCH`/`TARGETVARIANT`.

## [0.19.0] - 2026-03-04

### Fixed

- **Maker PoDLE commitment failure due to unconfirmed UTXOs**: Fixed a bug where the maker bot would advertise liquidity based on unconfirmed UTXOs but fail to complete the coinjoin during `!auth` because unconfirmed UTXOs are excluded from the selection phase. The maker now correctly respects `min_confirmations` (default: 1) for all balance calculations used in offer creation and periodic updates, ensuring it only advertises spendable, confirmed liquidity.
- **Spurious mempool warning after broadcast**: The taker no longer immediately checks the mempool for a just-broadcast transaction (which always fails with "No such mempool transaction"). A 5-second initial delay is now applied before the first mempool lookup in `_update_pending_transaction_now`. The `get_transaction` failure log in both backends is downgraded from WARNING to DEBUG since a missing mempool entry right after broadcast is an expected transient condition.
- **Directory server ignoring `LOGGING__LEVEL` env var**: The standalone `directory_server/docker-compose.yml` and `maker/tests/integration/docker-compose.yml` used flat env var names (e.g., `LOG_LEVEL`, `NETWORK`) that are silently ignored by pydantic-settings. Replaced with the correct nested delimiter form (e.g., `LOGGING__LEVEL`, `NETWORK_CONFIG__NETWORK`) matching `config.toml.template`.
- **Removed `.env.example` files from `directory_server/` and `orderbook_watcher/`**: These files documented incorrect flat env var names. Configuration is now documented directly in `docker-compose.yml` using the config file syntax with `__` delimiter, consistent with pydantic-settings.
- **`TOR__COOKIE_PATH` env var not applied to maker**: `MakerConfig.tor_control` was using `default_factory=TorControlConfig` which constructs a blank config ignoring all env vars, so `cookie_path` was always `None` even when `TOR__COOKIE_PATH` was set. Changed `default_factory` to `create_tor_control_config_from_env` so the Tor control config is always populated from the environment on startup.
- **Maker Tor hidden service setup reliability**: The maker now reliably obtains an ephemeral `.onion` address when Tor is configured.
  - **`jm-tor` Docker healthcheck**: The previous healthcheck (`test -f .../hostname`) was logically equivalent to only checking the `hostname` file due to shell operator precedence — it never actually verified Tor had bootstrapped or that the control auth cookie was valid. The new healthcheck verifies both the `hostname` file exists **and** the `control_auth_cookie` is exactly 32 bytes (the length written by Tor only after full initialization).
  - **Cookie validation in `TorControlClient`**: `_authenticate_cookie()` now explicitly validates the cookie file is exactly 32 bytes before sending the `AUTHENTICATE` command. A 0-byte or partial file (written by Tor during startup) raises `TorAuthenticationError` with a clear message instead of sending an empty hex string that Tor rejects with the cryptic "Got authentication cookie with wrong length (0)" message.
  - **Retry logic in `MakerBot`**: `_setup_tor_hidden_service()` now retries up to 5 times (3s delay) on `TorAuthenticationError`, covering any residual race between the Docker healthcheck passing and the maker process reading the cookie. All errors (auth and non-auth) fall back gracefully to `NOT-SERVING-ONION` with an informative warning rather than crashing.

### Added

- **Signet support in send command**: Sending to signet addresses (`tb1…`) now works correctly. Custom address decoding code replaced with `python-bitcointx` (`CCoinAddress`), which handles all address types and networks without manual script construction.
- **Trustless fidelity bond verification across all blockchain backends**: Replaced mempool.space-dependent bond verification with a unified `BlockchainBackend.verify_bonds()` interface implemented for all backends.
  - **Bitcoin Core backend**: Uses JSON-RPC batching (`_rpc_batch`) to verify all bonds in ~3 HTTP round-trips regardless of the number of bonds. Fetches UTXO existence (`gettxout`), block timestamps (`getblockhash` + `getblockheader`) in batched calls.
  - **Neutrino backend**: Verifies bonds via the `v1/utxo` endpoint with address hints derived from the bond proof (pubkey + locktime), solving the previous inability to verify bonds on Neutrino (which requires an address to scan compact block filters).
  - **Mempool backend**: Falls back to the existing MempoolAPI when no local node backend is configured.
  - **`jmcore`**: Added `derive_bond_address()` and `BondAddressInfo` to `btc_script.py` for P2WSH address derivation from bond proofs, centralizing this logic.
  - **Taker**: `_update_offers_with_bond_values` now delegates to `verify_bonds()` instead of calling MempoolAPI directly.
  - **Orderbook Watcher**: `OrderbookAggregator` uses `verify_bonds()` with fallback to MempoolAPI when no backend is configured. Orderbook watchers in local node setups no longer leak bond queries to mempool.space.
- **`jmwalletd` — JAM-compatible HTTP/WebSocket API daemon**: New monorepo package implementing the JoinMarket wallet RPC API as a FastAPI application, designed as a drop-in replacement for the reference `jmwalletd`. Enables the JAM web UI to work with joinmarket-ng's backend.
  - Full REST API on `/api/v1` matching the reference implementation's endpoints: wallet lifecycle (create, recover, open, lock, unlock), wallet data (display, UTXOs, addresses, seeds), transaction operations (direct send, freeze/unfreeze), CoinJoin control (taker, tumbler, maker start/stop), configuration (get/set), and session management.
  - WebSocket endpoint at `/jmws` (JAM-compatible), `/ws`, and `/api/v1/ws` for real-time CoinJoin state notifications with JWT authentication and heartbeat.
  - JWT authentication with HS256 access tokens (30min) and refresh tokens (4hr), matching the reference auth flow including the custom `x-jm-authorization` header.
  - Self-signed TLS certificate generation for HTTPS/WSS.
  - Backend factory supporting multiple wallet backends (descriptor, bitcoin-core, neutrino, mempool).
  - 161 unit tests with full coverage of auth, models, state, dependencies, routers, wallet operations, and WebSocket.

## [0.18.0] - 2026-03-02

### Breaking Changes

The following CLI options have been **removed** from all commands (`jm-wallet`, `jm-maker`, `jm-taker`):

| Removed option | Replacement |
|---|---|
| `--mnemonic "word1 word2 ..."` | `MNEMONIC="word1 word2 ..." jm-wallet ...` |
| `--password "pw"` | `MNEMONIC_PASSWORD="pw" jm-wallet ...` or `wallet.mnemonic_password` in config |
| `--bip39-passphrase "phrase"` | `BIP39_PASSPHRASE="phrase" jm-wallet ...` or `--prompt-bip39-passphrase` |
| `--rpc-user "user"` | `BITCOIN_RPC_USER="user" jm-wallet ...` or `bitcoin.rpc_user` in config |
| `--rpc-password "pw"` | `BITCOIN_RPC_PASSWORD="pw" jm-wallet ...` or `bitcoin.rpc_password` in config |
| `validate <mnemonic>` (positional) | `MNEMONIC="..." jm-wallet validate` or `jm-wallet validate --mnemonic-file wallet.mnemonic` |

These secrets were leaking into shell history, `/proc/PID/cmdline`, `ps aux`, and audit logs.

For unattended/automated operation, set `MNEMONIC_PASSWORD` (or `wallet.mnemonic_password` in config) so encrypted mnemonic files can be decrypted without a terminal prompt.

### Added

- **Signet infrastructure defaults**: The joinmarket-ng public signet directory node (`signetvaxgd3ivj4tml4g6ed3samaa2rscre2gyeyohncmwk4fbesiqd.onion:5222`) is now the default when signet network is selected. The public orderbook watcher for signet is available at `https://joinmarket-ng-signet.sgn.space/`. Updated `config.toml.template` and `orderbook_watcher/.env.example` with signet examples.

### Security

- **Remove sensitive credentials from CLI arguments** (#130, #132, #133, #136): The removed options appeared in shell history, `/proc/PID/cmdline`, `ps aux`, and audit logs. Secrets are now supplied via environment variables, config file, or interactive prompt. Added `MNEMONIC_PASSWORD` env var support for unattended decryption of encrypted mnemonic files.
- **Fix bech32 checksum bypass in send command (SND-1)**: The hand-rolled bech32 decoder in `_send_transaction` stripped the 6-character checksum without verifying it, meaning a single-character typo in a destination address would silently send funds to a permanently unspendable output. Replaced with the `bech32` library which properly validates checksums per BIP173. Also fixed: unhandled `ValueError` on non-bech32 characters (e.g. uppercase from QR decoders), and `IndexError` on truncated addresses. The same hand-rolled encoder in the neutrino backend was replaced with `bech32.encode()`.

## [0.17.0] - 2026-02-25

### Added

- **`--no-fidelity-bond` flag for maker**: A new CLI flag `--no-fidelity-bond` (config: `no_fidelity_bond = true`) allows running the maker without a fidelity bond proof even when bonds are present in the registry. This is useful for privacy: fidelity bonds are public and linkable to your offers. Mutually exclusive with `--fidelity-bond`, `--fidelity-bond-locktime`, and `--fidelity-bond-index`.

### Fixed

- **SOCKS5h Proxy Incompatibility with httpx-socks**: The `python-socks` library (used by `httpx-socks`) does not recognise the `socks5h://` URL scheme and raises `ValueError`, which was silently caught. This caused `MempoolAPI` and the GitHub update checker to fall back to direct connections without any proxy, failing with DNS resolution errors on `.onion` addresses ("Temporary failure in name resolution"). Added `normalize_proxy_url()` helper in `tor_isolation` that converts `socks5h://` to `socks5://` + `rdns=True`, enabling remote DNS resolution through the Tor SOCKS proxy. Applied to both `MempoolAPI` and `check_for_updates_from_github`.

## [0.16.0] - 2026-02-24

### Added

- **Enhanced Periodic Summary Stats**: The periodic summary notification and CLI `history --stats` now include:
  - **Volume split**: Volume is shown as "successful / total" to distinguish completed CoinJoin volume from total requested volume (including failed attempts).
  - **UTXOs disclosed**: Tracks the number of UTXOs disclosed to takers via `!ioauth`. This counts all UTXOs exposed regardless of whether the CoinJoin completed, since UTXO disclosure is a privacy-relevant event even when transactions fail.
- **Version and Update Check in Summary Notifications**: The periodic summary notification can now include the current version and notify when a newer release is available on GitHub. Opt-in via `check_for_updates = true` in `[notifications]`. The request is routed through Tor when `use_tor` is enabled. Privacy warning: this polls `api.github.com` each summary interval.
- **Tor Stream Isolation**: All outbound Tor connections are now isolated by purpose using SOCKS5 authentication credentials, so that directory, peer, mempool, notification, update-check, and health-check traffic each use separate Tor circuits. This prevents traffic correlation between connection types. Leverages Tor's built-in `IsolateSOCKSAuth` flag -- no Tor configuration changes required. Enabled by default (`stream_isolation = true` in `[tor]`). Six isolation categories: `DIRECTORY`, `PEER`, `MEMPOOL`, `NOTIFICATION`, `UPDATE_CHECK`, `HEALTH_CHECK`. Applied across maker, taker, and orderbook watcher components.

### Fixed

- **Orderbook Watcher DNS Leak**: The orderbook watcher's mempool API proxy used `socks5://` (local DNS resolution) instead of `socks5h://` (DNS resolved by Tor). This leaked DNS queries for `mempool.space` to the local resolver / ISP, even though the HTTP connection itself went through Tor. Now uses `socks5h://` consistently.
- **Wallet Not Reloaded After Bitcoin Core Restart**: When Bitcoin Core restarts while a maker (or taker) is running, the descriptor wallet is unloaded. All subsequent wallet RPC calls (`listunspent`, `listdescriptors`, etc.) fail with error -18 ("Requested wallet does not exist or is not loaded"), causing the wallet to report zero balance and reject CoinJoin requests. The `_rpc_call` method now detects error -18 on wallet-scoped calls, transparently reloads the wallet via `loadwallet`, and retries the failed call once. This makes both periodic rescans and in-flight CoinJoin requests resilient to Bitcoin Core restarts.

### Added

- **Cold Wallet Bond Spending (`spend-bond`)**: New CLI command to generate a PSBT (BIP-174) for spending cold storage fidelity bonds after locktime expires. The PSBT includes the CLTV witness script metadata needed for signing. Implements PSBT serialization from scratch in `jmcore/bitcoin.py`. Usage: `jm-wallet spend-bond <bond-address> <destination> --fee-rate 2.0`, then sign with one of the scripts below.
- **BIP32 Key Origin in Bond PSBTs**: The `spend-bond` command now accepts `--master-fingerprint` and `--derivation-path` to embed `PSBT_IN_BIP32_DERIVATION` (BIP-174 key type 0x06) in the PSBT. This allows HWI to automatically identify the signing key on the hardware wallet.
- **HWI Bond Signing Script**: New standalone `scripts/sign_bond_psbt.py` script for signing bond spending PSBTs via HWI (Hardware Wallet Interface). Supports Trezor, Coldcard, Ledger, and other HW wallets. No seed phrase required. Install with `pip install hwi`.
- **Mnemonic Bond Signing Script**: New standalone `scripts/sign_bond_mnemonic.py` script for signing bond spending PSBTs with a BIP39 mnemonic. Fully self-contained (no project dependencies beyond `coincurve`). Derives the private key from the mnemonic + BIP32 path, verifies it matches the PSBT, and outputs a signed transaction. Mnemonic is read via hidden input and cleared after use.

## [0.15.0] - 2026-02-14

### Fixed

- **Orderbook Watcher: Inflated Fidelity Bond Count**: The "Fidelity Bonds" stat and per-directory bond counts were counting offers-with-bonds instead of unique bonds (by UTXO). Makers with dual offers (relative + absolute) backed by the same bond were counted twice. The frontend now uses the already-deduplicated `fidelitybonds` array for the total count, and the backend deduplicates by UTXO key per directory.

- **Maker Handshake Protocol Incompatibility**: Fixed maker sending DN_HANDSHAKE (type 795, directory server format) instead of HANDSHAKE (type 793, peer client format) when responding to direct peer connections. The reference taker rejected these with "Unexpected dn-handshake from non-dn node", causing CoinJoin failures on direct connections. The maker now correctly responds with HANDSHAKE (793) using client format fields (`proto-ver`, `location-string`, `directory: false`). The orderbook watcher health checker was also updated to handle both response formats. Added regression tests that replicate the reference taker's validation logic.

- **Frozen UTXO Selector Crash** ([#125](../../issues/125)): Fixed `IndexError: list index out of range` when selecting frozen UTXOs in `jm-wallet send --select-utxos`. Frozen and locked fidelity bond UTXOs are now visible but unselectable in the interactive TUI, shown with `[-]` prefix. Toggle (Space/Tab) and "select all" (`a`) skip unselectable UTXOs. The footer displays selectable count accurately. Single-UTXO auto-selection respects frozen/locked status.

- **Frozen UTXO Display Inconsistencies** ([#126](../../issues/126)): Fixed multiple display issues with frozen UTXOs across commands:
  - Total Balance line now shows frozen amounts: `Total Balance: 30,200 sats (68,811 frozen)`.
  - Per-mixdepth balances in simple view show frozen amounts.
  - `[FROZEN]` tag moved after `(label)` in UTXO selector for consistency with `--extended` view.
  - `get_fidelity_bond_balance()` now excludes frozen UTXOs.
  - Taker interactive UTXO selection now shows frozen UTXOs as unselectable (previously they were silently filtered).

### Changed

- **Tor Connection Timeout Increased to 120s**: Increased the default Tor connection timeout from 30s to 120s across all components (maker, taker, directory client). The previous 30s timeout covered the entire SOCKS5 connection lifecycle (TCP + SOCKS negotiation + Tor circuit building + PoW solving), which is too short when PoW-protected hidden services are under DoS load. The reference JoinMarket implementation effectively has no SOCKS-level timeout (Twisted cancels the 60s timeout after TCP handshake, leaving circuit building with no limit). The new 120s default aligns with Tor's internal circuit timeout. Configurable via `connection_timeout` in the `[tor]` config section.

### Added

- **Periodic Summary Notifications**: Makers now receive daily summary notifications with CoinJoin statistics (requests, successes, failures, earnings, volume). Enabled by default with `notify_summary = true` and 24-hour interval. To disable, set `notify_summary = false` in config.toml `[notifications]` section. Configurable interval via `summary_interval_hours` (1-168). Respects existing privacy settings (`include_amounts`). Added `get_history_stats_for_period()` for time-filtered history stats.

- **Background Retry for Notifications**: Failed notifications are now automatically retried in the background with exponential backoff. This is critical for Tor-routed notifications where transient circuit failures are common. Retries never block the main process (fire-and-forget via `asyncio.create_task`). Enabled by default with 3 retry attempts and a 5-second base delay (doubling each attempt). Configurable via `retry_enabled`, `retry_max_attempts` (1-10), and `retry_base_delay` (1-60s) in the `[notifications]` config section. No new dependencies -- uses plain asyncio.

### Fixed

- **Taker History: Zero Mining Fee Recorded**: Fixed a bug where taker transaction history recorded `mining_fee=0` despite the taker paying the full mining fee. The history update after broadcast used `tx_metadata["fee"]` (the estimated fee from transaction construction) instead of `actual_mining_fee` (total inputs minus total outputs from the signed transaction). In sweep mode, these values diverge because the residual from integer rounding goes to miners. This caused the `Net Fee` column in `jm-wallet history` to show only maker fees, understating the taker's total cost.

## [0.14.0] - 2026-02-12

### Fixed

- **Taker Signature Completeness Check**: Fixed a bug in `_phase_collect_signatures` where the taker used `minimum_makers` to decide if enough signatures were collected. Once a transaction is built with specific maker inputs, every maker must provide valid signatures -- `minimum_makers` is only relevant during the filling phase. The old check could allow proceeding with missing signatures if `minimum_makers` was set lower than the actual number of makers in the transaction, producing an invalid (partially signed) transaction. The `add_signatures` method in `CoinJoinTxBuilder` now also raises `ValueError` if any input is missing a signature, as defense-in-depth.

### Added

- **UTXO Freezing** ([#104](../../issues/104)): Added `jm-wallet freeze` command to freeze/unfreeze individual UTXOs, preventing them from being used in automatic coin selection (taker, maker, and sweep operations). This is critical for privacy — preserving specific UTXO sizes, preventing dust attacks, and excluding newly deposited coins from being mixed.
  - **Interactive curses TUI**: Space/Tab to toggle freeze, j/k and arrow keys to navigate, a/n for freeze/unfreeze all, q to exit. Color-coded status indicators (red for frozen, green for spendable, magenta for fidelity bonds). Footer shows frozen count, frozen value, and spendable value. Optional `--mixdepth/-m` filter.
  - **BIP-329 JSONL persistence**: Frozen state is stored in `wallet_metadata.jsonl` using the BIP-329 label format with the `spendable` field on `output` type records. This gives Sparrow wallet interoperability for free — users can sync their coin control state between JoinMarket NG and Sparrow.
  - **Automatic exclusion**: Frozen UTXOs are excluded from `select_utxos()`, `get_all_utxos()`, `select_utxos_with_merge()`, and `get_balance()`. Makers won't advertise frozen funds, and takers won't use them.
  - **Visible in wallet info**: `jm-wallet info` shows frozen amounts per mixdepth in simple view and `[FROZEN]` tags on addresses in extended view.
  - **UTXO selector integration**: The interactive UTXO selector (`--select-utxos`) now shows frozen indicators and prevents selecting frozen UTXOs via "select all".
  - **Comprehensive e2e test suite**: 36 end-to-end tests covering freeze/unfreeze persistence, balance exclusion, UTXO selection exclusion across maker/taker/send paths, BIP-329 persistence and hot-reload, Sparrow interop, read-only filesystem handling, and realistic usage scenarios.

### Changed

- **Directory Disconnect Notification Defaults**: Changed `notify_disconnect` default to `false` (was `true`). Individual directory server disconnect/reconnect notifications are noisy and not actionable. Added new `notify_all_disconnect` setting (default `true`) that fires only when ALL directory servers are disconnected, which is the critical event users need to know about. The `notify_all_directories_disconnected()` method now respects this toggle.

## [0.13.12] - 2026-02-09

### Fixed

- **Pin Python Build Tools for Reproducible Builds**: Pinned `setuptools` and `wheel` versions in all Dockerfiles via `PIP_CONSTRAINT`. When pip builds local packages (jmcore, jmwallet, taker, etc.) via PEP 517 build isolation, it downloads the latest `setuptools` from PyPI. The setuptools version is stamped into each package's `WHEEL` metadata file (`Generator: setuptools (x.y.z)`), and different versions produce different `WHEEL` and `RECORD` file contents. This caused the pip packages layer to have different digests between CI build time (e.g., setuptools 81.0.0) and local verification days later (e.g., setuptools 82.0.0). The `./scripts/update-base-images.sh` script now also updates these pinned versions from PyPI.

- **Maker Infinite Loop on Connection Reset**: Fixed a tight infinite loop in the maker bot that occurred when a directory server connection was reset. A `ConnectionResetError` (errno 104) was not recognized by the string-based error detection in `listen_for_messages()`, causing the loop to `continue` immediately and retry the broken connection with zero delay. This flooded logs and consumed all available RAM over time. The fix adds proper exception type catching in `TCPConnection.receive()` for `OSError`/`ConnectionError`, replaces fragile string matching with explicit exception handling in `listen_for_messages()` with consecutive error tracking, and adds exponential backoff with max error limits in the maker's `_listen_client()` loop.

- **Missing maker-data Docker Volume**: Added the `maker-data` named volume to the root `docker-compose.yml` volumes section. It was referenced by the maker service but not declared, which could cause issues on some Docker versions.

### Changed

- **Docker Resource Limits for Test Environment**: Added deploy resource limits (1 CPU, 512MB memory) to all services in the root `docker-compose.yml` (test environment) to prevent runaway resource consumption from bugs like the infinite loop above. Component-specific docker-compose files (`maker/`, `taker/`, etc.) already had resource limits configured.

## [0.13.11] - 2026-02-08

### Fixed

- **Pin Apt Package Versions for Reproducible Builds**: All apt packages in Dockerfiles are now pinned to exact versions (e.g., `libsodium23=1.0.18-1+deb13u1`). Previously, `apt-get install` without version pins meant that a security update to any package (like libsodium23) between CI build time and local verification would produce a different layer digest, breaking `verify-release.sh --reproduce` within days of release.

- **Auto-Setup BuildKit Builder for OCI Export**: The `verify-release.sh --reproduce` and `sign-release.sh --reproduce` scripts now automatically detect when the current Docker buildx driver doesn't support OCI export format and create a suitable builder (`jmng-verify`) with the `docker-container` driver. Previously, users with plain Docker CE (without Docker Desktop or containerd image store) would get "OCI exporter is not supported for the docker driver" errors.

### Changed

- **update-base-images.sh Now Updates Apt Versions**: The `./scripts/update-base-images.sh` script now also resolves the latest available apt package versions from the base image and updates pinned versions in all Dockerfiles. This ensures that running the script before a release picks up both base image security patches and apt package updates in a single step.

## [0.13.10] - 2026-02-06

### Fixed

- **User Creation Shadow File Reproducibility**: Fixed reproducible builds broken by `useradd` setting the "last password change" field in `/etc/shadow` to the current day (days since Unix epoch). When verifying a release on a different day than CI built it, layer 7 (useradd) would have different digests. Now, if `SOURCE_DATE_EPOCH` is set, we calculate days from that epoch and fix the shadow entry to match.

- **Source File Timestamp Normalization**: Fixed reproducible builds for orderbook-watcher by normalizing source file timestamps to `SOURCE_DATE_EPOCH` in the builder stage. BuildKit's `rewrite-timestamp=true` only modifies the OCI tar output, not layer content hashes. Layer digests are computed before rewriting, so files must have identical timestamps during the build. Without normalization, local files (with old modification times) differ from CI (fresh git clone with recent times).

## [0.13.9] - 2026-02-05

### Fixed

- **Orderbook-Watcher Reproducibility via Builder Stage**: Fixed reproducible builds for orderbook-watcher by copying source and static files through the builder stage with permission normalization. Previously, files were copied directly to the production stage, preserving local filesystem permissions (based on umask), and the post-copy chmod ran as user `jm` which couldn't fix permissions on directories with restrictive modes. Now, files are copied to builder, normalized to 644/755 as root, then copied to production with `--from=builder`.

- **Root .dockerignore**: Added a root-level `.dockerignore` file to exclude development artifacts (`*.egg-info/`, `__pycache__/`, `*.pyc`, etc.) from Docker build context. These files don't exist in CI (fresh git clone) but accumulate locally during development, causing COPY layer mismatches.

## [0.13.8] - 2026-02-05

### Fixed

- **Empty Tor Cookie File Detection**: Cookie path auto-detection now verifies that the cookie file has content (non-zero size) before using it. Previously, an empty cookie file at `/run/tor/control.authcookie` would be selected, causing Tor authentication to fail with "cookie of size zero" errors.

- **Install Script Tor Configuration**: The install script now explicitly sets `CookieAuthFile /run/tor/control.authcookie` in torrc. Previously, only `CookieAuthentication 1` was set, leaving the cookie path to Tor's default which varies by distribution.

- **Install Script Update Mode Torrc Verification**: Running `install.sh --update` now verifies and fixes the Tor configuration if the JoinMarket-NG section is missing, commented out, or incomplete (e.g., missing `CookieAuthFile`).

- **Orderbook-Watcher File Permission Reproducibility**: Added permission normalization step to the orderbook-watcher Dockerfile. Previously, files copied directly to the production stage preserved local filesystem permissions (based on umask), causing builds to differ across systems. The new `RUN find ... chmod` step ensures consistent 644/755 permissions regardless of the build environment.

### Added

- **Skip Signature Verification Option**: Added `--skip-signatures` flag to `verify-release.sh` for testing reproducibility without requiring GPG signatures.

## [0.13.7] - 2026-02-05

### Fixed

- **File Timestamp Reproducibility with rewrite-timestamp**: Added `rewrite-timestamp=true` to Docker build outputs in both CI and verification scripts. This BuildKit feature clamps all file timestamps inside image layers to `SOURCE_DATE_EPOCH`, ensuring files created by `apt-get install`, `pip install`, and other commands have consistent timestamps regardless of when the build runs. Without this, directories like `/etc`, `/var/lib/apt`, etc. have timestamps from build time, causing layer digest mismatches.

- **Verification Script Target Mismatch**: Fixed `verify-release.sh --reproduce` and `sign-release.sh --reproduce` to specify the correct `--target` for each image, matching the CI workflow. Previously, `directory-server` was being built without a target, which defaults to the last stage (`debug`) instead of `production`.

### Note

Releases prior to these changes (including 0.13.5, 0.13.6, and 0.13.7) cannot be fully reproduced locally for the orderbook-watcher image due to file permission differences. Files copied directly to the production stage in orderbook-watcher preserved local filesystem permissions, which vary based on umask settings. CI runners typically use umask 0022 (resulting in 644 files), while developer machines often use umask 0002 (resulting in 664 files). Only releases built with the permission normalization fix will have fully reproducible orderbook-watcher images.

## [0.13.6] - 2026-02-05

### Changed

- **Disabled Build Cache for CI Releases**: Added `no-cache: true` to the CI release workflow. Cached layers from previous builds may contain different package versions, making local reproduction impossible. Fresh builds ensure consistency between CI and local verification.

- **Base Image Digest Pinning**: All Dockerfiles now pin Python base images by manifest list digest for reproducible builds. This ensures the exact same base image is used across builds, regardless of when they run. Use `./scripts/update-base-images.sh` to update digests when new Python images are released.

- **Faster Verification with Git Worktree**: `verify-release.sh --reproduce` and `sign-release.sh --reproduce` now use `git worktree` instead of cloning from GitHub. This is faster and more secure - it uses locally verified code rather than trusting the remote blindly. Users must have the commit locally (run `git fetch origin` if needed).

### Added

- **Base Image Update Script**: New `scripts/update-base-images.sh` script to update Python base image digests in all Dockerfiles. Run periodically to get security updates while maintaining reproducibility. Use `--check` to verify if updates are needed.

## [0.13.5] - 2026-02-05

### Changed

- **Layer-Based Reproducibility Verification**: Replaced manifest digest comparison with layer digest comparison for reproducible build verification. Layer digests are content-addressable hashes of actual image content and are identical regardless of manifest format (Docker vs OCI). This fixes the fundamental issue where CI builds (pushed to registry) produce Docker distribution manifests while local builds produce OCI manifests - even for identical image content, these have different manifest digests. By comparing layer digests instead, verification works reliably across different build environments.

- **Simplified CI Release Workflow**: Removed the slow OCI tar rebuild step from the CI release workflow. Previously, after pushing to the registry, CI would rebuild each platform as an OCI tar to extract digests - this caused timeouts (30+ minutes per image). The new approach extracts layer digests directly from the pushed images using `docker buildx imagetools inspect`, which is fast and reliable.

- **Updated Release Manifest Format**: The release manifest now contains per-platform layer digests in addition to manifest digests. Layer digests are listed under `### <image>-<arch>-layers` sections, enabling local verification to compare the actual image content rather than manifest metadata.

## [0.13.4] - 2026-02-05

### Changed

- **Use OCI Digests for Reproducible Build Verification**: The release manifest now contains OCI tar digests instead of registry manifest digests. CI builds each platform image as an OCI tar (in addition to pushing to registry) and stores those digests in the manifest. This ensures local verification produces the exact same digest as CI, since both use the same output format (`type=oci,dest=...,rewrite-timestamp=true`). Previously, local verification used OCI output while CI stored registry digests, which are fundamentally different even for identical image content.

- **Enabled rewrite-timestamp for Reproducible Builds**: Added `rewrite-timestamp=true` to Docker build outputs in CI and verification scripts. This BuildKit feature clamps all file timestamps inside images to `SOURCE_DATE_EPOCH`, ensuring that file metadata (like directory mtimes created by apt-get, ldconfig) doesn't vary between builds. Combined with disabling attestations, this achieves true reproducible Docker builds.

### Fixed

- **Docker Image Reproducibility (ldconfig cache)**: Added deletion of `/var/cache/ldconfig/aux-cache` after apt-get install in all Dockerfiles. This binary cache file contains non-deterministic data that caused builds to differ even with the same inputs.

## [0.13.3] - 2026-02-05

### Changed

- **Disabled Docker Attestations for Reproducible Builds**: Disabled provenance and SBOM attestations in the CI release workflow (`provenance: false`, `sbom: false`). These attestations include timestamps and environment-specific data that made builds non-reproducible across different build environments. While this removes supply chain metadata from images, it enables true reproducibility verification where anyone can build the same image and get the exact same digest.

## [0.13.2] - 2026-02-04

### Changed

- **Maker `min_size` Default Reduced to Dust Threshold**: Changed the default `min_size` for maker offers from 100,000 sats to 27,300 sats (the dust threshold). The previous 100k default was arbitrary and prevented makers with smaller UTXOs from participating. The dust threshold is the true minimum for any Bitcoin output, making it the natural floor for CoinJoin amounts.

- **Simplified Reproducibility Verification**: The `verify-release.sh --reproduce` and `sign-release.sh --reproduce` scripts no longer require a local Docker registry. Instead, they use OCI tar export (`--output type=oci,dest=...`) to extract the manifest digest directly from the built image. This reduces dependencies (no registry container needed) and is more reliable.

### Fixed

- **Reproducibility Verification Digest Extraction**: Fixed `verify-release.sh --reproduce` and `sign-release.sh --reproduce` to correctly extract platform-specific image digests instead of manifest list digests. When building with `--load`, Docker creates a manifest list that includes attestations, resulting in a different digest than the actual platform image. The scripts now use `jq` to extract the correct digest from `.manifests[]` excluding attestation manifests (platform.os != "unknown"), matching the CI workflow's digest extraction logic.

- **Docker Image Reproducibility**: Fixed Dockerfiles to delete apt/dpkg log files (`/var/log/dpkg.log`, `/var/log/apt/*`) after package installation. These logs contain timestamps that made builds non-reproducible across different build times. This affects all four images: maker, taker, directory-server, and orderbook-watcher.

## [0.13.1] - 2026-02-04

### Fixed

- **Release Verification Script Now Fails on Reproduce Errors**: Fixed `verify-release.sh --reproduce` to properly fail (exit 1) when locally built Docker images have different digests than the release manifest. Previously, digest mismatches were only logged as warnings and the script would exit successfully.

- **Single-Architecture Reproducibility Verification**: Fixed `verify-release.sh --reproduce` and `sign-release.sh` to build only for the current machine's architecture (e.g., amd64 on x86_64, arm64 on Apple Silicon/RPi4). Previously attempted to build all 3 platforms which was slow and unnecessary. Verification now also cross-checks the built image against both the manifest and the published registry image to ensure the release wasn't tampered with.

### Changed

- **Per-Platform Digests in Release Manifest**: The release manifest now stores individual digests for each platform (`maker-amd64`, `maker-arm64`, `maker-arm-v7`) in addition to the manifest list digest (`maker-manifest`). This enables faster verification by building only the current architecture while keeping provenance/SBOM attestations enabled for supply chain security.

- **All Signers Must Reproduce Builds**: The `sign-release.sh` script now enables `--reproduce` by default for all signers. Multiple signatures only add value if each signer independently verifies reproducibility. Use `--no-reproduce` to skip verification (not recommended).

## [0.13.0] - 2026-02-04

### Added

- **NUMS Point Generation Algorithm** ([#101](../../issues/101)): Added explicit documentation and implementation of the NUMS (Nothing Up My Sleeve) point generation algorithm for PoDLE commitments. The `generate_nums_point()` function now transparently generates deterministic NUMS points using SHA256 hashing of secp256k1's generator G. NUMS points are cached for efficiency and validated against test vectors from the original JoinMarket implementation. Support for NUMS indices expanded from 10 to the full range of 256 (0-255), providing generous headroom for multiple commitment reuses per UTXO.

- **Tor-Level DoS Defense for Hidden Services**: Makers can now configure Tor-level DoS protection for their hidden services via the `hidden_service_dos` config option. This includes:
  - **Proof-of-Work Defense** (`PoWDefensesEnabled`): Computational puzzle that clients must solve to connect. Makes flooding attacks expensive. Enabled by default with suggested effort starting at 0 (no puzzle required for normal operation) and auto-scaling under attack.
    - For ephemeral HS (ADD_ONION): Requires **Tor 0.4.9.2+** (not yet in stable releases)
    - For persistent HS (torrc): Requires Tor 0.4.8+ with `--enable-gpl` build
  - **Max Streams per Circuit** (`max_streams`): Limit concurrent streams per rendezvous circuit.
  - Automatic capability detection for Tor version and PoW module availability.
  - **Note**: Introduction point rate limiting (`HiddenServiceEnableIntroDoSDefense`) is NOT supported for ephemeral hidden services due to Tor control protocol limitations. Users who need this protection should configure persistent hidden services in torrc. See INSTALL.md for configuration examples.
  - Reference: https://community.torproject.org/onion-services/advanced/dos/

- **Connection-Based Rate Limiting for Direct Connections**: Added `DirectConnectionRateLimiter` that tracks by connection address (peer_str) instead of nick. This prevents nick rotation attacks where attackers use a random nick per request to bypass the existing nick-based rate limiting. Direct connections now have stricter limits: 30s orderbook interval (vs 10s), 10 violations to ban (vs 100), and general message rate limiting (5 msg/s with 20 burst).

### Fixed

- **Taker History Update Failure in Sweep Mode**: Fixed a bug where taker history entries were not being updated after a successful sweep CoinJoin. The issue occurred because a change address was always generated (even when not needed), but not always used in the transaction. This caused history matching to fail because the recorded change address didn't match reality. The fix prevents generating a change address when it's not needed: the taker now calculates whether change will exceed the dust threshold before generating an address. If no change output will be created (sweep mode or dust), no address is generated, and an empty string is stored in history. This ensures history accurately reflects which addresses were actually revealed in transactions.

- **Fidelity Bond Address Detection During Sync**: Fixed a bug where fidelity bond addresses were incorrectly flagged as "out of range" during wallet sync, triggering an unnecessary extended range search (~40 seconds delay). The root cause was that `_find_address_path()` only searched branches 0 and 1 (external/internal), but fidelity bond addresses use branch 2. The fix checks the fidelity bond registry before falling back to expensive derivation scanning, allowing bond addresses to be identified immediately.

- **Early Fund Validation for CoinJoin** ([#102](../../issues/102), [#106](../../issues/106)): Added early fund validation for `jm-taker coinjoin` to check if sufficient funds are available before connecting to directory servers. This avoids unnecessary waiting time when the wallet has insufficient funds. The `Taker` class now exposes `sync_wallet()` and `connect()` methods separately, allowing the CLI to validate funds after wallet sync but before directory connection. Additionally, when using `--select-utxos`, funds are now validated immediately after UTXO selection (fixing the bug where coinjoins would start with insufficient funds and only fail later with "Failed to generate PoDLE commitment").

### Changed

- **Improved CoinJoin Confirmation Display** ([#110](../../issues/110)): Redesigned the `jm-taker coinjoin` confirmation screen for better readability:
  - Title changed from "EXPECTED CJ TX" (all caps) to "Expected COINJOIN Transaction" (mixed case)
  - Information displayed in column form with consistent label widths
  - Reordered fields to match workflow: Source Mixdepth → Destination → CoinJoin Amount → Makers → Fees
  - Added "Miner Fee Rate" display (sat/vB)
  - Maker list now shows right-aligned fee and bond values for easier comparison
  - Removed redundant "Counterparties" field (count now shown inline as "Makers (N):")

## [0.11.6] - 2026-02-03

### Fixed

- **CoinJoin Confirmation Total Fee Display** ([#109](../../issues/109)): Fixed a bug where the "Total Fees (makers+network)" in the CoinJoin confirmation prompt only showed maker fees, not the actual total. The display now correctly shows the sum of maker fees and mining fees.

- **Address Reuse After Counterparty Disappears (Maker & Taker)**: Fixed a critical privacy bug affecting both makers and takers where addresses revealed during the CoinJoin protocol could be reused if the counterparty disappeared before the transaction completed.
  - **Maker fix**: Addresses revealed during `!ioauth` are now recorded to history before sending the response, ensuring they are blacklisted even if the taker disappears before sending `!tx`.
  - **Taker fix**: Addresses included in the `!tx` message (destination and change addresses) are now recorded to history before sending to makers, ensuring they are blacklisted even if makers don't respond with signatures or the broadcast fails.
  - Previously, both roles only recorded addresses to history after successful transaction signing/broadcast. Now, addresses are recorded **before** being revealed, with the history entry updated later with txid and fee information.
  - The `create_taker_history_entry()` function now requires a `change_address` parameter to ensure taker change addresses are also tracked and blacklisted.
  - Addresses are persisted before being revealed to prevent reuse even in failure scenarios.

## [0.11.5] - 2026-01-24

### Fixed

- **Maker Advertising Fidelity Bond Funds as Spendable**: Fixed a bug where makers would include fidelity bond (FB) UTXOs in their advertised max size, leading to failed CoinJoins when takers requested amounts that could only be satisfied by including the FB funds. The fix adds `get_balance_for_offers()` method that excludes all FB UTXOs, and updates the maker offer creation and mixdepth selection to use this balance. UTXO selection methods (`select_utxos`, `select_utxos_with_merge`, `get_all_utxos`) now exclude FB UTXOs by default via the `include_fidelity_bonds` parameter. The `jm-wallet info` command now shows FB balance separately.

- **External Fidelity Bonds Not Recognized During Sync**: Fixed a bug where external fidelity bonds (cold storage bonds with `index=-1`) were not being properly recognized during wallet sync. These UTXOs were incorrectly treated as regular spendable funds instead of fidelity bonds, causing them to be included in offer balances and potentially leading to failed CoinJoins. The fix adds additional checks in `sync_with_descriptor_wallet()` to recognize fidelity bond addresses from the registry even when they don't match through the primary lookup path.

## [0.11.4] - 2026-01-23

### Fixed

- **Address Reuse Bug for Used-but-Empty Addresses**: Fixed a critical privacy bug where addresses that had been used (received and spent funds) would incorrectly show as "new" instead of "used-empty". This could lead to address reuse, a serious privacy concern for CoinJoin wallets. The root cause was that `listsinceblock` and `listtransactions` RPCs don't reliably return transaction details for addresses in descriptor wallets, especially after wallet import. The fix uses `listaddressgroupings` RPC as the primary source for detecting used addresses, which reliably returns all addresses that have been involved in any transaction (as inputs or outputs). This is combined with `listsinceblock` as a secondary source for completeness.

## [0.11.3] - 2026-01-22

### Fixed

- **Descriptor Wallet Sync Hanging with Deep History**: Fixed a critical bug where wallets with address history at indices beyond the current descriptor range would cause sync to hang or fail to find those addresses. This affected users migrating from other wallet software or with extensive transaction history. The fix includes:
  - Added `_find_address_path_extended()` to search beyond the current descriptor range when addresses are not found
  - Addresses from transaction history beyond the current range now trigger an extended search (up to 5000 indices beyond)
  - Once found, the descriptor range is automatically upgraded to accommodate the high-index addresses
  - Added detailed progress logging for address cache population (shows ETA for large caches)
  - Added logging to track addresses found beyond the current range

- **Extended Address Search for Non-Wallet Addresses**: Fixed a performance issue where the extended address range search would unnecessarily search for counterparty addresses from CoinJoin transactions. The `get_addresses_with_history()` method now excludes "send" category addresses (addresses we sent to, not our own) which don't belong to this wallet. This prevents slow extended searches after CoinJoin transactions and ensures makers restart quickly between transactions.

## [0.11.2] - 2026-01-21

### Changed

- **Dependency Lock Files with Hashes**: Updated all dependency lock files (`requirements.txt` and `requirements-dev.txt`) to include SHA256 hashes for enhanced security. This ensures package integrity verification during installation. The `scripts/update-deps.sh` script now uses `pip-compile --generate-hashes` flag. The `coincurve` dependency is pinned to a specific commit hash for reproducibility and hash verification.

### Added

- **Nick State Files for External Tracking**: All components (maker, taker, directory server, orderbook watcher) now write their nick to a state file at startup (`~/.joinmarket-ng/state/<component>.nick`). This allows operators to easily identify running bots' nicks for external monitoring and tracking. The files are automatically cleaned up on shutdown.

- **Nick Included in Startup Notifications**: Startup notifications now include the component's nick in the notification body, making it easier for operators to identify which bot sent the notification without needing to check logs.

- **Self-CoinJoin Protection**: When running both maker and taker from the same wallet/data directory, the components now automatically detect and protect against self-CoinJoins:
  - Taker reads the maker nick state file and automatically excludes it from peer selection
  - Maker reads the taker nick state file and rejects fill requests from its own taker nick
  - This protection is automatic and requires no configuration

### Fixed

- **Spent Addresses Shown as 'new' After Wallet Import**: Fixed a bug where addresses that had received and spent funds (now empty) would incorrectly show as 'new' instead of 'used-empty' after importing a wallet from mnemonic. The issue was that `sync_with_descriptor_wallet()` only added addresses to `addresses_with_history` if they were already in the address cache. If the cache wasn't populated far enough, spent addresses returned by `get_addresses_with_history()` would be silently ignored. The fix uses `_find_address_path()` which will derive and find addresses even if not in the initial cache.

- **DirectoryServer Shutdown Hang in Python 3.12+**: Fixed a hang during test fixture teardown when using Python 3.12+. The `DirectoryServer.stop()` method now properly tracks and cancels client handler tasks before calling `wait_closed()`, which in Python 3.12+ waits for all handler tasks to complete. Added timeout safeguards to both `stop()` and test fixtures to prevent indefinite hangs.

- **CoinJoin Confirmation Prompt Input Handling**: Fixed an issue where user confirmation ("y") would be incorrectly declined during the final broadcast confirmation. The stdin buffer is now properly flushed before reading user input to avoid stale data when running in asyncio context.

- **Encrypted Mnemonic Decryption Error Handling**: Fixed an unhandled `UnicodeDecodeError` that could occur when loading encrypted mnemonic files from config. If the decrypted content is not valid UTF-8 (e.g., file corrupted or encrypted with a different tool), the error is now caught and a clear error message is displayed instead of a raw codec error.

- **Default Wallet Uses Config Password**: Fixed an issue where `wallet.mnemonic_password` from config was not used when loading the default wallet at `~/.joinmarket-ng/wallets/default.mnemonic`. Previously, setting `mnemonic_password` in config only worked if `mnemonic_file` was also explicitly set. Now the config password is used for the default wallet path as well, eliminating the need to set `mnemonic_file` when using the default location. Also consolidated mnemonic resolution logic from jmwallet into the shared `resolve_mnemonic` function in jmcore.

- **Directory Server Uses Random Nick**: Fixed the directory server to use a random JM-format nick (e.g., `J5FA1Gj7Ln4vSGne`) instead of a hardcoded `directory-{network}` nick. This matches the reference implementation behavior where directory servers use the same nick format as any other peer.

- **Descriptor Wallet Gap Limit Bug**: Fixed a critical bug where wallets with more than 1000 addresses would show 0 balance in `jm-wallet info` despite having funds. The issue was threefold:
  1. `_find_address_path()` only scanned up to index 100, so addresses beyond that were marked "unknown"
  2. `DEFAULT_SCAN_RANGE` (1000) was used as a max index rather than a true gap limit
  3. No mechanism existed to upgrade descriptor ranges when wallets grew beyond the initial range

  The fix includes:
  - `_find_address_path()` now scans up to the full descriptor range (retrieved from Bitcoin Core)
  - Pre-populate address cache during sync for O(1) lookups
  - Automatic detection and upgrade of descriptor ranges when highest used index approaches the limit
  - Added `get_descriptor_ranges()`, `get_max_descriptor_range()`, and `upgrade_descriptor_ranges()` methods to DescriptorWalletBackend
  - Added `check_and_upgrade_descriptor_range()` method to WalletService that automatically expands ranges as needed

- **recover-bonds Now Waits for Wallet Rescan**: Fixed a bug where `jm-wallet recover-bonds` would attempt to query UTXOs before the wallet rescan completed, causing "Wallet is currently rescanning" errors or missing bond discovery. The command now properly waits for each batch of descriptor imports to finish rescanning before querying for UTXOs. Added `wait_for_rescan_complete()` method to the descriptor wallet backend.

- **list-bonds Now Updates Registry with Discovered Bonds**: Fixed a bug where `jm-wallet list-bonds --locktime` would find bonds on the blockchain but not save them to `fidelity_bonds.json`. Now when bonds are discovered via `--locktime` scanning, they are automatically added to the registry with full UTXO information (txid, vout, value, confirmations). Existing registry entries also get their UTXO info updated.

### Changed

- **Improved CoinJoin Transaction Summaries**:
  - Changed "Fee:" to "Total Fees (makers+network):" in confirmation prompts to clearly show it represents the sum of maker fees and mining fees
  - Added CSV entry logging when users decline to broadcast, allowing manual transaction tracking and later broadcast via the transaction hex

- **Improved Fidelity Bond Recovery Documentation**: Enhanced maker/README.md with detailed fidelity bond recovery workflow including BIP39 passphrase handling. Added note in DOCS.md clarifying that BIP39 passphrases are intentionally not read from config.toml for security reasons.

## [0.11.0] - 2026-01-20

### Added

- **Fidelity Bond Tool ASCII Signature Format Support**: The `fidelity_bond_tool.py` script now correctly verifies certificate signatures in both binary and ASCII message formats. Previously, it only tried the binary format (raw pubkey bytes in the message), which failed for cold storage bonds where the certificate was signed using Sparrow Wallet's message signing feature. The ASCII format (hex pubkey string in the message) is now also tried, matching the behavior of the reference implementation and our `verify_fidelity_bond_proof` function. The tool now also reports which format was used for successful verification.

- **Enhanced Fidelity Bond Modal in Orderbook Watcher**: The bond details modal now shows comprehensive verification information similar to `fidelity_bond_tool.py`:
  - **Verification summary banner** at the top with color-coded status (green=valid, yellow=expired cert, blue=pending)
  - **Certificate details section** showing UTXO pubkey (cold wallet), certificate pubkey (hot wallet), and certificate type (self-signed vs delegated)
  - **Certificate expiry validation** fetches current block height from Mempool API and shows remaining validity or expiration status
  - **Improved locktime display** shows human-readable unlock date with time remaining
  - Helps diagnose why a bond may show value 0 (e.g., expired certificate)

- **Improved Offer Type Configuration Documentation and Logging**: Enhanced maker configuration to make the `offer_type` setting more intuitive:
  - Updated `config.toml.template` with clearer documentation explaining that `offer_type` must be explicitly set to use absolute fees (simply setting `cj_fee_absolute` alone is not sufficient)
  - Added startup logging that clearly shows the configured offer type and fee (e.g., "Offer config: type=sw0reloffer, relative fee=0.001 (0.1000%)")
  - Added detailed startup logging when using `--dual-offers` showing both offer configurations
  - Added summary log after offer creation showing all offers to be announced with their sizes and fees
  - Addresses issue [#86](../../issues/86) where users expected commenting out `cj_fee_relative` would switch to absolute fees

- **Real-Time Autocomplete for Mnemonic Input**: The `jm-wallet import` interactive mnemonic input now features real-time autocomplete suggestions as you type. When there are 10 or fewer matching BIP39 words, they are displayed inline in gray. When only one match remains (after typing 3+ characters), the word auto-completes automatically. Tab completion is also available for partial matches. The feature gracefully falls back to readline-based completion on terminals that don't support raw input mode. Additionally, you can now paste all words at once (or a subset), with validation and clear error messages for invalid words.

- **Component Name in Notification Titles**: Notifications now include the component name in the title, making it easier to identify which component sent a notification when running multiple JoinMarket components (Maker, Taker, Directory, Orderbook). For example, instead of "JoinMarket NG: Fill Request Received", notifications now show "JoinMarket NG (Maker): Fill Request Received". This is especially useful when running multiple components simultaneously and receiving notifications through a single channel.

- **Fix Scientific Notation in Maker Fee Offers**: Fixed an issue where small relative fee values (like `0.00001`) were being sent in scientific notation (e.g., `1e-05`) instead of decimal notation. This happened when the fee was configured as a float in TOML config or environment variables, and Python's default float-to-string conversion produced scientific notation. The JoinMarket protocol expects decimal notation, which could cause compatibility issues with reference implementations. Added field validators to normalize all `cj_fee_relative` values to proper decimal strings.

- **Improved Wallet Info Display**: Redesigned the `jm-wallet info` output to be clearer and less misleading:
  - **Standard view**: Balance and deposit addresses are now shown on separate lines with clear headers, instead of on the same line which could be misinterpreted as showing the balance at that specific address.
  - **Extended view**: Added a legend explaining address status labels (new, deposit, cj-out, non-cj-change, used-empty, flagged) so users can understand why addresses were skipped or marked as do-not-reuse.

- **Unconfirmed Transaction Display in Wallet Info**: The `jm-wallet info --extended` command now shows "(unconfirmed)" status for addresses with unconfirmed UTXOs. This detects pending transactions directly from the Bitcoin backend (via `listunspent` with `minconf=0`), providing visibility into unconfirmed funds even for direct sends that aren't tracked in CoinJoin history.

- **Spent Address Shows "used-empty" Instead of "new"**: Fixed a bug in `jm-wallet info --extended` where an address that previously had funds but was spent (outside of CoinJoin) would incorrectly show as "new" with 0 balance instead of "used-empty". The address display range calculation now correctly considers general blockchain activity (`addresses_with_history`) in addition to CoinJoin history.

- **Pending Transaction Timeout**: Maker now automatically marks pending CoinJoin transactions as failed after 60 minutes (configurable via `pending_tx_timeout_min` setting). This prevents the transaction history from being cluttered with entries that the taker never broadcast. Previously, these entries would remain in "pending" state indefinitely, causing repeated (and noisy) transaction lookup attempts in the logs.

- **Fix CoinJoin Address Labels Not Showing After Failed Retries**: Fixed a bug where addresses used in successful CoinJoin transactions would incorrectly show as "flagged" instead of "cj-out" (for CoinJoin outputs) or proper labels if the same address appeared in later failed transactions. This happened when a taker would retry using the same maker address multiple times, resulting in one successful entry and multiple failed entries in history. The fix ensures that successful transaction types take precedence - once an address is used in a confirmed CoinJoin, it keeps its "cj-out" or "change" label regardless of subsequent failed attempts.

- **Fix Address Reuse in Concurrent CoinJoin Sessions**: Fixed a critical privacy bug where the maker could reuse the same CoinJoin output and change addresses across multiple concurrent sessions. This occurred because addresses were only marked as "used" in history after the CoinJoin completed (when `!tx` was received), so a second `!fill` request arriving before the first completed would get the same addresses. The fix adds in-memory address reservation: when a maker sends `!ioauth` with addresses, those addresses are immediately reserved and will not be reused for subsequent sessions, even if the CoinJoin fails.

- **Mempool Min Fee Check for Wallet Send**: The `jm-wallet send` command now checks the fee rate against the node's mempool minimum fee (like the taker already does). If a manual `--fee-rate` is below the node's `minrelaytxfee`, a warning is logged and the mempool minimum is used instead, preventing "min relay fee not met" broadcast failures.

- **Minimum Relay Fee Documentation**: Added new section to DOCS.md explaining Bitcoin node fee rate configuration, including how to enable sub-satoshi fee rates via `minrelaytxfee` in `bitcoin.conf`.

- **Log Level CLI Flag Across All Components**: Added `--log-level` / `-l` flag to all CLI commands that were missing it:
  - `jm-maker start` and `jm-maker generate-address` commands
  - `jm-directory-server` CLI (status, health subcommands)
  - `jm-orderbook-watcher` main entry point
  - The flag accepts TRACE, DEBUG, INFO, WARNING, and ERROR levels (TRACE was not documented before)
  - Updated `config.toml.template` and settings documentation to include TRACE as a valid log level
  - Environment variable for log level is `LOGGING__LEVEL` (not `LOGGING__LOG_LEVEL` - the latter never worked)

- **Wallet Name in Startup Logs**: Both maker and taker now log the Bitcoin Core descriptor wallet name (e.g., `jm_xxxxxxxx_mainnet`) during startup when using the descriptor wallet backend. This makes it easier to identify which wallet is being used, especially when running multiple instances.

- **Accurate Fee Rate in Final Transaction Summary**: The taker's final transaction summary now displays the actual mining fee and fee rate calculated from the signed transaction. Previously, only the estimated fee was shown, which didn't account for residual/dust amounts absorbed into the fee when change would be below dust threshold. This is especially important for sweep transactions where the actual fee can be significantly higher than the estimate. The summary now shows actual vsize alongside byte size.

- **Automatic Password Prompt for Encrypted Mnemonics**: All CLI commands that load mnemonic files now automatically detect encrypted files (Fernet (AES)) and prompt for a password interactively. Previously, users had to explicitly pass `--password` on the command line, which led to confusing errors when trying to use encrypted mnemonic files. This works across `jm-taker`, `jm-maker`, and `jm-wallet` commands.

- **Password Confirmation Retry Loop**: The `jm-wallet import` and `jm-wallet generate` commands now retry password confirmation up to 3 times when passwords don't match, instead of immediately exiting. This improves the user experience by allowing correction of typos without having to restart the command.

- **BIP39 Passphrase Prompt for Maker/Taker**: Added `--prompt-bip39-passphrase` option to `jm-maker start` and `jm-taker coinjoin` commands. This allows users to enter their BIP39 passphrase interactively at startup rather than passing it via environment variable or command line argument.

- **Wallet Scan Start Height Setting**: New `scan_start_height` configuration option in `[wallet]` section allows specifying an explicit block height for initial wallet scanning. This is useful when you know when your wallet was first used, enabling faster initial sync for newer wallets.

- **Fee Rate Configuration Option**: Added `fee_rate` option to `[taker]` config section for manual fee rate specification in sat/vB. This takes precedence over `fee_block_target` when set, useful for users who prefer explicit fee rates over estimation.

- **Troubleshooting Documentation**: Added new "Troubleshooting" section to DOCS.md with:
  - Common `bitcoin-cli` debugging commands for wallet sync issues
  - Smart scan configuration tips for faster initial sync
  - RPC timeout troubleshooting guide

- **Reproducible Docker Builds**: All Docker images now support reproducible builds using `SOURCE_DATE_EPOCH`. This allows anyone to verify that released images were built from the published source code.
  - Dockerfiles updated to use `SOURCE_DATE_EPOCH` build arg for consistent timestamps
  - CI/CD workflows pass git commit timestamp to builds
  - Release workflow generates manifest files with image digests
  - New verification script: `scripts/verify-release.sh` to verify GPG signatures and image digests
  - New signing script: `scripts/sign-release.sh` for trusted parties to attest releases
  - GPG signature infrastructure in `signatures/` directory
  - Documentation added to DOCS.md and README.md

- **Directory Server Auto-Reconnection**: Makers now automatically attempt to reconnect to disconnected directory servers. This improves maker uptime and resilience against temporary network issues or directory server restarts. Previously, if a maker lost connection to a directory server during startup or due to network issues, it would remain disconnected indefinitely.
  - New config options: `directory_reconnect_interval` (default: 300s/5min) and `directory_reconnect_max_retries` (default: 0 = unlimited)
  - On successful reconnection, offers are automatically re-announced to the reconnected directory
  - Notifications are sent for both disconnections and successful reconnections

- **External Wallet Fidelity Bonds (Cold Storage Support)**: Added support for fidelity bonds with external wallet (hardware wallet/cold storage) private keys. The bond UTXO private key never needs to touch an internet-connected device. Instead, users create a certificate chain where the cold wallet signs a certificate authorizing a hot wallet keypair to sign nick proofs on its behalf.
  - New CLI commands:
    - `jm-wallet create-bond-address <pubkey>`: Create bond address from public key (no mnemonic needed)
    - `jm-wallet generate-hot-keypair`: Generate hot wallet keypair for certificate
    - `jm-wallet prepare-certificate-message`: Create message for hardware wallet signing
    - `jm-wallet import-certificate`: Import signed certificate into bond registry
  - Certificate chain: `UTXO keypair (cold) -> signs -> certificate (hot) -> signs -> nick proofs`
  - Security benefits: Bond funds remain completely safe in cold storage; certificate has configurable expiry (~2 years default); if hot wallet is compromised, only certificate is at risk
  - Compatible with hardware wallets via Sparrow Wallet message signing

- **Multi-Offer Support (Dual Offers)**: Makers can now advertise both relative and absolute fee offers simultaneously with different offer IDs. This allows makers to serve different types of takers (those preferring percentage-based fees vs fixed fees) from a single instance.
  - New `--dual-offers` CLI flag for `jm-maker start` creates both offer types automatically
  - Each offer type gets a unique offer ID (0 for relative, 1 for absolute)
  - !fill requests are routed to the correct offer based on the offer ID
  - Fidelity bond value is shared across all offers
  - Extensible architecture: `offer_configs` list in `MakerConfig` allows N offers (internal API, not yet exposed via CLI for simplicity)
  - Usage: `jm-maker start --dual-offers --cj-fee-relative 0.001 --cj-fee-absolute 500`

- **Wallet Import Command**: New `jm-wallet import` command to recover existing wallets from BIP39 mnemonic phrases. Features interactive word-by-word input with Tab completion (where readline is available), automatic word auto-completion when only one BIP39 word matches the prefix, suggestions display when multiple words match, mnemonic checksum validation after entry, and optional encryption of the saved wallet file. Supports 12, 15, 18, 21, and 24-word mnemonics.

### Fixed

- **Sweep Transaction Mining Fee Accuracy**: Fixed a bug where sweep transactions (taker with `amount=0`) would pay significantly higher mining fees than displayed at the start of the CoinJoin. The issue was caused by two problems:
  1. The `tx_fee_factor` randomization was applied when calculating the tx fee budget for sweep amount calculation, causing the budget to be up to 4x (with default `tx_fee_factor=3.0`) the base fee rate.
  2. At transaction build time, a new fee estimate with different randomization was used, creating a mismatch.

  With this fix:
  - Sweep fee budgets are calculated without randomization to ensure deterministic amounts
  - The same fee budget is used at both order selection and build time
  - The mining fee amount stays constant; only the effective fee rate may vary based on actual transaction size
  - Improved logging shows the tx fee budget, actual vsize, and effective fee rate

- **Log Level from Config/Env Ignored**: Fixed a bug where `LOGGING__LEVEL` environment variable and `[logging] level` config setting were ignored by CLI commands. The `--log-level` CLI argument worked correctly, but the env/config values were never applied because logging was configured before settings were loaded. Now the priority is: CLI argument > env/config > default ("INFO").

- **Maker cj_fee_absolute config setting ignored**: Fixed bug where setting `cj_fee_absolute` in `config.toml` had no effect because the maker always defaulted to relative fee offers. Added new `offer_type` setting to the `[maker]` config section that allows specifying which fee type to use: `sw0reloffer` (relative, default) or `sw0absoffer` (absolute). Previously, the only way to use absolute fees was via the `--cj-fee-absolute` CLI flag.

- **Install script missing python3-dev dependency**: Added `python3-dev` to the install script's dependency checks. This package is required for building Python C extensions (like the cryptography library used for wallet encryption). Previously, installations would fail when trying to install jmcore if this package was missing, and the script would exit before creating the activation script.

- **Tor cookie path auto-detection order**: Reordered the auto-detection paths for Tor cookie authentication to prioritize `/run/tor/control.authcookie` (common on Debian/Ubuntu with systemd) over `/var/lib/tor/control_auth_cookie`. Previously, the less common path was checked first, causing auto-detection to fail on most modern Linux systems.

- **Taker --fee-rate validation error with default fee_block_target**: Fixed bug where specifying `--fee-rate` on the CLI would fail with "Cannot specify both fee_rate and fee_block_target" error even when fee_block_target was not explicitly set. The issue was that `build_taker_config()` unconditionally fell back to `wallet.default_fee_block_target` (default: 3) even when `fee_rate` was provided. Now `fee_block_target` is only set when `fee_rate` is not provided.

- **Channel consistency check allows messages from different directory servers**: Fixed false positive channel consistency violations when taker messages arrived via different directory servers. The JoinMarket protocol broadcasts messages to ALL directory servers, so receiving `!auth` from `dir:serverA` after `!fill` from `dir:serverB` is expected behavior. The check now only validates that "direct" and "directory" channel types are not mixed, not the specific server identity.

- **Direct message parse failures now logged with content**: When the maker fails to parse a direct message, the log now includes a preview of the message content (truncated to 100 chars) to aid debugging. Previously only logged "Failed to parse direct message" with no indication of what was received.

- **Rate limiting for direct message parse failure warnings**: Parse failure warnings are now rate-limited (1 per 10 seconds per peer) to prevent log spam when receiving repeated malformed messages from the same peer.

- **Chunked PEERLIST responses**: Directory server now sends PEERLIST responses in chunks of 20 peers instead of a single massive message. This fixes timeout issues when receiving large peerlists over slow Tor connections. Previously, mainnet directories with hundreds of peers would frequently timeout because the entire peerlist had to be transmitted in one message. The client now accumulates peers from multiple PEERLIST messages, using a 5-second inter-chunk timeout to detect when all chunks have been received.

- **CoinJoin output destination address path**: Changed INTERNAL destination addresses to use internal chain (/1) instead of external chain (/0). This matches the reference implementation where all JoinMarket-generated addresses (CJ outputs and change) use the internal branch, while external (/0) is reserved for user-facing deposit addresses.

- **Fee rate randomization (tx_fee_factor)**: Changed from a simple multiplier (default 3.0x) to randomization like the reference implementation. Fees are now randomized between `base_fee` and `base_fee * (1 + tx_fee_factor)` for privacy. Default changed from 3.0 to 0.2 (20% randomization range). Set to 0 to disable randomization.

- **Fee rate resolution with mempool minimum**: Fee estimation now checks against mempool minimum fee and uses the higher value. Manual fee rates below mempool minimum trigger a warning and use mempool minimum instead. This prevents transactions from being rejected due to insufficient fee.

- **Interactive UTXO selection (--select-utxos) logging**: Improved logging for `--select-utxos` in sweep mode to better indicate whether UTXOs were manually selected or all UTXOs were used. This helps debug cases where the interactive selector might not appear.

### Improved

- **BIP39 Passphrase Documentation**: Expanded DOCS.md to clarify that `jm-wallet import` only stores the mnemonic without the BIP39 passphrase. The passphrase is provided when using the wallet (via `--bip39-passphrase`, `--prompt-bip39-passphrase`, or `BIP39_PASSPHRASE` env var).

- **Config Template Clarity**: Improved `config.toml.template` comments to:
  - Distinguish "coinjoin fees" (paid to makers) from "network/miner fees"
  - Document `fee_rate` option precedence over `fee_block_target`
  - Explain smart scan and background rescan behavior for wallet import

- **Orderbook watcher feature detection**: Fixed race condition where offers from new makers were stored with empty features before the peerlist response arrived. Now when peerlist response arrives with features, all cached offers for those makers are retroactively updated with the correct features.

- **Peer location updates now include features**: Fixed directory server to include peer features (neutrino_compat, peerlist_features) in peer location update messages sent after private message routing. Previously, when a client learned about a new peer through a PEERLIST update (not via explicit GETPEERLIST request), the features were missing. This caused orderbook watchers to miss feature information for makers discovered through private message routing.

- **Faster feature discovery for new makers**: Improved orderbook watcher feature discovery timing:
  - Added immediate feature discovery (30 seconds after startup) instead of waiting 10 minutes for first health check
  - Reduced initial health check delay from 10 minutes to 2 minutes
  - Added automatic feature discovery for makers without features after each peerlist refresh (every 5 minutes)
  - Direct health checks now populate features in directory client caches, ensuring offers are tagged with correct features

- **Feature merging across directories**: Fixed issue where maker features (neutrino_compat, peerlist_features) were being overwritten instead of merged when receiving updates from multiple directory sources. When a PEERLIST came from a reference directory (no features), it would overwrite features previously learned from an NG directory. Now features are properly merged: once we learn a feature for a nick, we keep it. This ensures the orderbook watcher and taker correctly detect maker capabilities regardless of which directory responds first.

- **Multiple offers per maker with same bond**: Fixed bond deduplication in orderbook watcher incorrectly dropping offers when a maker advertises multiple offer IDs (e.g., oid=0 and oid=1) backed by the same fidelity bond. Previously, only one offer was kept per bond UTXO. Now the deduplication key includes both the bond UTXO and offer ID, preserving all distinct offers from the same maker while still deduplicating when different nicks share the same bond (maker restart scenario).

- **Maker direct connection handshake support**: Makers now respond to handshake requests on direct connections (via their hidden service). This enables health checkers and feature discovery tools to connect directly to makers and discover their features (neutrino_compat, peerlist_features) without relying on directory server peerlists. Previously, direct connections only handled CoinJoin protocol messages (fill, auth, tx, push), causing health checks to time out and feature discovery to fail for NG makers.

- **Direct connection orderbook requests**: Makers now properly handle `!orderbook` requests received via direct connection (PUBMSG type 687). Previously, orderbook requests sent over direct connections were ignored with "Failed to parse direct message" warnings, because the maker only handled PRIVMSG (type 685) on direct connections. This was causing repeated warnings like `'{"type": 687, "line": "J5xxx!PUBLIC!orderbook"}'`. Now these requests are processed with the same rate limiting as directory-relayed requests.

- **Improved rate limiting and ban logging**: Added DEBUG/TRACE level logging throughout the rate limiter to help diagnose peer behavior:
  - TRACE: Logs each allowed request
  - DEBUG: Logs each rate-limited request with violation count, backoff level, and wait time
  - DEBUG: Logs when banned peer requests are rejected (with remaining ban time)
  - DEBUG: Logs when ban expires and peer state is reset
  - WARNING: Ban events now include the final backoff level for context

- **Improved PoDLE verification logging**: Added DEBUG/TRACE level logging for PoDLE proof verification to help diagnose authentication issues:
  - TRACE: Logs verification inputs (P, P2, sig, e, commitment - truncated)
  - DEBUG: Logs full PoDLE details on success (taker, utxo, commitment)
  - DEBUG: Logs detailed failure reasons including commitment/utxo info
  - DEBUG: Logs UTXO validation details (value, confirmations)
  - DEBUG: Logs specific rejection reasons (too young, too small)

- **Peer feature logging in handshake**: Makers now log advertised peer features (version, network, features) at DEBUG level when receiving handshake requests on direct connections. This helps diagnose feature negotiation and compatibility issues. Supports both reference implementation format (dict: `{"peerlist_features": true}`) and NG format (comma-string: `"neutrino_compat,peerlist_features"`).

- **Improved direct message parse failure logging**: Parse failures now log the full message content at DEBUG level (in addition to the rate-limited WARNING with truncated preview). This helps diagnose protocol issues without flooding logs.

## [0.10.0] - 2026-01-15

### Security

- **Sensitive data protection**: Refactored configuration models to use Pydantic's `SecretStr` type for sensitive fields (mnemonics, passphrases, passwords, destination addresses, notification URLs). This prevents accidental exposure of sensitive data in logs, error messages, and tracebacks. All sensitive values are automatically masked as `**********` in string representations and logging output, while remaining accessible via `.get_secret_value()` when needed.

### Fixed

- **Config file section headers**: Fixed config.toml.template to have all section headers (like `[bitcoin]`, `[tor]`, `[maker]`, etc.) uncommented by default. Previously, users would uncomment individual settings but forget to uncomment the section header, causing the settings to be silently ignored by the TOML parser. This led to confusion where config file settings appeared to be ignored even though they were correctly uncommented.
- **Config file error handling**: Improved error handling for malformed config.toml files. The application now exits immediately with exit code 1 and displays a clear error message when the config file has invalid TOML syntax (e.g., missing closing brackets, invalid characters). Previously, parsing errors were silently logged as warnings, and the application would continue with default values, making it difficult to diagnose configuration issues.
- **jm-directory-ctl config compliance**: Fixed `jm-directory-ctl status` and `jm-directory-ctl health` commands to respect `directory_server.health_check_host` and `directory_server.health_check_port` settings from config.toml. Previously, these commands always used hardcoded defaults (127.0.0.1:8080) and ignored the config file.
- **jm-wallet generate-bond-address config compliance**: Fixed `jm-wallet generate-bond-address` to respect `network_config.network` and `data_dir` settings from config.toml when CLI arguments are not provided. Previously, it always defaulted to mainnet and used hardcoded data directory logic.
- **jm-taker clear-ignored-makers config compliance**: Fixed `jm-taker clear-ignored-makers` to respect `data_dir` setting from config.toml when the `--data-dir` argument is not provided.
- **Orderbook watcher feature detection**: Fixed orderbook watcher to correctly identify JoinMarket NG makers' features (neutrino_compat, peerlist_features). Two issues resolved: (1) When new makers join after orderbook watcher startup, their features weren't being discovered until the next periodic peerlist refresh (5 minutes) or health check (15 minutes). Now the orderbook watcher immediately requests peerlist when discovering new peers to fetch their features. (2) Health checker now properly advertises peerlist_features support in its handshake to extract maker features, and merges these features with offers even when peerlist has already provided some features (health check provides authoritative confirmation via direct connection).
- **Taker pending transaction update on exit**: Fixed issue where taker CoinJoin transactions remained marked as `[PENDING]` in history after successful broadcast. The taker now immediately checks transaction status (mempool for full nodes, block confirmation for Neutrino) right after recording the history entry, before the CLI exits. Additionally, `jm-wallet info` now automatically updates the status of any pending transactions found in history, acting as a safeguard for transactions that confirm after the taker process has exited.
- **Spent address tracking in descriptor wallet**: Fixed issue where addresses that had been used but fully spent (zero balance) were not being tracked in `addresses_with_history`. The descriptor wallet backend now uses `listtransactions` RPC to fetch all addresses with any transaction history, ensuring the wallet correctly tracks which addresses have been used even if they no longer have UTXOs. This prevents address reuse and ensures `jm-wallet info` shows the correct next address.
- **Signature Ordering Mismatch**: Fixed critical bug where maker signatures were matched to the wrong transaction inputs, causing `OP_EQUALVERIFY` failures during broadcast. Root cause: signatures from the reference maker are sent in **transaction input order** (sorted by position in the serialized tx), not in the order UTXOs were originally provided in the `!ioauth` response. The taker now correctly matches signatures to transaction inputs by finding maker UTXOs in the actual transaction input order, rather than assuming they match the `!ioauth` order.
- **Slow Signature Processing**: Fixed 60-second delay between receiving signatures and processing them. Two issues: (1) For `!sig` responses (which expect multiple messages per maker), the loop condition `accumulate_responses and responses` kept waiting for the full timeout even after all signatures were received. Now uses `expected_counts` parameter to know when all signatures are collected. (2) Directory clients were polled sequentially, each waiting up to 5 seconds. Now polls all directories concurrently with `asyncio.gather()` using shorter 1-second chunks to allow more frequent checking of the direct message queue.
- **Sweep Mode CJ Amount Preservation**: Fixed critical bug where reference makers would reject sweep transactions with "wrong change". Root causes: (1) In sweep mode, the taker was recalculating `cj_amount` in `_phase_build_tx` when actual maker inputs differed from the initial estimate. Since makers calculate their expected change based on the original `cj_amount` from the `!fill` message, this recalculation caused a mismatch. (2) The initial tx_fee estimate used only 2 inputs per maker, which was insufficient when makers provided 6+ UTXOs, causing negative residual. The fix: (a) Preserve the original `cj_amount` sent in `!fill` - any tx_fee difference becomes additional miner fee (residual), (b) Use conservative tx_fee estimate (2 inputs/maker + 5 buffer) to minimize negative residual cases, (c) Fail gracefully with clear error when a maker provides many UTXOs causing negative residual (rare edge case).
- **Smart Message Routing**: Fixed `CryptError` with reference makers caused by duplicate `!fill` messages resetting session keys. Taker now intelligently routes messages via a single directory instead of broadcasting to all connected directories.
- **Session Channel Consistency**: Fixed critical protocol error where taker would mix communication channels (directory relay for `!fill`, direct connection for `!auth`) within a single CoinJoin session. This caused reference makers to reject messages as they appeared to be from different sessions. Taker now establishes ONE communication channel per maker before sending `!fill` and uses ONLY that channel for all subsequent messages (`!auth`, `!tx`, `!push`) in that session. Channel selection: tries direct connection first (5s timeout), falls back to directory relay if unavailable.
- **Directory Signature Verification**: Fixed `hostid` used for signing directory-relayed messages. Now correctly uses the fixed `"onion-network"` hostid (matching the reference implementation in `jmdaemon/onionmc.py`) instead of the directory's hostname. Previously, messages relayed through directories were signed with the wrong hostid, causing "nick signature verification failed" errors on reference makers.
- **Direct Peer Connection Message Signing**: Fixed message signing for direct peer-to-peer Tor connections. Messages sent via direct onion connections now include the required signature (pubkey + sig) that reference makers expect. Previously, direct connection messages were sent without signatures, causing reference makers to reject them with "Sig not properly appended to privmsg". The fix adds `nick_identity` parameter to `OnionPeer` and uses `ONION_HOSTID` ("onion-network") as the hostid for signing, matching the reference implementation's expectations.
- **Notification Configuration**: Fixed notification system to respect config file settings. Previously, notifications only read from environment variables (`NOTIFY_URLS`, etc.), completely ignoring the `[notifications]` section in `config.toml`. Now the notification system uses the unified settings system (config file + env vars + CLI args), with proper precedence: CLI args > environment variables > config file > defaults. All components (taker, maker, orderbook watcher, directory server) have been updated to pass settings to `get_notifier()`.
- **Fidelity Bond Verification**: Fixed a bug where fidelity bonds were parsed but not verified against the blockchain, causing their value to be 0. This prevented bond-weighted maker selection from working correctly, falling back to random selection. Taker now verifies bond UTXOs and calculates their value before maker selection.
- **Maker Selection Strategy**: Fixed maker selection to use deterministic mixed bonded/bondless strategy. The bondless allowance determines the proportion of maker slots using fair rounding: with 3 makers and 12.5% allowance, round(3 × 0.875) = 3 bonded slots. Bonded slots are filled by bond-weighted selection (prioritizing high-bond makers), while bondless slots are filled randomly from ALL remaining offers (both bonded and bondless makers, with equal probability). "Bondless" means bond-agnostic, not anti-bond. This ensures bonded makers are consistently rewarded while still supporting new/bondless makers. If insufficient bonded makers exist, remaining slots are filled from all available offers (optionally requiring zero-fee via `bondless_require_zero_fee` flag).
- **Orderbook Timeout**: Increased orderbook request timeout from 10s to 120s based on empirical testing. The previous timeout was missing ~75-80% of available offers. New timeout captures ~95% of offers (95th percentile response time is ~101s over Tor).
- **Peer-to-Peer Handshake Format**: Fixed message format for direct peer connections to use `{"type": 793, "line": "<json>"}` format, matching reference implementation (was using `{"type": 793, "data": {...}}`).
- **Maker Replacement Selection**: Fixed maker replacement to exclude makers already in the current session. Previously, a maker that already responded could be incorrectly re-selected as a replacement, causing commitment rejection errors.
- **Taker peerlist handling**: Fixed taker peerlist handling that was previously ignored. This way we start colelcting peer features and onion addresses earlier.
- **Minimum makers default**: Changed `minimum_makers` default from 2 to 1 (taker + 1 maker = 2 participants).
- **UTXO selection timing**: Moved UTXO selection (including interactive selector) before orderbook fetch to avoid wasting user time if they cancel.
- **Log verbosity**: Changed fee filtering logs from DEBUG to TRACE to reduce noise.
- **Ignored makers persistence**: Ignored makers list now persists across taker sessions in `~/.joinmarket-ng/ignored_makers.txt`. New CLI command `jm-taker clear-ignored-makers` to clear the list.
- **Blacklisted commitment handling**: Fixed taker to not permanently ignore makers who reject due to a blacklisted commitment. When a maker rejects a commitment as blacklisted, the taker now retries with a different commitment (different NUMS index or UTXO) instead of permanently ignoring that maker. The maker might accept a different commitment, so they should remain available for future attempts.
- **Self-broadcast fallback on already-spent inputs**: Fixed taker broadcast fallback to recognize when a maker has already successfully broadcast the CoinJoin transaction. When self-broadcast fails with "bad-txns-inputs-missingorspent" (UTXOs already spent) or similar errors, the taker now verifies if the CoinJoin transaction exists on-chain before reporting failure. This handles multi-node setups where the maker's broadcast propagates before the taker's verification can confirm it.
- **Wallet history status display**: Fixed `jm-wallet history` to show `[PENDING]` for unconfirmed transactions instead of incorrectly showing `[FAILED]`. Pending transactions (waiting for first confirmation) are now clearly distinguished from actually failed transactions.
- **Wallet info address display**: Fixed `jm-wallet info` to show the next address after the last used one (highest used index + 1) instead of the next unused address. This prevents showing index 0 when higher indexes have been used, making it clear which addresses have been utilized. The display now ignores gaps in the address sequence and always shows the address immediately following the highest used index, considering all usage sources (blockchain history, current UTXOs, and CoinJoin history).

### Added

- **Centralized Version Management**: Introduced a single source of truth for project versioning in `jmcore/src/jmcore/version.py`. All components now import their `__version__` from this central location, ensuring consistency across the project. The version is also accessible via `jmcore.VERSION`, `jmcore.get_version()`, and `jmcore.get_version_info()`.
- **Directory Server Version in MOTD**: Directory servers now advertise the JoinMarket NG version in their MOTD (Message of the Day), similar to the reference implementation. The format is: `JOINMARKET VERSION: X.Y.Z`. This helps clients identify the server software version.
- **Version Bump Script**: New `scripts/bump_version.py` automates the release process by updating all version files, preparing the changelog (adding version header and date, preserving Unreleased section, adding diff link), updating `install.sh`, creating a git commit with a standard message (`release: X.Y.Z`), and tagging. Usage: `python scripts/bump_version.py 0.10.0 --push`
- **Orderbook watcher directory metadata display**: The orderbook watcher web UI now displays directory server metadata including MOTD (message of the day), protocol version (e.g., v5 or v5-6), and supported features (e.g., neutrino_compat, peerlist_features). This information appears in the "Offers per Directory Node" section, helping users understand the capabilities and configuration of each directory server.
- **Interactive UTXO Selection for Taker**: Added `--select-utxos` / `-s` flag to `jm-taker coinjoin` command, enabling interactive UTXO selection before CoinJoin execution. Uses the same fzf-like TUI as `jm-wallet send`, allowing users to manually choose which UTXOs to include in the CoinJoin transaction. Works with both sweep mode and normal CoinJoin mode.
- **Orderbook Response Measurement Tool**: New `scripts/measure_orderbook_delays.py` tool to measure response time distribution when requesting orderbooks from directory servers over Tor. Helps validate timeout settings empirically.
- **Direct Peer Connections**: Taker can now establish direct Tor connections to makers, bypassing directory servers for private message exchange.
  - Improves privacy by preventing directories from observing who is communicating with whom
  - Attempts to establish direct connections before sending `!fill` (5s timeout, no added latency if unavailable)
  - Once a channel is chosen (direct or directory), ALL messages to that maker use the same channel
  - Automatic fallback to directory relay if direct connection fails
  - Connection attempts use exponential backoff to avoid overwhelming peers
  - Enabled by default (`prefer_direct_connections=True` in `MultiDirectoryClient`)
  - New `OnionPeer` class in `jmcore.network` handles direct peer connection lifecycle

- **Maker Replacement on Non-Response**: Taker now automatically replaces non-responsive makers during CoinJoin.
  - New config option: `max_maker_replacement_attempts` (default: 3, range: 0-10)
  - If makers fail to respond during fill or auth phases, taker selects replacements from orderbook
  - Failed makers are added to an ignored list to prevent re-selection
  - Replacement makers go through the full handshake (fill + auth phases)
  - Setting to 0 disables replacement (original behavior: fail immediately)
  - Improves CoinJoin success rate when some makers are unresponsive or drop out

- **Simplified Installation**: New one-line installation with automatic updates.
  - Install: `curl -sSL https://raw.githubusercontent.com/joinmarket-ng/joinmarket-ng/main/install.sh | bash`
  - Update: `curl -sSL ... | bash -s -- --update`
  - Installs from tagged releases via pip (no git clone required)
  - Creates shell integration at `~/.joinmarket-ng/activate.sh`
  - Unified install/update mode with automatic detection of existing installations

- **Configuration File Support**: Added TOML configuration file (`~/.joinmarket-ng/config.toml`) for persistent settings.
  - Configuration priority: CLI args > environment variables > config file > defaults
  - Auto-generated template with all settings commented out on first run
  - Users only uncomment settings they want to change, facilitating software updates
  - New `config-init` command for maker and taker to initialize the config file
  - Unified settings model in `jmcore.settings` using pydantic-settings

- **Interactive UTXO Selection TUI**: New `--select-utxos` / `-s` flag for `jm-wallet send` command.
  - fzf-like curses interface for manually selecting UTXOs
  - Navigate with arrow keys or j/k, toggle selection with Tab/Space
  - Shows mixdepth, amount (sats and BTC), confirmations, and outpoint
  - Visual indicators for timelocked fidelity bond UTXOs
  - Real-time display of selected total vs target amount
  - Keyboard shortcuts: a (select all), n (deselect all), g/G (top/bottom)

### Changed

- **Renamed `full_node` backend to `scantxoutset`** for clarity. The backend type has been renamed to better reflect what it does (uses Bitcoin Core's `scantxoutset` RPC to scan the UTXO set). This is an alternative backend that should not be recommended for general usage - `descriptor_wallet` is the preferred default for full nodes. Updated all documentation to reflect this change and removed examples about the `scantxoutset` backend from tutorials.
- **Environment Variable Naming Standardization**: Standardized environment variable naming to use double underscore (`__`) for nested settings, following pydantic-settings convention.
  - Old format: `TOR_SOCKS_HOST`, `NOTIFY_URLS`
  - New format: `TOR__SOCKS_HOST`, `NOTIFICATIONS__URLS`
  - Consolidated `TorSettings` and `TorControlSettings` into a single `TorSettings` model
  - Tor control settings now use `TOR__CONTROL_ENABLED`, `TOR__CONTROL_HOST`, `TOR__CONTROL_PORT`, `TOR__COOKIE_PATH`
  - Updated all Docker Compose files to use the new format
  - Config template no longer shows separate `[tor_control]` section (now part of `[tor]`)
- **Installation path**: Virtual environment now lives at `~/.joinmarket-ng/venv/` (was `jmvenv/` in repo)
- **Documentation**: Updated all READMEs to use config file approach instead of .env files
- **Directory connections now parallel**: Taker and orderbook watcher connect to all directory servers concurrently instead of sequentially.
  - Significantly reduces startup time when connecting to multiple directories (especially over Tor).
  - Directory orderbook fetching is also parallelized.
- **Removed peerlist-based offer filtering**: Directory's orderbook is now trusted as authoritative.
  - If a maker has an offer in the directory, they are considered online.
  - Peerlist responses may be delayed or unavailable over Tor, so offers are no longer filtered based on peerlist presence.
  - This prevents incorrectly rejecting valid offers from active makers.
- **Enhanced CoinJoin routing visibility**: Taker now logs detailed message routing information during CoinJoin.
  - Shows which directory servers are used to relay messages to makers.
  - Displays maker onion addresses in the transaction confirmation prompt.
  - Debug logs show routing details for !fill, !auth, !tx, and !push messages.
  - Indicates whether messages are sent via direct connection or directory relay.

## Fixed

- **Wallet Info Shows Next Unused Address**: The `jm-wallet info` command now displays the first unused address (next index after highest used) instead of always showing index 0. This allows users to quickly grab an address for depositing without manual derivation path lookups.
- **Address reuse after internal send**: Fixed address reuse bug where `get_next_address_index` would return an already-used address index after funds were spent.
  - Now properly considers `addresses_with_history` (addresses that ever had UTXOs, including spent ones).
  - Always returns the next index after the highest used, never reusing lower indices even if they appear empty.
  - Prevents privacy leaks from address reuse after internal sends or CoinJoins.
- **Signature base64 padding error**: Fixed "Incorrect padding" errors when decoding maker signatures.
  - Base64 strings without proper padding are now handled correctly.
- **PoDLE commitment blacklist retry**: Taker now automatically retries with a new NUMS index when a maker rejects due to blacklisted commitment.
  - Previously, a blacklisted commitment would cause the entire CoinJoin to fail.
  - Now retries up to `taker_utxo_retries` times (default 3) with different commitment indices.

## [0.9.0] - 2026-01-12

### Added

- **Descriptor Wallet Backend now exposed via CLI**: Users can now select `--backend descriptor_wallet` for fast UTXO tracking.
  - Available in all CLIs: `jm-wallet`, `jm-maker`, `jm-taker`
  - Uses Bitcoin Core's `importdescriptors` for one-time wallet setup
  - Fast syncs via `listunspent` (~1s vs ~90s for scantxoutset)
  - Automatic descriptor import and wallet setup on first use
  - **New default backend** for maker, taker, and wallet commands (changed from `full_node`)
  - Docker compose examples updated to use `descriptor_wallet` by default
- **Orderbook Watcher: Maker direct reachability tracking**.
  - Each offer now includes `directly_reachable` field (true/false/null) showing if maker is reachable via direct Tor connection.
  - Health checker extracts maker features from handshake, useful when directory servers don't provide peerlist features.
  - Reachability info available in orderbook.json API response for monitoring and debugging.
  - Note: Unreachable makers are NOT removed from orderbook - directory may still have valid connection.
- **Operator Notifications**: Push notification system via Apprise for CoinJoin events.
  - Supports 100+ notification services (Gotify, Telegram, Discord, Pushover, email, etc.)
  - Privacy-aware: configurable amount/txid/nick inclusion
  - Per-event toggles for fine-grained control
  - Fire-and-forget: notifications never block protocol operations
  - Components integrated: Maker, Taker, Directory Server, Orderbook Watcher
  - Docker images now include `apprise` by default for notification support
- **DescriptorWalletBackend**: New Bitcoin Core backend using descriptor wallets for efficient UTXO tracking.
  - Uses `importdescriptors` RPC for one-time wallet setup
  - Uses `listunspent` RPC for fast UTXO queries (O(wallet) vs O(UTXO set))
  - Persistent tracking: Bitcoin Core maintains UTXO state automatically
  - Real-time mempool awareness: sees unconfirmed transactions immediately
  - Deterministic wallet naming based on mnemonic fingerprint
- `setup_descriptor_wallet()` method in WalletService for one-time descriptor import
- `sync_with_descriptor_wallet()` method for fast wallet sync via listunspent
- Helper functions `generate_wallet_name()` and `get_mnemonic_fingerprint()` for deterministic wallet naming
- Early backend connection validation in taker CLI before wallet sync.
- Estimated transaction fee logging before user confirmation prompt (assumes 1 input per maker + 20% buffer).
- Final transaction summary before broadcast with exact input/output counts, maker fees, and mining fees.
- Support for broadcast confirmation callback to allow user to review transaction before broadcasting.
- `has_mempool_access()` method to BlockchainBackend for detecting mempool visibility.
- `BroadcastPolicy.MULTIPLE_PEERS` - new broadcast policy that sends to N random makers (default 3).
- `broadcast_peer_count` configuration parameter to control number of peers for MULTIPLE_PEERS policy.
- Unified broadcast behavior between full node and Neutrino clients.
- Comprehensive backend comparison documentation in jmwallet README with performance characteristics and use cases.
- **Smart Scan for Descriptor Wallet**: Fast startup for descriptor wallet import on mainnet.
  - Initial import only scans ~1 year of blockchain history (52,560 blocks)
  - Reduces first-time wallet sync from 20+ minutes to seconds on mainnet
  - Background full rescan runs automatically to ensure no old transactions are missed
  - Configurable via `smart_scan`, `background_full_rescan`, `scan_lookback_blocks` in WalletConfig

### Changed

- **Default backend changed from `scantxoutset` to `descriptor_wallet`** for all components (maker, taker, wallet CLI).
  - Scantxoutset (formerly `full_node`) still available via `--backend scantxoutset`
  - Provides significant performance improvement for ongoing operations (~1s vs ~90s per sync)
  - Docker compose examples updated to use descriptor_wallet by default
- Fee rate handling improvements:
  - Changed default fee rate from 10 sat/vB to 1 sat/vB fallback.
  - Added support for sub-1 sat/vB fee rates (float instead of int).
  - Added `--block-target` option for fee estimation (1-1008 blocks).
  - Added `--fee-rate` option for manual fee rate (mutually exclusive with `--block-target`).
  - Default behavior: 3-block fee estimation when connected to full node.
  - Neutrino backend: falls back to 1 sat/vB (cannot estimate fees).
  - Error when `--block-target` is used with neutrino backend.
- Backend `estimate_fee()` now returns `float` for precision with sub-sat rates.
- Added `can_estimate_fee()` method to backends for capability detection.
- Increased default counterparty count from 3 to 10 makers.
- Reduced logging verbosity: parsed offers, fidelity bond creation, and Neutrino operations now logged at DEBUG level.
- Improved sweep coinjoin logging: initial "Starting CoinJoin" message now shows "ALL (sweep)" instead of "0 sats".
- **Default broadcast policy changed from RANDOM_PEER to MULTIPLE_PEERS** (sends to 3 random makers).
- **Unified broadcast behavior**: All policies (SELF, RANDOM_PEER, MULTIPLE_PEERS, NOT_SELF) work
  the same way for both full node and Neutrino backends. The only difference is Neutrino skips
  mempool verification when falling back to self-broadcast.
- RANDOM_PEER and MULTIPLE_PEERS now allow self-fallback if all makers fail (both full node and Neutrino).
- Neutrino pending transaction timeout reduced from 48h to 10h before warning.
- Neutrino pending transaction monitoring uses block-based UTXO verification (cannot access mempool).
- Neutrino backend UTXO detection improved with incremental rescans and retries for better robustness.

### Fixed

- **Taker failing when Maker uses multiple UTXOs**: Fixed handling of multiple `!sig` messages from makers with multiple inputs.
- **Orderbook Watcher peerlist timeout with JoinMarket NG directories**: Fixed incorrect timeout handling when directory announces `peerlist_features` during handshake.
  - Directories announcing `peerlist_features` now use a longer timeout (120s vs 30s) for peerlist requests over Tor.
  - Timeout on directories with `peerlist_features` no longer permanently disables peerlist requests (the peerlist may simply be large and slow to transmit).
  - Improved log messages to distinguish between "likely reference implementation" timeouts and "large peerlist or slow network" timeouts.
- **Orderbook Watcher bond deduplication logging noise**: Fixed false "stale offer replacement" logs when the same offer from the same maker was seen from multiple directories.
  - Same (nick, oid) pairs are now silently deduplicated instead of logging as "stale replacement".
  - Only logs when an actual different maker reuses the same bond UTXO (e.g., after nick restart).
- **Orderbook Watcher aggressive offer pruning**: Fixed overly aggressive cleanup that was removing valid offers.
  - **Removed age-based staleness cleanup entirely** - makers can run for months, offer age is not a valid signal.
  - Maker health check no longer removes offers from makers that are unreachable via direct connection (directory may still have valid connection).
  - Peerlist-based cleanup now skips if any directory refresh fails (avoids false positives).
  - Philosophy changed to **"show offers when in doubt"** rather than aggressive pruning.
  - Only removes offers when explicitly signaled by directory (`;D` disconnect marker or nick absent from ALL directories' peerlists).
- **Orderbook Watcher showing only few offers despite receiving many from directories**.
  - Directory servers send realtime PEERLIST updates (one per peer) when peers connect/disconnect.
  - DirectoryClient was incorrectly treating these partial updates as complete peerlist replacements.
  - Now accumulates active peers from partial responses instead of replacing the entire list.
  - Only removes offers for nicks explicitly marked as disconnected (`;D` suffix).
  - Periodic peerlist refresh now collects active nicks from ALL directories before cleanup.
  - This fixes orderbooks being pruned down to just the most recently seen makers.
- Critical maker transaction fee calculation bug causing "Change output value too low" errors.
  - Maker `txfee` from offers is the total transaction fee contribution (in satoshis), not per-input/output.
  - Previously incorrectly multiplied `offer.txfee` by `(num_inputs + num_outputs + 1)`, causing maker change calculations to fail.
  - Now correctly uses `offer.txfee` directly as per JoinMarket protocol specification.
- Concurrent read bug in TCPConnection causing "readuntil() called while another coroutine is already waiting" errors.
  - Added receive lock to serialize concurrent `receive()` calls on the same connection.
  - This fixes race conditions when `listen_continuously()` and `get_peerlist_with_features()` run concurrently.
- Wallet address alignment in `jm-wallet info --extended` output.
  - Fixed misalignment when address indices transition from single to double digits (e.g., 9 to 10).
  - Derivation paths now use fixed-width padding (24 characters) for consistent column alignment.

## [0.8.0] - 2026-01-08

### Added

- Support for multiple directory servers with message deduplication.
- Maker health checking via direct onion connection.
- BIP39 passphrase support for wallets (CLI and component integration).
- BIP84 zpub support for native SegWit wallets.
- Auto-discovery for fidelity bonds and timenumber utilities.
- Configuration for separate Tor hidden service targets (split onion serving host).
- Tests for BIP39 passphrase and multi-directory functionality.

### Fixed

- Flaky E2E tests regarding taker commitment clearing and neutrino blacklist resetting.
- Detection of peer count after CoinJoin confirmation in Maker bot.

## [0.7.0] - 2026-01-03

### Added

- Generic per-peer rate limiting across all components.
- Specific rate limiting for orderbook requests to prevent spam.
- Fidelity bond proof compatibility and analysis tool.
- Exponential backoff and banning for orderbook rate limiter.
- Docker multi-architecture builds (ARM support).
- Periodic directory connection status logging.
- `INSTALL.md` with detailed installation instructions.
- Support for `MNEMONIC_FILE` environment variable.
- SimpleX community link to README.

### Changed

- Unified data directory to `~/.joinmarket-ng`.
- Improved Dockerfile efficiency with multi-stage builds.
- Moved to `prek` action for CI.
- Renamed project title to JoinMarket NG in documentation and orderbook watcher.

### Fixed

- Linking of standalone fidelity bonds to offers in Orderbook Watcher.
- Maker orderbook rate limit logging.
- Docker layer caching for ARM builds.

## [0.6.0] - 2025-12-28

### Added

- Persistence for PoDLE commitment blacklist.
- Tracking of CoinJoin transaction confirmations in wallet history.
- Stale offer filtering.
- UTXO max PoDLE retries for makers.
- Advanced UTXO selection strategies for takers and makers.
- Configurable dust threshold for CoinJoin transactions.
- Periodic wallet rescan.
- CoinJoin notifier script.

### Changed

- Redesigned dependency management.
- Moved `CommitmentBlacklist` to `jmcore`.
- Moved to integer satoshi amounts for Bitcoin values to avoid float issues.

### Fixed

- Maker change calculation bug causing negative change.
- Directory server message routing concurrency.
- Fee estimation and Bitcoin units display format.
- Maker sending fidelity bonds via PRIVMSG.

## [0.5.0] - 2025-12-21

### Added

- Protocol v5 extension feature for Neutrino support.
- Feature negotiation via handshake (`neutrino_compat`).
- Push broadcast policy for taker.
- Auto-miner for regtest in Docker Compose.
- Mnemonic generation, encryption, and fidelity bond generation.
- JSON-line message parsing limits to prevent DoS.
- Support for Tor ephemeral hidden services and Cookie Auth.

### Changed

- Migrated from `cryptography` to `coincurve` for ECDSA operations.
- Adopted feature flags instead of strict protocol version bumps.
- Consolidated documentation into `DOCS.md`.

### Fixed

- Taker fee limit checks.
- Fidelity bond proof verification and generation.
- Reference implementation compatibility.

## [0.4.0] - 2025-12-14

### Added

- Complete Maker Bot implementation with fidelity bonds and signing.
- Taker implementation with input signing.
- Neutrino backend integration.
- `AGENTS.md` for AI agents documentation.
- Comprehensive E2E tests with Docker Compose.

### Changed

- CI workflow to always run all tests.
- Updated READMEs for components.

### Fixed

- Blockchain height consistency in E2E tests.
- GitHub Actions workflow to start Bitcoin Regtest node properly.

## [0.3.0] - 2025-12-07

### Added

- Health check and monitoring features to Directory Server.
- Fidelity bond offer counts to directory stats.
- Docker health check for directory server.
- Debug Docker image with `pdbpp` and `memray`.

### Changed

- Increased max message size to 2MB.
- Increased max peers limit to 10000.
- Set log level to INFO in docker-compose files.

### Fixed

- Orderbook Watcher clean shutdown on SIGTERM/SIGINT.
- Directory Server file-based logging removal.
- Handling of failed peer mappings on send failures.

## [0.2.0] - 2025-11-20

### Added

- Orderbook Watcher component.
- Healthcheck to Orderbook Watcher service.
- Directory node connection status tracking.
- Auto-remove stale offers from inactive counterparties.
- Tor hidden service support for mempool.space integration.

### Fixed

- "Unexpected response type: 687" error.
- Fidelity bond handling for new offers.
- Orderbook request logic improvements.
- Connection handling and UI status indicators.

## [0.1.0] - 2025-11-16

### Added

- Initial project structure.
- Directory Server implementation with Peer Types and Monitoring.
- Basic README and Docker setup.
- Pre-built image support for directory server compose.
- Tor configuration instructions.

[Unreleased]: ../../compare/0.34.0...HEAD
[0.34.0]: ../../compare/0.33.0...0.34.0
[0.33.0]: ../../compare/0.32.0...0.33.0
[0.32.0]: ../../compare/0.31.1...0.32.0
[0.31.1]: ../../compare/0.31.0...0.31.1
[0.31.0]: ../../compare/0.30.0...0.31.0
[0.30.0]: ../../compare/0.29.0...0.30.0
[0.29.0]: ../../compare/0.28.1...0.29.0
[0.28.1]: ../../compare/0.28.0...0.28.1
[0.28.0]: ../../compare/0.27.0...0.28.0
[0.27.0]: ../../compare/0.26.1...0.27.0
[0.26.1]: ../../compare/0.26.0...0.26.1
[0.26.0]: ../../compare/0.25.0...0.26.0
[0.25.0]: ../../compare/0.24.0...0.25.0
[0.24.0]: ../../compare/0.23.1...0.24.0
[0.23.1]: ../../compare/0.23.0...0.23.1
[0.23.0]: ../../compare/0.22.0...0.23.0
[0.22.0]: ../../compare/0.21.0...0.22.0
[0.21.0]: ../../compare/0.20.0...0.21.0
[0.20.0]: ../../compare/0.19.3...0.20.0
[0.19.3]: ../../compare/0.19.2...0.19.3
[0.19.2]: ../../compare/0.19.1...0.19.2
[0.19.1]: ../../compare/0.19.0...0.19.1
[0.19.0]: ../../compare/0.18.0...0.19.0
[0.18.0]: ../../compare/0.17.0...0.18.0
[0.17.0]: ../../compare/0.16.0...0.17.0
[0.16.0]: ../../compare/0.15.0...0.16.0
[0.15.0]: ../../compare/0.14.0...0.15.0
[0.14.0]: ../../compare/0.13.12...0.14.0
[0.13.12]: ../../compare/0.13.11...0.13.12
[0.13.11]: ../../compare/0.13.10...0.13.11
[0.13.10]: ../../compare/0.13.9...0.13.10
[0.13.9]: ../../compare/0.13.8...0.13.9
[0.13.8]: ../../compare/0.13.7...0.13.8
[0.13.7]: ../../compare/0.13.6...0.13.7
[0.13.6]: ../../compare/0.13.5...0.13.6
[0.13.5]: ../../compare/0.13.4...0.13.5
[0.13.4]: ../../compare/0.13.3...0.13.4
[0.13.3]: ../../compare/0.13.2...0.13.3
[0.13.2]: ../../compare/0.13.1...0.13.2
[0.13.1]: ../../compare/0.13.0...0.13.1
[0.13.0]: ../../compare/0.11.6...0.13.0
[0.11.6]: ../../compare/0.11.5...0.11.6
[0.11.5]: ../../compare/0.11.4...0.11.5
[0.11.4]: ../../compare/0.11.3...0.11.4
[0.11.3]: ../../compare/0.11.2...0.11.3
[0.11.2]: ../../compare/0.11.1...0.11.2
[0.11.0]: ../../compare/0.10.0...0.11.0
[0.10.0]: ../../compare/0.9.0...0.10.0
[0.9.0]: ../../compare/0.8.0...0.9.0
[0.8.0]: ../../compare/0.7.0...0.8.0
[0.7.0]: ../../compare/0.6.0...0.7.0
[0.6.0]: ../../compare/0.5.0...0.6.0
[0.5.0]: ../../compare/0.4.0...0.5.0
[0.4.0]: ../../compare/0.3.0...0.4.0
[0.3.0]: ../../compare/0.2.0...0.3.0
[0.2.0]: ../../compare/0.1.0...0.2.0
[0.1.0]: ../../releases/tag/0.1.0
