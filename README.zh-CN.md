# League Skin Rebaser

[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
![Windows 10 1803+](https://img.shields.io/badge/Windows-10%201803%2B-0078D6?logo=windows&logoColor=white)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

[English](README.md) | [简体中文](README.zh-CN.md)

League Skin Rebaser 从本地 League of Legends 客户端读取皮肤数据，生成可导入
CSLoL 的 ZIP 或 Fantome 模组包。生成的模组会保留目标皮肤内容，并将其运行时身份
重新映射到英雄的默认皮肤 `skin0`。

> [!NOTE]
> 本仓库不包含或分发 Riot 游戏资源。

## 特性

- **本地数据：** 英雄、皮肤、炫彩和 WAD 数据均来自当前安装的客户端。
- **灵活选择：** 支持交互选择，也可按英雄处理全部非经典皮肤和炫彩。
- **高效增量生成：** 默认 Direct 后端只解码所需 WAD 内容，并复用输入未变化的有效输出。
- **多种格式：** 支持 ZIP、Fantome，或一次生成两种格式。
- **明确失败：** 遇到损坏或不受支持的必要输入时停止生成，不静默回退。

## 环境要求

- Windows 10 版本 1803 或更新版本
- 本地已安装 League of Legends
- Python 3.10 或更新版本
- [uv](https://docs.astral.sh/uv/getting-started/installation/)

## 快速开始

```powershell
git clone https://github.com/syy674998887/league-skin-rebaser.git
cd league-skin-rebaser

Copy-Item config.example.json config.json
# 在 config.json 中将 lol_path 设置为本机 League of Legends 安装目录

uv sync
.\setup.bat
uv run rebaser
```

`uv sync` 会创建项目环境并安装依赖。[`setup.bat`](setup.bat) 会把固定版本的
Ritobin 和 CSLoL 工具下载到 `bin/` 与 `cslol-tools/`，并在解压前校验 SHA-256。
这些安装步骤会访问网络。

不带参数运行时，`rebaser` 会交互选择英雄或精确的皮肤/炫彩。

## 使用方式

无需交互，为 Ahri 的全部非经典皮肤和炫彩生成模组：

```powershell
uv run rebaser --champion Ahri
```

| 参数 | 默认值 | 作用 |
|---|---|---|
| `--format {zip,fantome,both}` | `zip` | 选择输出格式。 |
| `--force` | 关闭 | 即使现有输出有效，也重新生成所选模组。 |
| `--wad-mode {direct,legacy}` | `direct` | 选择英雄 WAD 准备后端。 |
| `--hash-update {auto,force,never}` | `never` | 控制 CommunityDragon `hashes.game` 更新；`never` 禁用字典网络访问。 |
| `--champion CHAMPION` | 交互选择 | 处理指定英雄的全部非经典皮肤和炫彩。 |
| `--metrics-json METRICS_JSON` | 不启用 | 写入结构化计时与操作指标。 |

```powershell
uv run rebaser --help
```

## 输出

模组默认写入 `output/<Champion>/<Base Skin>/`；炫彩会增加一个以炫彩名称命名的目录。
ZIP 和 Fantome 输出都是标准 ZIP 容器，内部结构为：

```text
META/info.json
WAD/<Champion>.wad.client
```

`--format both` 只压缩一次，并发布字节完全相同的 `.zip` 和 `.fantome` 文件。

## 支持边界

- Direct 模式支持 WAD v3.0-v3.4，以及不含 subchunk 的 Raw、GZip 和单帧 Zstandard
  required chunk。
- 所需 chunk 使用 Satellite 或 ZstdMulti 时不受支持；未知 WAD 版本和损坏输入会被拒绝。
- Direct 模式不会自动回退；需要使用本地安装的 `wad-extract.exe` 时，请显式选择
  `--wad-mode legacy`。

## 项目边界

皮肤目录、首发补丁版本、采集脚本及相关测试由独立的 `league-skin-version` 项目维护。
League Skin Rebaser 运行时使用本地客户端提供的身份与内容，不读取该项目的数据文件。

## 开发

```powershell
uv sync
uv run python -B -m unittest discover -s tests -q
```

- [Benchmark 与 Golden 指南](benchmarks/README.zh-CN.md)

通过 [GitHub Issues](https://github.com/syy674998887/league-skin-rebaser/issues)
提交问题时，请提供复现命令、错误信息、客户端版本和 WAD 模式；请勿上传 Riot 游戏资源。

## 许可证

本项目原创代码和文档采用 [MIT License](LICENSE)。第三方工具、数据与商标说明见
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。League of Legends 和 Riot Games
是 Riot Games, Inc. 的商标；本项目与 Riot Games 不存在认可或关联关系。
