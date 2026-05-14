# Security

This page summarizes practical security properties and controls. For the
full adversary model and per-threat mitigations, see
[Threat Model](threat-model.md).

## Threat Model (High Level)

Primary adversaries:

- malicious peers
- network observers
- malicious or degraded infrastructure nodes

Primary goals:

- protect funds from unauthorized signing
- reduce linkability between participant activity
- preserve availability under spam/DoS pressure

## Core Controls

- transaction verification before maker signing (see
  [Maker Verification Checklist](maker-verification-checklist.md))
- PoDLE anti-abuse commitments
- Tor-based transport and hidden-service support
- rate limiting and message validation in directory/maker paths
- fidelity bond weighting as Sybil-cost mechanism

## Directory and Messaging

- use multiple directory servers where possible
- prefer direct maker/taker channels when available
- enforce channel/session consistency during CoinJoin flow

## Neutrino Notes

- neutrino is convenient, but full-node backends remain the strongest default for verification and compatibility
- run neutrino infrastructure you trust and route traffic with Tor where possible
- neutrino-api supports TLS with certificate pinning and bearer-token authentication, enabled by default; see [Neutrino TLS](neutrino-tls.md) for setup details
- the TLS certificate is self-signed and pinned on first use (TOFU model), so only the specific neutrino-api instance that generated it is trusted

## Operational Advice

- treat mnemonics and wallet files as high-value secrets
- keep software updated
- test operational setup on testnet/signet/regtest before production use

### Process Memory Hardening

Each long-running daemon (jmwalletd, maker, taker, directory server,
orderbook watcher) calls `jmcore.process_hardening.harden_current_process`
at startup. This applies two cheap, OS-level mitigations on Linux to keep
secrets (mnemonic, BIP32 extended keys, derived private keys, NaCl session
keys, signed PSBTs) from leaking on crash or live introspection:

- `RLIMIT_CORE = 0` disables core dumps for the process, preventing tools
  like `systemd-coredump(8)` from writing the address space to disk
- `prctl(PR_SET_DUMPABLE, 0)` blocks non-privileged `ptrace` and
  `/proc/$pid/mem` reads from peer processes in the same user namespace

Set `JOINMARKET_DISABLE_PROCESS_HARDENING=1` to opt out (only useful when
debugging with gdb or rr).

These mitigations do not protect anonymous pages that get paged out to
swap or written to a hibernation image. Operators who hold non-trivial
funds should also:

- enable encrypted swap (for example LUKS-backed swap with a random key
  on each boot, or zram-swap, so paged-out pages never persist plaintext)
- disable hibernation, or back the hibernation partition with the same
  encrypted volume as `/`
- avoid running daemons on systems with `kernel.yama.ptrace_scope = 0`
  (Ubuntu's default is `1`, which is fine; RHEL-style systems should
  set this in `/etc/sysctl.d/`)
- prefer running daemons under a dedicated unprivileged user account
  and, where possible, a `systemd` unit with
  `ProtectKernelTunables=`, `ProtectControlGroups=`,
  `PrivateTmp=`, and `NoNewPrivileges=`

For protocol-level details, see [Protocol](protocol.md) and [Privacy](privacy.md).
For the maker-side pre-sign checklist, see
[Maker Verification Checklist](maker-verification-checklist.md).
