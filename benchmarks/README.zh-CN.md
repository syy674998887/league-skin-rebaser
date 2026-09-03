# Benchmark 与本地验证

`benchmarks/` 保存本地验证使用的固定输入定义。结果与生成时的客户端、WAD、工具和机器
环境绑定。Pipeline 与历史 Git benchmark 默认使用 Git 已忽略的 `.benchmarks/` 作为工作
目录；下文示例将报告写入同样被忽略的 `.cache/`。两者都不参与普通皮肤包生成，也不提交
到仓库。

仓库中的 pool 对应 16.14 和 16.15 客户端。使用 pool 的命令仅在其 `gameVersion` 与本地
客户端一致时运行。没有匹配的 pool 时，应记录新 pool，不要修改已有 pool 的预期值。

## 目录

```text
benchmarks/
├── fixtures/    # 本地历史 Git 比较使用的固定输入
└── pools/       # 英雄、皮肤、unit 与预期 workload
```

`tools/benchmark_git_phases.py` 是本地维护工具，依赖本地 `pre-publish` 分支保留的历史
commit；只包含公开 `main` 的克隆无法运行它。`fixtures/hash-upgrade-units.json` 仅供该
工具使用。

## 自动测试

普通代码变更优先运行完整测试：

```powershell
uv run python -B -m unittest discover -s tests -q
```

## Pipeline benchmark

修改 WAD reader、候选发现、缓存 identity、Ritobin batching 或输出语义后，可运行固定
workload：

```powershell
uv run python -B tools/benchmark_pipeline.py `
  --phase local-check `
  --pool benchmarks/pools/hash-upgrade-fast5.json `
  --scenarios app-cold-build,output-cache-hit `
  --include-derived-warm `
  --result .cache/benchmarks/pipeline.json
```

- `app-cold-build`：清空输出和派生缓存后的完整生成。
- `output-cache-hit`：保留有效 archive，验证输出复用。
- `derived-warm-build`：删除 archive、保留派生缓存，验证跨进程复用。

使用 `--operation-baseline <result.json>` 可要求操作结构与可比较的结果一致。

## Golden 验证

Source Golden 记录源 WAD 中需要验证的 BIN；Output Golden 检查生成包中的 WAD path、
paired unit、身份字段及其与目标内容的关系：

```powershell
uv run python -B tools/golden_local.py `
  --pool benchmarks/pools/hash-upgrade-fast5.json `
  --output .cache/benchmarks/source-golden.json

uv run python -B tools/golden_outputs.py `
  --benchmark-result .cache/benchmarks/pipeline.json `
  --source-golden .cache/benchmarks/source-golden.json `
  --output .cache/benchmarks/output-golden.json
```

## 专项 audit

Prepared WAD audit 与 Layout Shadow 共用完整的 `upgrade-v2` Source Golden：

```powershell
uv run python -B tools/golden_local.py `
  --pool benchmarks/pools/upgrade-v2.json `
  --output .cache/audits/upgrade-v2-source-golden.json

uv run python -B tools/prepared_audit.py `
  --source-golden .cache/audits/upgrade-v2-source-golden.json

uv run python -B tools/layout_shadow.py `
  --source-golden .cache/audits/upgrade-v2-source-golden.json
```

其他专项检查：

```powershell
uv run python -B tools/wad_corpus_audit.py `
  --output .cache/audits/wad-corpus.json

uv run scripts/update_champion_units.py --check `
  --report .cache/audits/champion-units.json
```

英雄附属单位注册表以本地 LCU、`hashes.game.txt` 和经过验证的英雄 WAD 内容为准。排查
疑似遗漏时，可以把 Hitori 的
[`championPets.ts`](https://github.com/hitori-rebocchi/hitori-bocchi/blob/3e217ceeb072cde9cfa92c1eae136a1e8fa394a2/src/main/services/championPets.ts)
等外部列表仅作为反向核对的候选清单。任何名称仍须通过本地 WAD 验证；仓库不保存外部
列表快照。

## 结果解释

- 只有输入 identity、pool、scenario 和操作结构一致的结果才可直接比较。
- 正确性 Gate 与操作次数是硬证据；单次 wall time 只作描述。
- Windows page cache 与机器负载会影响耗时，小幅差异不足以支持增加实现复杂度。
- 原始结果保留在 `.cache/` 或外部归档中，不提交到仓库。
