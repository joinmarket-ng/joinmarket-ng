# Channel escrow LND backend (experimental)

Reproducible packaging of the authorized `ChannelEscrow` patch for LND
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

The single patch in `patches/` contains the `ChannelEscrow` subserver,
per-channel authorizers, durable recovery records, the force-close exception
for escrow-locked channels, and the late-quiescence fix needed by buyouts.
It is `git format-patch` output, applied by the image with `git apply`.

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
is set; the patch adds that tag to `RELEASE_TAGS`, so `make release-install`
picks it up.

Because the patches are applied to the working tree and not committed, the
built binary reports `v0.21.3-beta-dirty`.

## Scope and limitations

- Exactly one parent transaction per escrow session. There is no multi-parent
  support: `PrepareParent` binds a session to a single raw parent and its input
  index, and a different parent requires `Cancel` plus a new session.
- The RPC surface (`FreezeChannel`, `PrepareParent`, `BeginSigning`,
  `SignParent`, `FinalizeParent`, `Status`, `Cancel`, plus the authorization
  RPCs below) is the signing backend only. The buyout lifecycle (peer negotiation, funding, CoinJoin
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
  matches and no newer escrow owns the channel.
  Missing receipts never trigger a release, scan, or migration.
- An explicit force close remains available while an escrow session locks the
  channel, including after signing and restarting LND. Cancellation still refuses
  to release a signed session. The force close spends the funding output through
  LND's normal commitment recovery; it does not broadcast the escrow parent.
  Upgrading does not initiate any closes or alter existing escrow records.


## Escrow authorization

`PrepareParent` checks the channel input, while a per-channel authorizer must
approve the exact parent and its outputs before a new signature is released:

- `AuthorizeChannel(channel_point, authorizer_pubkey)` enrolls a BIP340 x-only
  key for one channel. It needs `offchain:write`, the permission that already
  opens and closes channels, and not a `channelescrow` permission. The escrow
  macaroon alone cannot enroll a key.
- `FreezeChannel` refuses a channel without an enrolled key, before quiescing
  it. `SignParent` releases a new partial signature only with a valid
  `authorizer_signature` over the exact parent, its prevouts, the channel input
  index, the session, the funding outpoint, the node identity and the chain.
  A partial that is already durable is returned without one, so recovery of a
  signed session never needs the authorizer.
- Re-enrolling the same key is idempotent. Changing or revoking it is refused
  while any escrow record exists for the channel.
- `PolicyInfo` (`channelescrow:read`) reports policy version 1. Clients refuse
  to freeze against a node that does not report it.

In JoinMarket NG the authorizer is derived from the wallet seed (one key per
wallet and network, never a spending key). The ring maker and taker enroll
each ring channel when its funding confirms. Retry interrupted enrollment
with `jm-maker authorize-ring-channels`.

Macaroons:

- `channelescrow.macaroon` can freeze, prepare, sign and finalize, but cannot
  enroll a key, so it can no longer move funds on its own. Keep it private all
  the same.
- `admin.macaroon` has no `channelescrow` permissions, so it cannot sign, but
  it can enroll a key. Whoever holds both it and the escrow macaroon (or a
  custom macaroon with `offchain:write` and the escrow permissions) can enroll
  their own key and authorize spends, together with the channel peer. Treat
  that combination like a spending key.

Escrow records and authorization policy start at version 1. Earlier unreleased
experimental encodings are unsupported; unknown or incomplete state is rejected,
never migrated or reset. An upgrade from stock LND has no escrow state and
starts no buyout or enrollment automatically. Finish or cancel escrow sessions
before replacing the patched binary.
