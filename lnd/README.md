# Channel escrow LND backend (experimental)

Reproducible packaging of the `ChannelEscrow` patch series for LND
`v0.21.3-beta`. It provides the single, exact-parent escrow backend that a
channel buyout needs: quiesce a channel, then privately cosign one specific
parent transaction that spends the channel funding output.

This directory contains only the patches, the pinned build, and this note. It
adds no RPC or Go logic of its own.

## Pinned base

- Tag: `v0.21.3-beta`
- Annotated tag object: `84d2392c15355b986ca1ef565029f48d6aebce44`
- Release commit (build base): `572b561bf05f03dfe6135110970c4d858c3482dc`
- Go toolchain: `1.26.6` (the `GO_VERSION` of that commit's `Makefile`;
  `go.mod` requires at least `go 1.25.13`)

Patches in `patches/`, applied in file name order:

1. `0001-feat-channelescrowrpc-add-exact-parent-channel-escro.patch`: the
   `ChannelEscrow` subserver, its durable channel database records, and the
   force-close exception for escrow-locked channels.
2. `0002-fix-htlcswitch-resolve-late-local-quiescence-request.patch`: an
   independent quiescence fix the buyout flow depends on.

Both are `git format-patch` output. The image applies them with `git apply`.

## Build and verify

```sh
# From this directory. Never reuse the lnd-channelescrow image name.
docker build -t jm-buyout-lnd:v0.21.3-beta .

# Or, against a throwaway checkout:
git clone https://github.com/lightningnetwork/lnd.git /tmp/lnd-check
git -C /tmp/lnd-check checkout 572b561bf05f03dfe6135110970c4d858c3482dc
git -C /tmp/lnd-check apply /path/to/lnd/patches/*.patch
```

The image build runs the focused tests before installing the binaries:
`go test -tags=channelescrowrpc ./lnrpc/channelescrowrpc/...`,
`go test -run TestEscrow ./channeldb` and `go test -run TestQuiesc
./htlcswitch`. The subserver only exists when the `channelescrowrpc` build tag
is set; patch 1 adds that tag to `RELEASE_TAGS`, so `make release-install`
picks it up.

Because the patches are applied to the working tree and not committed, the
built binary reports `v0.21.3-beta-dirty`.

## Scope and limitations

- Exactly one parent transaction per escrow session. There is no multi-parent
  support: `PrepareParent` binds a session to a single raw parent and its input
  index, and a different parent requires `Cancel` plus a new session.
- The RPC surface (`FreezeChannel`, `PrepareParent`, `BeginSigning`,
  `SignParent`, `FinalizeParent`, `Status`, `Cancel`) is the signing backend
  only. The buyout lifecycle (peer negotiation, funding, CoinJoin
  construction, payout, abort handling) lives outside this directory and is not
  described here.
- Experimental. The patches are not upstream, the wire and database formats can
  change, and the build is pinned to one release commit.
- No migration and no reset of existing state. Escrow records are stored per
  channel inside the existing channel bucket and are written only when a
  session runs, so an existing `channel.db` keeps working unchanged and an
  absent escrow record simply means no session was started. Nothing in this
   packaging rewrites, rebuilds, or clears journals or channel state on startup,
   and downgrading to a stock `v0.21.3-beta` binary leaves unrelated channel
   data intact.
- Cancellation writes an exact-session receipt atomically with releasing the
  escrow lock. A retry after a lost RPC response succeeds only if that receipt
  matches and no newer escrow owns the channel. Upgrading does not backfill
  receipts: a channel canceled by an older build remains unknown on retry.
  Missing receipts never trigger a release, scan, or migration.
- An explicit force close remains available while an escrow session locks the
  channel, including after signing and restarting LND. Cancellation still refuses
  to release a signed session. The force close spends the funding output through
  LND's normal commitment recovery; it does not broadcast the escrow parent.
  Upgrading does not initiate any closes or alter existing escrow records.
