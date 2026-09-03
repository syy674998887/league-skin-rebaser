# League Skin Rebaser

[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
![Windows 10 1803+](https://img.shields.io/badge/Windows-10%201803%2B-0078D6?logo=windows&logoColor=white)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

[English](README.md) | [简体中文](README.zh-CN.md)

League Skin Rebaser reads skin data from a local League of Legends installation and
generates CSLoL-compatible ZIP or Fantome mod archives. Each generated mod keeps the
selected skin's content while remapping its runtime identity to the champion's default
`skin0`.

> [!NOTE]
> This repository does not contain or distribute Riot game assets.

## Features

- **Local inputs:** Champion, skin, chroma, and WAD data come from the installed client.
- **Flexible selection:** Choose skins interactively or process every non-classic skin and
  chroma for one champion.
- **Efficient regeneration:** The default Direct backend decodes only required WAD content
  and reuses valid outputs when their inputs have not changed.
- **Multiple formats:** Generate ZIP, Fantome, or both formats in one run.
- **Explicit failures:** Damaged or unsupported required inputs stop generation instead
  of triggering a silent fallback.

## Requirements

- Windows 10 version 1803 or later
- League of Legends installed locally
- Python 3.10 or later
- [uv](https://docs.astral.sh/uv/getting-started/installation/)

## Quick Start

```powershell
git clone https://github.com/syy674998887/league-skin-rebaser.git
cd league-skin-rebaser

Copy-Item config.example.json config.json
# Set lol_path in config.json to the local League of Legends installation.

uv sync
.\setup.bat
uv run rebaser
```

`uv sync` creates the project environment and installs dependencies.
[`setup.bat`](setup.bat) downloads pinned Ritobin and CSLoL tool releases into `bin/`
and `cslol-tools/`, and verifies SHA-256 before extraction. These setup steps can access
the network.

Without arguments, `rebaser` interactively selects a champion or exact skin/chroma.

## Usage

Generate every non-classic Ahri skin and chroma without interactive selection:

```powershell
uv run rebaser --champion Ahri
```

| Option | Default | Effect |
|---|---|---|
| `--format {zip,fantome,both}` | `zip` | Select the output archive format. |
| `--force` | Off | Rebuild selected archives even when existing outputs are valid. |
| `--wad-mode {direct,legacy}` | `direct` | Select the champion WAD preparation backend. |
| `--hash-update {auto,force,never}` | `never` | Control CommunityDragon `hashes.game` updates; `never` disables dictionary network access. |
| `--champion CHAMPION` | Interactive selection | Process every non-classic skin and chroma for one champion. |
| `--metrics-json METRICS_JSON` | Disabled | Write structured timing and operation metrics. |

```powershell
uv run rebaser --help
```

## Output

Archives are written to `output/<Champion>/<Base Skin>/` by default. Chromas use an
additional directory named after the chroma. ZIP and Fantome outputs are standard ZIP
containers with this layout:

```text
META/info.json
WAD/<Champion>.wad.client
```

`--format both` compresses once and publishes byte-identical `.zip` and `.fantome`
files.

## Support Boundaries

- Direct mode supports WAD v3.0-v3.4 and required Raw, GZip, and single-frame Zstandard
  chunks without subchunks.
- Required Satellite and ZstdMulti chunks are unsupported; unknown WAD versions and
  malformed inputs are rejected.
- Direct mode does not fall back automatically. Use `--wad-mode legacy` explicitly to use
  the locally installed `wad-extract.exe`.

## Project Scope

The separate `league-skin-version` project maintains the skin catalog, release-patch
metadata, collection scripts, and related tests. League Skin Rebaser uses identity and
content from the local client at runtime and does not read that project's data files.

## Development

```powershell
uv sync
uv run python -B -m unittest discover -s tests -q
```

- [Benchmark and Golden guide](benchmarks/README.md)

Report problems through [GitHub Issues](https://github.com/syy674998887/league-skin-rebaser/issues)
with the command, error, client version, and WAD mode. Do not attach Riot game assets.

## License

Original code and documentation are licensed under the [MIT License](LICENSE). See
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for third-party tools, data, and
trademarks. League of Legends and Riot Games are trademarks of Riot Games, Inc. This
project is not endorsed by or affiliated with Riot Games.
