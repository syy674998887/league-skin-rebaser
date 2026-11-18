# Benchmark 与本地验证

`benchmarks/` 保存项目的固定测试输入。基准工具使用 `.benchmarks/` 作为临时工作目录，
下文示例将报告写入 `.cache/benchmarks/` 和 `.cache/audits/`。核对结果后可删除这三个目录。
程序使用的皮肤路径索引和字典更新状态保存在 `.cache/` 顶层。

仓库中的 pool 对应 16.14、16.15 和 16.22 客户端。使用 pool 的命令仅在其 `gameVersion` 与本地
客户端一致时运行。没有匹配的 pool 时，应记录新 pool，不要修改已有 pool 的预期值。

`client-16.22-six` 对应客户端 `16.22.834.8910`，包含 Annie、Heimerdinger、Wukong、
Locke、Zoe 和 Ahri。预期生成 277 个皮肤及炫彩包，合计 603 对 BIN。每对 BIN 对应同一
单位的目标皮肤和默认皮肤。

## 16.22 验证范围

在 `16.22.834.8910` 上，六位英雄的 277 个包全部通过首次生成、已有包复用、
保留派生缓存重新生成，以及最终包内容核验。

另一项源文件检查覆盖 173 位英雄。它从本地客户端读到 9,095 个非默认皮肤及炫彩的完整
`skin_id`，在运行结果中记作 `fullSkinId`。例如 DJ Sona 的 `skin_id` 为 `37006`，
英雄 ID 为 `37`，英雄内序号 `skinNumber` 为 `6`，对应目标文件 `skin6.bin`。
逐项比对后，9,095 个名称和 7,126 个炫彩的归属均与客户端一致。另选 8 个 ID 实际打包，
包内 21 个单位 BIN 与基准逐字节一致，第二次运行时 8 个包全部命中缓存。

全部 15,060 份基础和目标 BIN 都能读取身份字段，用原身份记录重组后与源文件逐字节一致。
`client-16.22-resource-layouts` 收录之前受影响的 11 位英雄的全部 617 款皮肤和炫彩。
共核验 1,329 对单位 BIN，包含原先受影响的 420 个完整 `skin_id` 和对应的 428 对 BIN。
复测时，Pipeline 和 Source Golden 均使用 `benchmarks/pools/client-16.22-resource-layouts.json`。

逐包追踪这 428 对文件，其中 150 对沿用原皮肤省略资源链接的写法，其余 278 对均能找到
与原皮肤内容逐字节一致的资源表。只有两个皮肤 ID 需要补文件依赖。

DJ Sona（`37006`）的附属单位沿用默认形态省略名称的写法。璐璐的变形单位可继续使用
共享文件。斯维因的恶魔形态在 `50001` 跟随同包本体的资源链接，在 `50002` 和 `50003`
则显式引用已确认提供资源的 `skin1.bin`。

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
  --pool benchmarks/pools/client-16.22-six.json `
  --scenarios app-cold-build,output-cache-hit `
  --include-derived-warm `
  --result .cache/benchmarks/pipeline.json
```

- `app-cold-build`：清空输出和派生缓存后的完整生成。
- `output-cache-hit`：保留有效 archive，验证输出复用。
- `derived-warm-build`：删除 archive、保留派生缓存，验证跨进程复用。

使用 `--operation-baseline <result.json>` 可要求操作结构与可比较的结果一致。

## Golden 验证

Source Golden 记录客户端 WAD 中每个必要 BIN 的路径和摘要。Output Golden 核对生成包的
数量、皮肤 ID、包内 WAD 路径和默认身份，再逐单位核对资源链接的提供方。其余属性、
资源映射表的字节、记录顺序及原有依赖均与目标皮肤比对。每条新增文件依赖都须在源 WAD
中提供所引用的资源表。

```powershell
uv run python -B tools/golden_local.py `
  --pool benchmarks/pools/client-16.22-six.json `
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
