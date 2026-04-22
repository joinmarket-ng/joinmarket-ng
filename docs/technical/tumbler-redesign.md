# Tumbler Redesign (jm-tumbler)

Status: accepted design, implementation in progress.
Scope: replaces the reference `tumbler.py` script and the stub
`POST /api/v1/wallet/{walletname}/taker/schedule` router in jmwalletd.

## Problem

The goal of the tumbler is to take a wallet with a known origin (KYC'd buy,
address reuse, observed UTXO graph) and produce a wallet whose UTXOs cannot be
linked to that origin by a chain-analysis observer. The reference
implementation does this with a taker-only loop:

1. Sweep every non-empty mixdepth to the next mixdepth using
   CoinJoin-sweeps (no change). "Stage 1."
2. In each mixdepth, do N CoinJoins with random fractional amounts, with
   exponentially-distributed wait times. The last entry in each mixdepth is a
   sweep. "Stage 2."
3. The last `addrcount` mixdepths' final sweeps go to user-supplied external
   destination addresses instead of `INTERNAL`.

Three problems follow the reference design into joinmarket-ng:

- **Subset-sum deanonymisation**. A taker's inputs satisfy
  `sum(inputs) = cj_amount + change + cj_fees + tx_fee` — an arithmetic
  constraint an observer can exploit with subset-sum solving to recover the
  taker's true input set from the combined CoinJoin input pool. The density
  of non-equal CoinJoin outputs is well below the Lagarias–Odlyzko bound,
  which makes the recovery computationally trivial in practice. See
  `joinmarket-ng#114`. A tumbler that acts only as a taker leaks this signal
  on every single transaction.

- **Persistent role fingerprint**. A wallet that is *always* a taker and
  *never* a maker has a different on-chain footprint than a wallet that
  alternates roles — different fee directions, different UTXO age
  distributions, different timings. A long taker-only tumbler is therefore a
  worse mix than a shorter mixed-role one.

- **Opaque state**. The reference tumbler persists its schedule as a
  comma-delimited text file whose column semantics (especially the
  `completed` flag — `0`, `1`, or a 64-char txid) are undocumented and
  brittle. `taker/config.py::Schedule` in jm-ng is a cleaner pydantic model
  but is not persisted at all: a jmwalletd restart loses the full run plan.

## Goals

- Produce a wallet whose UTXOs are not trivially linkable to the wallet's
  pre-tumbler UTXOs, *without* depending on Lightning, submarine swaps, or
  any out-of-band infrastructure beyond the existing JoinMarket IRC/onion
  message bus.
- Reduce taker-side subset-sum leakage by mixing taker and maker roles
  within one tumble, not across separate user actions.
- Resume correctly after `jmwalletd` restart, kill -9, host reboot.
- Produce an on-disk plan and run log a human can read and a support
  engineer can reason about, without a custom binary format and without a
  database.
- Keep the existing wallet / taker / maker components as libraries; no new
  network code, no new crypto.

## Non-goals

- Lightning integration. `joinmarket-ng#280` (LN swap input camouflage) is
  explicitly out of scope for this module. `jm-tumbler` composes with it if
  it lands, but does not depend on it.
- Protocol-level changes to JoinMarket coinjoin. We consume `Taker.do_coinjoin`
  and `MakerBot.start`/`stop` unchanged.
- A generic workflow engine. `jm-tumbler` does one job: move funds from
  origin mixdepths to destination addresses through mixed-role phases.

## Architecture

New package `jm-tumbler` at `joinmarket-ng/jm_tumbler/` sits *above* both
`taker` and `maker`:

```
jmwalletd
   |
   v
jm-tumbler  --->  taker.Taker.do_coinjoin (per CJ)
   |         \-> maker.MakerBot.start / stop (per maker phase)
   v
jmwallet.WalletService + jmwallet.backends.*
```

The package exports three public symbols:

- `Plan` — the pydantic model of a full tumble (phases, transitions, state).
- `PlanBuilder` — produces a `Plan` from wallet state and user input.
- `TumbleRunner` — consumes a `Plan` and drives it to completion,
  persisting progress after every state transition.

No other package imports anything else from `jm-tumbler`.

### Phases

A `Plan` is an ordered list of *phases*. The runner executes phases one at a
time; a phase completes before the next begins. There are three phase kinds:

1. **taker_coinjoin** — a single CoinJoin as taker.
   Inputs: `mixdepth`, `amount` (sats or a `sweep` flag), `destination`
   (external address or `"INTERNAL"`), `counterparty_count`, `rounding`.
   Produces one txid.

2. **maker_session** — run `MakerBot` for a bounded window of time *or* until
   a target number of CoinJoins have been taken against this wallet,
   whichever comes first. Inputs: `deadline` (monotonic wall-clock),
   `target_cj_count` (optional, None = unlimited), `offer_params`
   (ordertype, minsize, relfee, absfee, txfee).

3. **bondless_taker_burst** — a small run of taker coinjoins with no fidelity
   bond requirement, where the taker role is chosen *precisely to avoid
   looking like a tumbler taker*. The difference from `taker_coinjoin` is
   configuration: these always use `amount_fraction` with specific denominations
   tuned to match the maker-change distribution in the current orderbook,
   and they never advance the internal mixdepth; they stay in the current
   mixdepth and consume mixed UTXOs. They are used between maker sessions to
   break subset-sum correlation on change outputs created during the
   maker phase (see [Subset-sum mitigation](#subset-sum-mitigation) below).

Phases are separated by a *wait* (simple `asyncio.sleep`) sampled from an
exponential distribution. Between any two phase kinds except two consecutive
`taker_coinjoin` phases, the runner also triggers a **nick rotation** —
`Taker` and `MakerBot` get fresh `NickIdentity` instances for the next phase.

### Why these three phases

Three invariants motivate the design:

- **Origin-cleavage**. The first phase is always a `taker_coinjoin` sweep of
  a funded mixdepth, with `destination=INTERNAL`. This corresponds to the
  reference tumbler's "stage 1" and is the single most important step:
  every input with known origin goes through a CoinJoin before anything else
  happens. All subsequent phases act on post-CoinJoin UTXOs.

- **Role-mixing**. A pure taker-only run is a tumbler fingerprint. A pure
  maker-only run cannot end on a specific external destination. Alternating
  them within a single tumble lets the final phases be takers (to reach the
  destination addresses) while the bulk of mid-tumble activity is maker
  activity, where *other* takers' choices drive the UTXO consumption pattern
  and fees flow *into* the wallet instead of out of it.

- **Subset-sum mitigation on change**. See next section.

### Subset-sum mitigation

We use two techniques, composable and each standalone-useful:

- **Maker phase between two taker phases.** A maker phase consumes UTXOs
  selected by other takers (unpredictable subsets from our wallet's
  perspective) and creates new CJ-out and CJ-change outputs matched to
  *other* participants' amounts. By the time the next `taker_coinjoin`
  fires, the wallet's UTXO graph has new points whose linkage back to the
  pre-maker set is mediated by taker-chosen subsets we did not control. This
  is the primary mitigation.

- **Bondless-taker burst with orderbook-matched denominations.** When we
  *must* act as taker (to reach a destination address, or to sweep residual
  change), we round the CoinJoin amount to a significant-figure count chosen
  so that the amount is indistinguishable from the maker-change outputs
  currently visible in the orderbook. This is a refinement of the existing
  `rounding` knob in `ScheduleEntry`. We sample rounding weights from the
  current orderbook rather than from a static config distribution.

Neither technique closes the subset-sum vulnerability completely; closing it
requires protocol changes (standard denominations or taker-matches-maker-
change). They raise the cost of recovery from "trivial with off-the-shelf
solver" to "requires participating in or simulating the tumble." They
compose cleanly with future protocol fixes.

### Plan structure (YAML)

Stored at `<data_dir>/schedules/<walletname>.yaml`. Single file per wallet.
Overwritten in place on every state transition. No database, no WAL, no
journal. The file is valid YAML 1.2, human-editable, and its schema is this
pydantic model serialized with `yaml.safe_dump`:

```yaml
plan_id: 01HP5K9K2H3XR0QW0A1B2C3D4E   # ULID
wallet_name: Satoshi.jmdat
created_at: 2026-04-22T12:34:56Z
updated_at: 2026-04-22T12:41:03Z
destinations:
  - bcrt1q...abc
  - bcrt1q...def
  - bcrt1q...ghi
parameters:
  target_mixdepth_count: 5
  maker_session_budget_hours: 6.0
  min_maker_count: 5
  max_maker_count: 9
  time_lambda_seconds: 180
  rounding_strategy: orderbook_matched  # or: none | static
  seed: 8f3a2b6e7c1d4f59         # for deterministic re-planning; optional
phases:
  - index: 0
    kind: taker_coinjoin
    status: completed
    wait_before_seconds: 0
    mixdepth: 0
    amount_sats: 0                # 0 = sweep
    destination: INTERNAL
    counterparty_count: 7
    rounding: 16
    started_at: 2026-04-22T12:34:56Z
    finished_at: 2026-04-22T12:38:02Z
    txid: 9ab4...c7
  - index: 1
    kind: maker_session
    status: running
    wait_before_seconds: 154.3
    deadline_at: 2026-04-22T18:41:03Z
    target_cj_count: null
    offer:
      ordertype: sw0reloffer
      minsize: 500000
      cjfee_r: "0.0002"
      cjfee_a: 1000
      txfee: 0
    started_at: 2026-04-22T12:41:03Z
    txids_participated: [...]
  - index: 2
    kind: bondless_taker_burst
    status: pending
    ...
  - index: 3
    kind: taker_coinjoin
    status: pending
    mixdepth: 4
    amount_sats: 0
    destination: bcrt1q...abc
    ...
current_phase: 1
```

`status` is one of `pending | running | completed | failed | cancelled`.
The file is the full source of truth. `jmwalletd` reloads it on startup and
if `current_phase` points to a `running` phase it resumes, otherwise it
continues with the next `pending` phase.

YAML is chosen over JSON because operators will open this file with an
editor during support — comments, timestamps, and multi-line amounts read
better in YAML. It is chosen over TOML because nested phase structures with
heterogeneous schemas per `kind` are awkward in TOML.

### Plan builder

Input: wallet balance per mixdepth, destination addresses, user options.

Algorithm:

```
if no destinations: error
if total_balance == 0: error

# stage 1: origin cleavage (same as reference)
for each non-empty mixdepth, descending:
    append taker_coinjoin(mixdepth, amount=0, dest=INTERNAL)

# role-mixed body
for each destination-bearing mixdepth (last addrcount of the chain):
    append maker_session(budget = sample(maker_window_distribution))
    append bondless_taker_burst(count = sample(1..3))
    append taker_coinjoin(mixdepth, amount=small fraction, dest=INTERNAL, rounding=orderbook_matched)
    ... (repeat fractional steps, like stage 2)
    append taker_coinjoin(mixdepth, amount=0, dest=external_destination_address)

# interleave wait_before_seconds via exponential(time_lambda)
```

The key change vs the reference: between the "stage 2" fractional taker
CoinJoins we insert maker sessions of bounded duration and bondless-taker
bursts. The number of inserted phases is proportional to the balance and the
`maker_session_budget_hours` parameter. On a small regtest wallet this
collapses to a near-linear sweep; on a full wallet it produces a multi-hour
plan.

### Runner

A single `TumbleRunner.run()` coroutine. Control flow:

```
while plan.has_pending_phase():
    phase = plan.current_phase()
    await sleep(phase.wait_before_seconds)
    phase.mark_running(); plan.persist()
    try:
        match phase.kind:
            taker_coinjoin:       await _run_taker_phase(phase)
            maker_session:        await _run_maker_phase(phase)
            bondless_taker_burst: await _run_bondless_taker_phase(phase)
        phase.mark_completed(); plan.persist()
    except asyncio.CancelledError:
        phase.mark_cancelled(); plan.persist(); raise
    except PhaseFailed as e:
        phase.mark_failed(e); plan.persist()
        if plan.retry_policy.should_retry(phase): continue
        raise
```

`_run_taker_phase` and `_run_bondless_taker_phase` instantiate `Taker`,
call `await taker.start()`, `await taker.do_coinjoin(...)`, then
`await taker.stop()`. `_run_maker_phase` instantiates `MakerBot`, calls
`await bot.start()` inside a task, waits either for the deadline or for the
target CJ count (observed via `bot.current_offers` and history entries),
then calls `await bot.stop()`.

The runner always calls `stop()` in a `finally` block. This fixes the
existing leak in `jmwalletd/src/jmwalletd/routers/coinjoin.py:127-138` where
`taker.start()` spawns a monitoring task that is never cancelled.

### Concurrency contract with jmwalletd

Existing `DaemonState.coinjoin_state` is a tri-state
`NOT_RUNNING | TAKER_RUNNING | MAKER_RUNNING`. We extend it to:

```
NOT_RUNNING | TAKER_RUNNING | MAKER_RUNNING | TUMBLER_RUNNING
```

`TUMBLER_RUNNING` is mutually exclusive with the other two at the daemon
level. Inside a tumbler run the daemon does *not* toggle between
`TAKER_RUNNING` and `MAKER_RUNNING`; the phase kind is exposed via the
new `/tumbler/status` endpoint. This keeps callers of the existing
`/session` endpoint from seeing rapid state flicker.

The existing `/taker/schedule` and `/taker/stop` endpoints become thin
adapters over `jm-tumbler` so the Jam frontend keeps working during the
migration. A new `/tumbler` subtree is the canonical API surface.

### API surface

Canonical (new):

- `POST /api/v1/wallet/{walletname}/tumbler/plan`
  Body: `{destinations: [addr], options: TumblerOptions}`. Response: the
  full `Plan` document as JSON (i.e. the YAML structure, JSON-encoded).
  Does *not* start execution; 201 Created.

- `POST /api/v1/wallet/{walletname}/tumbler/start`
  Body: `{plan_id: str}` or `{}` to start the persisted plan.
  Response: 202 Accepted.

- `GET  /api/v1/wallet/{walletname}/tumbler/status`
  Response: `{plan: Plan, currentPhase: PhaseStatus | null, rescanning:
  bool}`.

- `POST /api/v1/wallet/{walletname}/tumbler/stop`
  Response: 202. Cancels the runner task, persists cancelled state.

Legacy (kept wire-compatible for one release):

- `POST /api/v1/wallet/{walletname}/taker/schedule` — adapter that builds a
  plan and starts it. Returns a legacy-shaped
  `{schedule: list[list[...]]}` for the Jam frontend until the frontend is
  updated.
- `GET  /api/v1/wallet/{walletname}/taker/schedule` — adapter that projects
  `Plan.phases` to the legacy 7-tuple shape.
- `GET  /api/v1/wallet/{walletname}/taker/stop` — delegates to
  `/tumbler/stop`.

The Jam frontend will be updated in a separate commit to consume the new
endpoints directly and drop the 7-tuple schedule type.

### Security / privacy notes

- The Plan file contains destination addresses in cleartext. This matches
  the existing reference schedulefile behavior. Not encrypted at rest.
- Nick rotation between phases is mandatory. Rationale: if a passive
  observer of the orderbook correlates our maker-phase nick with our
  taker-phase nick, the role-mixing benefit collapses. Nick rotation is a
  cheap countermeasure already supported by `MakerBot.start`.
- `maker_session` phases check wallet self-protection (taker nick not equal
  to maker nick within the same session). The existing
  `read_nick_state` / `write_nick_state` machinery in `jmcore.paths` is
  reused, but the runner writes a tumbler-specific nick entry so
  long-running standalone maker/taker processes outside the tumble still
  see a consistent self-exclusion set.

### Failure handling

- A failed `taker_coinjoin` retries up to `RetryPolicy.max_taker_retries`
  (default 3) with a tweak-schedule pass analogous to
  `tweak_tumble_schedule` in the reference: lower counterparty count on
  sweeps, re-fraction remaining non-sweep amounts. Retries live in the same
  phase; the phase status goes `running -> failed -> running` on retry.
- A failed `maker_session` does not retry by default; the session ended
  early. The runner proceeds to the next phase. If the wallet balance is
  now insufficient, the planner is re-run in place and the plan file is
  rewritten with the new phases.
- A crashed `jmwalletd` resumes from the persisted plan. Safe because every
  `mark_*` is followed by a `plan.persist()`. Worst case: a single phase is
  attempted twice, which for taker phases means an extra CoinJoin (user
  money lost only in fees) and for maker phases means a brief double-maker
  that will be rejected by the directory servers due to nick collision.

### Testing

- Unit (`jm_tumbler/tests/`): `test_plan_builder.py`,
  `test_plan_persistence.py`, `test_runner_transitions.py`. Uses fakes for
  `Taker` and `MakerBot`.
- Router (`jmwalletd/tests/`): extend `test_coinjoin_router.py` and add
  `test_tumbler_router.py`. TestClient level.
- E2E (`tests/e2e/test_tumbler_e2e.py`): regtest docker-compose stack with
  two makers + our jmwalletd; plan a 2-phase tumble (taker sweep + taker
  external) and assert the destination address receives funds. A full
  role-mixed tumble is too long for CI; we cover the role-mixed path with
  a unit test that fakes the `MakerBot` and records phase ordering.

## Migration

1. Land `jm-tumbler` package with plan/runner, no router wiring. Passing
   unit tests.
2. Wire jmwalletd new `/tumbler/*` endpoints + legacy adapters. E2E test.
3. Regenerate Jam OpenAPI client, switch `SweepPage` to new endpoints,
   drop legacy 7-tuple types. Separate commit, separate repo.
4. Once Jam is on new endpoints, the legacy `/taker/schedule` adapter can
   be removed in a follow-up release.
