# Benchmarks and local validation

`benchmarks/` contains fixed input definitions used by local validation. Results are
tied to the client, WADs, tools, and host that produced them. The pipeline and
historical Git benchmark use the Git-ignored `.benchmarks/` as their default work
area; the examples below write reports to the Git-ignored `.cache/`. Neither
participates in normal package generation or is committed to the repository.

The checked-in pools target client versions 16.14 and 16.15. Commands that use a pool
run only when its `gameVersion` matches the installed client. If no pool matches,
record a new pool instead of changing an existing pool's expected values.

## Contents

```text
benchmarks/
├── fixtures/    # Fixed input for local historical Git comparisons
└── pools/       # Champions, skins, units, and expected workloads
```

`tools/benchmark_git_phases.py` is a local maintainer tool. It requires the historical
commits retained on the local `pre-publish` branch and cannot run from a public
`main`-only clone. `fixtures/hash-upgrade-units.json` is used only by that tool.

## Automated tests

Run the full test suite for ordinary code changes:

```powershell
uv run python -B -m unittest discover -s tests -q
```

## Pipeline benchmark

Use the fixed workload after changing the WAD reader, candidate discovery, cached
identities, Ritobin batching, or output semantics:

```powershell
uv run python -B tools/benchmark_pipeline.py `
  --phase local-check `
  --pool benchmarks/pools/hash-upgrade-fast5.json `
  --scenarios app-cold-build,output-cache-hit `
  --include-derived-warm `
  --result .cache/benchmarks/pipeline.json
```

- `app-cold-build`: full generation after clearing outputs and derived caches.
- `output-cache-hit`: retain a valid archive and verify output reuse.
- `derived-warm-build`: remove the archive, retain derived caches, and verify reuse
  across processes.

Pass `--operation-baseline <result.json>` to require the same operation structure as a
comparable result.

## Golden validation

Source Golden records the BIN entries to verify in source WADs. Output Golden checks
WAD paths, paired units, identity fields, and their relationship to target content in
generated packages:

```powershell
uv run python -B tools/golden_local.py `
  --pool benchmarks/pools/hash-upgrade-fast5.json `
  --output .cache/benchmarks/source-golden.json

uv run python -B tools/golden_outputs.py `
  --benchmark-result .cache/benchmarks/pipeline.json `
  --source-golden .cache/benchmarks/source-golden.json `
  --output .cache/benchmarks/output-golden.json
```

## Focused audits

Prepared WAD audit and Layout Shadow share the complete `upgrade-v2` Source Golden:

```powershell
uv run python -B tools/golden_local.py `
  --pool benchmarks/pools/upgrade-v2.json `
  --output .cache/audits/upgrade-v2-source-golden.json

uv run python -B tools/prepared_audit.py `
  --source-golden .cache/audits/upgrade-v2-source-golden.json

uv run python -B tools/layout_shadow.py `
  --source-golden .cache/audits/upgrade-v2-source-golden.json
```

Other focused checks:

```powershell
uv run python -B tools/wad_corpus_audit.py `
  --output .cache/audits/wad-corpus.json

uv run scripts/update_champion_units.py --check `
  --report .cache/audits/champion-units.json
```

The champion auxiliary-unit registry is derived from the local LCU,
`hashes.game.txt`, and verified champion WAD contents. When investigating a possible
omission, external lists such as Hitori's
[`championPets.ts`](https://github.com/hitori-rebocchi/hitori-bocchi/blob/3e217ceeb072cde9cfa92c1eae136a1e8fa394a2/src/main/services/championPets.ts)
may be used only as reverse-check candidates. Every name must still be verified
against local WADs; the repository does not retain external snapshots.

## Interpreting results

- Compare results only when the input identity, pool, scenario, and operation
  structure match.
- Correctness gates and operation counts are hard evidence; a single wall-clock
  measurement is descriptive only.
- Windows page cache and host load affect timing. Small differences do not justify
  additional implementation complexity.
- Keep raw results in `.cache/` or an external archive; do not commit them.
