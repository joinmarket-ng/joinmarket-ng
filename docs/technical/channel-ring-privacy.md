# Graph-Observer Anonymity for Co-funded Channel Rings

This chapter records a bounded, reproducible observer-model experiment for the
[co-funded channel-ring protocol](protocol.md#co-funded-channel-ring-participant).
It asks whether an observer who already knows unusually strong graph facts can
attribute residual funds at funding or after selected disclosures. It is not an
expected real-world anonymity estimate, a production privacy proof, or a
measure of the equal CoinJoin outputs.

The terms and limits here are deliberately narrower than the general privacy
guidance in [Privacy](privacy.md). In particular, an operating envelope is a
testable collection of assumptions, not a calibration to normal user behavior.
No closure, cooperation, payment, or collusion percentage is a guaranteed
privacy threshold.

## Observer Model and Inference

Each synthetic ring has four or six directed, pseudonymous vertices. The
observer knows the labeled directed topology, every channel capacity, and every
participant's exact residual after input clustering and fees. It does not know
the owner-to-vertex assignment. This is a strong graph observer: it grants more
than a chain observer normally has, while deliberately omitting network,
payment-route, timing, wallet, and external-identity evidence.

The experiment generates each ring through the production
`taker.ring_planner.plan_cofunded_ring` planner with homogeneous loose channel
limits. Only the 1,000 sat incoming and outgoing contribution floors bind.
Residuals are independent synthetic samples from a Gaussian with mean 100,000
sat and standard deviation 20,000 sat, quantized to 1,000 sat and clipped at
20,000 sat. This is a convenient stress population, not an observed residual
distribution or a claim about any user population.

For a candidate owner-to-vertex assignment, let `r_i` be the residual mapped to
vertex `i`, `x_i` its outgoing contribution, and `c_i` the public capacity of
the edge from `i` to `i + 1`. Capacity conservation requires:

```text
c_i = x_i + r_(i+1) - x_(i+1)
```

Starting with `x_0`, the equations determine every other `x_i`. The solver
intersects the resulting integer bounds for all vertices:

```text
1,000 <= x_i <= r_i - 1,000
```

Every assignment with a nonempty common integer interval for `x_0` remains in
the support. At funding only, the stated prior is uniform over feasible integer
splits, so an assignment's weight is the inclusive length of its `x_0`
interval. This prior is valid only for the synthetic funding model. It is not
used for close, force-close, movement, or collusion rows.

The plain-change baseline gives the observer the residual amount directly;
only equal residual values remain ambiguous. For the funding-only ring row,
the experiment reports effective anonymity and mean Bayes vulnerability (the
mean maximum per-vertex posterior). The realized-label posterior is diagnostic
only, not attacker accuracy.

### Close and Collusion Disclosures

Close observations use opener-side balances on directed edges. A known 250 sat
close fee is normalized out. With movement bound `B`, the synthetic generator
moves a balance in the nonreversed direction by at most `B`, while the observer
uses the deliberately conservative interval `x_i in [current_i - B, current_i
+ B]`. Here `B` is 0%, 10%, or 50% of that trial's median residual.

A colluding endpoint knows its own identity and those of both neighbors, plus
the opening balances on its two incident ring edges. This is a strong bound,
not a claim that all peers or chain observers have those facts. In the optional
force-close endpoint-leak bound, every forced-close vertex and its next vertex
are also revealed. An on-chain force close does not inherently provide this
identity disclosure.

Any balance or identity disclosure changes the available support but lacks a
modeled likelihood for movement, reversals, or payments. Those rows therefore
report only feasible-owner support and uniquely identified percentages, never
posterior, entropy, or expected-anonymity claims.

## Experiment and Results

The supplied reports use seed `20260905`, 300 matched trials per size, and 400
bootstrap resamples. The 95% intervals below summarize finite seeded synthetic
trials. They are exploratory bootstrap intervals, not exact confidence
intervals for a real-world population.

The primary comparison is pre-specified and limited to the all-zero,
funding-only row. Both contrasts exclude zero in this synthetic population:

| Ring size | Plain-change effective anonymity, mean [95% CI] | Ring effective anonymity, mean [95% CI] | Plain-change Bayes vulnerability, mean [95% CI] | Ring Bayes vulnerability, mean [95% CI] |
|---:|---:|---:|---:|---:|
| 4 | 1.038 [1.023, 1.055] | 3.747 [3.700, 3.793] | 0.981 [0.972, 0.988] | 0.327 [0.319, 0.337] |
| 6 | 1.078 [1.058, 1.100] | 5.459 [5.383, 5.527] | 0.963 [0.954, 0.971] | 0.250 [0.243, 0.258] |

| Ring size | Paired ring minus baseline effective anonymity | Paired baseline minus ring Bayes vulnerability |
|---:|---:|---:|
| 4 | +2.709 [2.663, 2.757] | 0.654 [0.642, 0.664] |
| 6 | +4.382 [4.306, 4.452] | 0.713 [0.700, 0.724] |

These are significant only as paired contrasts within the specified synthetic
funding population and uniform split prior. They do not establish an expected
normal real-world anonymity gain.

### Selected Disclosure Cells

The selected rows below show support after disclosures. `C` is the closed-edge
fraction, `F` is the force-close share of closed edges, `K` is the colluding
vertex fraction, and `M` is the movement bound as a fraction of median
residual. `Standard` means the test envelope does not reveal force-close
endpoint identities. It is not a claim that these choices are normal in use.

| Size | C / F / K / M | Model | Feasible owners, mean [95% CI] | Unique targets, % [95% CI] |
|---:|---:|---|---:|---:|
| 4 | 50 / 0 / 0 / 10 | Standard | 3.230 [3.145, 3.310] | 6.08 [4.75, 7.42] |
| 4 | 100 / 10 / 0 / 10 | Standard | 2.314 [2.233, 2.403] | 23.17 [20.42, 26.00] |
| 4 | 100 / 10 / 0 / 10 | Endpoint-leak bound | 1.874 [1.788, 1.968] | 47.08 [43.00, 51.33] |
| 4 | 0 / 0 / 25 / 10 | Colluding-endpoint bound | 1.000 [1.000, 1.000] | 100.00 [100.00, 100.00] |
| 6 | 50 / 0 / 0 / 10 | Standard | 4.566 [4.450, 4.689] | 3.94 [2.94, 4.89] |
| 6 | 100 / 10 / 0 / 10 | Standard | 3.192 [3.077, 3.303] | 14.39 [12.61, 16.11] |
| 6 | 100 / 10 / 0 / 10 | Endpoint-leak bound | 2.506 [2.386, 2.621] | 34.33 [31.00, 37.44] |
| 6 | 0 / 0 / 25 / 10 | Colluding-endpoint bound | 1.152 [1.128, 1.180] | 84.83 [81.83, 87.33] |

For `F = 10%`, force close is sampled independently with a Bernoulli draw for
each closed edge. In these reports it measured 11.33% of closed four-node edges
(mean 0.453 per ring) and 10.06% of closed six-node edges (mean 0.603 per
ring). Cooperative or force-close fraction alone is not an anonymity
predictor: the relevant facts are balance movement, endpoint knowledge,
topology, collusion, and all other observer evidence.

### Negative Controls and Limits

The unused-channel, exact-close null sets all close balances exactly to their
opening values and assumes no post-funding movement. It is an attribution leak,
not a privacy result. Its unique-target support is 96.17% [94.67%, 97.67%] for
four nodes and 92.72% [91.06%, 94.33%] for six nodes. A fully informed
coordinator has anonymity set size one by definition.

An analytical equal-residual control guards against the opposite overclaim.
With residuals `(10, 10, 10, 10)`, a common two-sat circular payment changes
opening balances `(4, 5, 6, 7)` to exact close balances `(2, 3, 4, 5)`. All 24
assignments and all four owners per vertex remain feasible. Common circular
payments and reversals are therefore not evidence of privacy, and neither is
an identical circuit payment schedule.

Other material limits are:

- Directed topology labels can have rotational symmetry. Treating them as
  externally distinct can overstate attribution when no anchor breaks it.
- Equal transaction outputs are not measured. This chapter concerns residual
  attribution only.
- The observer has no likelihood model for close timing, reversed movement,
  circular movement, routing, amount patterns, network observations, wallet
  behavior, or external labels.
- The `channel_input` settlement path is a star, not this ring, and has no
  empirical privacy claim here. Its full live test remains pending.

The current ring live E2E is functional evidence only: it passed directed
payments of 1,111, 2,222, 3,333, and 4,444 sat, a taker restart, and four
sequential cooperative P2TR closes with confirmation. It does not validate a
privacy model or supply evidence for `channel_input` privacy.

### Reproduction and References

From the repository root, regenerate the two supplied local reports with:

```bash
python scripts/ring_privacy_experiment.py --seed 20260905 --trials 300 --sizes 4,6 --bootstrap-resamples 400 --output tmp/ring_privacy_experiment-300.json
python scripts/ring_privacy_experiment.py --seed 20260905 --trials 300 --sizes 4,6 --bootstrap-resamples 400 --force-endpoint-identity-leak --output tmp/ring_privacy_experiment-300-force-endpoint-leak.json
```

The focused experiment test suite has 11 tests. It checks the integer interval
derivation, ground-truth support, disclosure monotonicity, null controls,
baseline collisions, deterministic contrasts and sampling, and the command-line
smoke run.

Practical precautions follow from the model rather than from policy thresholds:
do not use identical circuit payments as a mix, avoid consolidating close
outputs or the equal CoinJoin output with them, vary real payment activity and
close timing, resist Sybil participation, and treat the coordinator and
colluding endpoints as observed parties.

For broader design limits, see Spiral's
[The Many Headaches of CoinJoin](https://spiralxyz.substack.com/p/the-many-headaches-of-coinjoin),
nothingmuch's [Collaborative Transaction Privacy](https://gist.github.com/nothingmuch/d84ba390d89b5b08897af2d95009c2a1),
and its [follow-up on breaking links across slots](https://gist.github.com/nothingmuch/f5b9a559958c6116606d9da0d4d884f2).
