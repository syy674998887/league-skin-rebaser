# Benchmarks and local validation

`benchmarks/` contains the versioned test inputs. The benchmark tools use
`.benchmarks/` for temporary work, and the examples write reports under
`.cache/benchmarks/` and `.cache/audits/`. You can delete those directories after
reviewing the results. The application stores its skin-path index and dictionary
update state at the top level of `.cache/`.

The checked-in pools target client versions 16.14, 16.15, and 16.22. Commands that use a pool
run only when its `gameVersion` matches the installed client. If no pool matches,
record a new pool instead of changing an existing pool's expected values.

The `client-16.22-six` pool covers Annie, Heimerdinger, Wukong, Locke, Zoe, and Ahri
on build `16.22.834.8910`. Expect 277 skin and chroma archives with 603 unit-level BIN
pairs. Each pair links one unit's target skin to that unit's default skin.

## Client 16.22 coverage

On build `16.22.834.8910`, all 277 archives in the six-champion pool passed cold
generation, output reuse, regeneration from derived caches, and output validation.

A separate source scan read 9,095 distinct full skin IDs for non-default skins and
chromas across 173 champions from the local client. Run results call this ID
`fullSkinId`. DJ Sona has ID `37006`, champion ID `37`, and champion-local
`skinNumber` `6`, which selects the target file `skin6.bin`.
All 9,095 catalog names and the parent assignments for all 7,126 chromas matched the
installed client. A separate eight-ID run reproduced all 21 reference unit BINs
byte for byte and reused all eight archives on its second run.

All 15,060 required base and target BINs passed identity extraction and byte-for-byte
reassembly with their original identity records. The `client-16.22-resource-layouts`
pool covers all 617 skins and chromas of the 11 champions that previously failed on
these layouts. Its 1,329 unit pairs include the 428 previously failing pairs across
420 full skin IDs. To repeat this benchmark and its output validation, use
`benchmarks/pools/client-16.22-resource-layouts.json` for both the pipeline and Source Golden.

Tracing those 428 pairs in the packaged WADs confirmed that 150 keep an absent
resource link and the other 278 reach resource maps with byte-identical contents
to the originals. Two skin IDs needed an additional file dependency.

DJ Sona (`37006`) uses the base unit's absent name. Lulu's polymorph units can reuse
a shared resource file. Swain's demon form follows the packaged champion's resolver
for `50001` and explicitly links the verified skin 1 provider for `50002` and `50003`.

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
  --pool benchmarks/pools/client-16.22-six.json `
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

Source Golden records paths and digests for each required BIN in the installed WADs.
Output Golden checks the archive set, WAD paths, default identity, and resolver
ownership across the units in each package. It compares the remaining properties,
resource-map bytes, entry order, and original dependencies with the target. Each
additional dependency must supply the referenced resolver in the source WAD.

```powershell
uv run python -B tools/golden_local.py `
  --pool benchmarks/pools/client-16.22-six.json `
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
