# Development

## Local Setup

From repository root:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip

pip install -e './jmcore[dev]' -e './jmwallet[dev]' -e './maker[dev]' -e './taker[dev]' -e './directory_server[dev]' -e './orderbook_watcher[dev]' -e './jmwalletd[dev]' -e './tumbler[dev]'
```

## Lint / Format / Type Check

Install and enable [prek](https://github.com/anomalyco/prek) to run checks automatically on commit:

```bash
pip install prek
prek install
```

Run all checks manually:

```bash
prek run --all-files
```

The complexity gate uses `complexipy`:

```bash
complexipy . --failed --suggest-refactors
```

Fallback (if prek is unavailable):

```bash
pre-commit run --all-files
```

## Commit Conventions

All commits must follow [Conventional Commits](https://www.conventionalcommits.org/).

Format: `<type>(<scope>): <description>`

Common types: `feat`, `fix`, `refactor`, `test`, `docs`, `build`, `ci`, `chore`.

Use the component as scope (e.g. `fix(jmwallet): ...`, `feat(taker): ...`).

For `feat:` and `fix:` commits, include at least one user-oriented `Changelog:` trailer in the commit body:

```
feat(taker): add fee estimation fallback

Changelog: Taker now estimates fees when the maker's quote is unavailable.
```

`Changelog:` trailers are not required (and should be omitted) for `docs:`, `test:`, `build:`, `refactor:`, `chore:`, and `ci:` commits.

Changelog entries are generated automatically at release time from these trailers — do not edit `CHANGELOG.md` manually during development.

## Tests

Fast unit test run (per-component packages plus the repo-root `tests/`
directory, which holds the TUI script tests, release/changelog/flatpak
helper tests, and the finalize-bond-psbt tests):

```bash
pytest jmcore directory_server orderbook_watcher maker taker jmwallet jmwalletd tumbler
pytest --ignore=tests/playwright tests
```

The two invocations are required because each component ships its own
`tests` package and pytest's collector cannot reconcile the duplicated
top-level module name in a single run.

Full orchestrated suite (unit + Docker-backed suites, in parallel):

```bash
./scripts/run_parallel_tests.sh
```

When selecting Docker-marked tests manually, use `--fail-on-skip`.

### Rootless Docker Port Forwarding

With rootless Docker using pasta and `--port-driver=implicit`, recent pasta
versions exclude ephemeral ports from automatic forwarding (`--tcp-ports=auto`).
Bitcoin RPC can therefore be healthy inside its container while the runner reports
`Bitcoin RPC not ready on host port ...`. See the
[pasta port-forwarding documentation](https://passt.top/builds/latest/web/passt.1.html).

Check the host's ephemeral range and existing listeners:

```bash
sysctl net.ipv4.ip_local_port_range
ss -ltn
docker ps
```

By default, an instance's E2E Bitcoin RPC port is `20000 + instance * 2000`;
other services and suites use offsets within that instance's port band. For an
ephemeral range of `32768-60999`, instance 18 uses the excluded port `56000`,
while instance 3 uses `26000`. Choose an unused instance whose published ports
are outside the host's ephemeral range and do not overlap existing listeners:

```bash
./scripts/run_parallel_tests.sh --instance 3 --suite e2e
```

This workaround does not require restarting Docker or changing host networking.

## Documentation

Build docs locally from repository root:

```bash
python scripts/build_docs.py
```

What this does:

- installs docs dependencies from `requirements-docs.txt`
- installs editable project packages used by API doc generation
- runs `properdocs build -q -f properdocs.yml` and writes output to `site/`

If you want to run the steps manually:

```bash
python -m pip install -r requirements-docs.txt
python -m pip install -e jmcore -e jmwallet -e taker -e maker -e directory_server -e orderbook_watcher -e jmwalletd -e tumbler
python -m properdocs build -q -f properdocs.yml
```

For local preview:

```bash
python -m properdocs serve -q -f properdocs.yml
```

## Reference Compatibility Tests

Some e2e tests require a local clone of the reference implementation at repository root:

```bash
git clone --depth 1 https://github.com/JoinMarket-Org/joinmarket-clientserver.git
```

Then run marker-specific tests as needed (`-m reference`, `-m reference_maker`).

## Releases and Signatures

Reproducible release verification and signing workflows:

- verify: `scripts/verify-release.sh`
- build locally: `scripts/build-release.sh`
- sign: `scripts/sign-release.sh`

See [Signatures](../README-signatures.md) for repository signature layout.

### Pre-Release Preparation

Update dependencies, regenerate help text, run the full test suite, then
commit the resulting changes manually before bumping the version:

`scripts/update-deps.sh` also advances the maintained `python-bitcointx` release
wheel URL and SHA-256 pin everywhere it is referenced before regenerating locks.

```bash
scripts/update-base-images.sh \
  && scripts/update-deps.sh \
  && scripts/update-flatpak-deps.py \
  && scripts/update_readme_help.py \
  && scripts/generate_completions.py \
  && prek run --all-files \
  && scripts/run_parallel_tests.sh 2>&1 | tee tmp/run_parallel_tests.log
```

Review `tmp/run_parallel_tests.log`, then commit:

```bash
git add -p && git commit
```

### Local-First Workflow (Recommended for Release Managers)

Build, sign locally, then push. CI verifies independently against your
signed manifest.

```bash
# 1. Bump version — opens $EDITOR on CHANGELOG.md before committing/tagging
# Select patch/minor/major as appropriate for the release.
scripts/bump_version.py patch --no-push

# 2. Build release images locally (generates release-manifest-<version>.txt)
scripts/build-release.sh

# 3. Sign the locally-built manifest
VERSION=$(grep -oP '__version__\s*=\s*"\K[^"]+' jmcore/src/jmcore/version.py)
scripts/sign-release.sh "$VERSION" \
  --manifest "release-manifest-$VERSION.txt" \
  --key 1C53A412D11EF3051704419C44912E1E03005B31

# 4. Push commit, tag, and signature to trigger CI
git push && git push --tags
```

CI builds the same images independently and creates the GitHub release as a
**pre-release**: it stays invisible to `releases/latest` (the resolution
point for the installer, the TUI, and the update check) until the trusted
signature quorum exists. Nothing further is required from the release
manager: once the remaining signatures land on `main`,
`promote-release.yaml` runs `verify-release.sh` (manifest signature quorum
with local-manifest layer cross-check, installer signature quorum, installer
asset vs release commit, registry digests) and automatically promotes the
release to the published latest release.

The version bump also compares the bundled `config.toml.template` with the
previous release. It adds the complete commented diff to the changelog, or an
explicit unchanged notice, so the same configuration guidance is included in
the GitHub release notes. Existing user configuration files are never modified.

To preview GitHub release-note synchronization from the committed changelog:

```bash
scripts/sync_github_release_notes.py
```

Review the listed tags, then apply all published release updates or selected
ones:

```bash
scripts/sync_github_release_notes.py --apply
scripts/sync_github_release_notes.py 0.34.0 0.33.0 --apply
```

This requires an authenticated GitHub CLI session with permission to edit
releases in `joinmarket-ng/joinmarket-ng`.

`release-manifest-<version>.txt` is gitignored — it is a build artefact
and is not committed to the repository.

`build-release.sh` starts its BuildKit builder before launching parallel
builds. It retains each run's output under
`tmp/release-build-<version>.*`; use the reported per-image log path to
diagnose a failed build. OCI export intermediates are still removed on exit.

**LEVEL**: `bump_version.py` accepts `patch`, `minor`, or `major` as its
positional argument. Set `LEVEL` as a shell variable if you want to
parameterise it:

```bash
LEVEL=minor  # or patch / major
scripts/bump_version.py "$LEVEL" --no-push
```

The JAM standalone image is built and published by
[`jam-docker`](https://github.com/joinmarket-webui/jam-docker/tree/master/standalone-ng),
so it is not included in JoinMarket NG release manifests. Historical manifests
that included `jam-ng` still skip its non-deterministic frontend layers during
verification.

Playwright builds pin both the JAM release and the `jam-docker` build context in
`docker-compose.yml`. Update those pins together with the Flatpak JAM source by
running `scripts/update-flatpak-deps.py`; do not advance them manually. The pinned
standalone image is a build-only base. A local Playwright Dockerfile overlays the
current JoinMarket NG working tree so local runs and CI test the same checkout,
even when the base image's remote clone layer is cached. Set `JAM_NG_PULL_POLICY`
explicitly only when testing a pre-built `JAM_NG_IMAGE` override.

#### How reproducibility is achieved

Three inputs must match between local builds and CI builds for layer digests
to be byte-identical:

- `SOURCE_DATE_EPOCH`: derived from the release commit's timestamp.
- `JOINMARKET_BUILD_COMMIT` / `JOINMARKET_BUILD_REF`: stamped into wheel
  metadata via `jmcore/setup.py` (writes `_build_info.py`). When unset,
  `setup.py` falls back to `git rev-parse`, but the docker build sandbox
  has no `.git` directory, so passing these explicitly is required.
- Pinned base image digests, apt package versions, and pip build constraints
  (`setuptools`, `wheel`) — all enforced in the Dockerfiles.

`build-release.sh` derives commit/ref from the local git state and passes
them as `--build-arg` to `docker buildx build`, mirroring CI's
`release.yaml` invocation. `verify-release.sh --reproduce` does the same,
deriving the commit from the manifest and the ref from the tag pointing at
that commit (falling back to the supplied version). If you invoke
`docker buildx build` directly you must replicate this manually or
local/CI digests will diverge.

### CI-First Workflow (For Additional Signers)

Wait for the Release workflow to complete (the release appears as a
pre-release), then reproduce and sign:

```bash
git pull
VERSION=<version>
scripts/sign-release.sh "$VERSION" --key <fingerprint>
```

This downloads the CI manifest, rebuilds locally, and signs both the manifest
and the release installer if digests match. When merging via pull request
instead of pushing directly, pass `--no-push` and commit the signature files
on a branch:

```bash
scripts/sign-release.sh "$VERSION" --key <fingerprint> --no-push
git checkout -b "sign-$VERSION"
git add "signatures/$VERSION/"
git commit -m "build: add GPG signature for release $VERSION"
# push the branch and open a pull request
```

Once the signatures merge to `main`, `promote-release.yaml` verifies the
quorum and promotes the pre-release automatically; no manual release edits
are needed.

## Verify a Release

```bash
./scripts/verify-release.sh <version>

# with local reproduction check
./scripts/verify-release.sh <version> --reproduce
```

Reproduction uses Dockerfiles from the release commit to ensure strict
historical accuracy.

## Sign a Release

```bash
# Sign a CI-built release (downloads manifest, reproduces, signs)
./scripts/sign-release.sh <version> --key <gpg-key-id>

# Sign a locally-built manifest (from build-release.sh)
./scripts/sign-release.sh <version> --manifest release-manifest-<version>.txt --key <gpg-key-id>
```

For the local-first workflow, the manifest must come from the same release
commit as the local tag created by `bump_version.py`. `sign-release.sh` refuses
to sign a local manifest if its embedded `commit:` does not match the release
tag (or `HEAD` when no local tag exists).
