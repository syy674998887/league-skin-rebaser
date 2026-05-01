"""Automated League of Legends skin rebaser.

Project layout (relative to this script):

    config.json              {"lol_path": "..."} (auto-created on first run)
    skin_ids.json            {"<skin id>": "<skin name>"} (Riot skin id lookup)
    versions.json            {"<skin name>": "<version>"} (user-maintained)
    input/
        <skin name>/
            <unit>/                     (auto-extracted; name = unit,
                                         lowercase, e.g. "annie",
                                         "annietibbers")
                skin0.bin               (base skin)
                skin<N>.bin             (target skin, N != 0)
            <unit2>/                    (optional: summons / additional units)
                ...
            step1/<unit>/        skin0.py, skin<N>.py       (dumped, per unit)
            step2/<unit>/        skin<N>_modified.py        (replaced, per unit)
            step3/               data/characters/<unit>/skins/skin0.bin
            step4/               WAD/<Champion>.wad.client, META/info.json
    output/
        <Champion>/              (display name from skin_ids.json, e.g. "Jarvan IV")
            <base skin>/
                <base skin>.zip  (final deliverable — presence means done)
                <chroma>/
                    <chroma>.zip

Behavior:
    - On first run, prompts for LoL install path and saves to config.json.
    - Prompts for skin name(s) (comma-separated). Looks up skin ids from
      skin_ids.json and automatically includes chromas.
    - Extracts skin0.bin + skin<N>.bin from the champion WAD found in
      <LoL path>/Game/DATA/FINAL/Champions/ (all matching units auto-discovered).
    - Multi-unit support: champions with summons (Annie+Tibbers, etc.) get one
      subfolder per unit; all units are packed into a single WAD.
    - Main champion = shortest unit-folder name (tiebreak: lex).
    - Author is hardcoded to "Untargetable".
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
RITOBIN_CLI = SCRIPT_DIR / "bin" / "ritobin_cli.exe"
WAD_MAKE = SCRIPT_DIR / "cslol-tools" / "wad-make.exe"
WAD_EXTRACT = SCRIPT_DIR / "cslol-tools" / "wad-extract.exe"
INPUT_ROOT = SCRIPT_DIR / "input"
OUTPUT_ROOT = SCRIPT_DIR / "output"
VERSIONS_PATH = SCRIPT_DIR / "versions.json"
SKIN_IDS_PATH = SCRIPT_DIR / "skin_ids.json"
CONFIG_PATH = SCRIPT_DIR / "config.json"
AUTHOR = "Untargetable"
WAD_CLIENT_SUFFIX = ".wad.client"
LOL_CHAMPIONS_REL = Path("Game") / "DATA" / "FINAL" / "Champions"


def log(msg: str, prefix: str = "rebaser") -> None:
    print(f"[{prefix}] {msg}", flush=True)


_WINDOWS_FORBIDDEN_RE = re.compile(r'[<>:"/\\|?*]')


def sanitize_for_windows(name: str) -> str:
    # Windows forbids < > : " / \ | ? * in file/folder names. Strip them and
    # collapse runs of whitespace so "PROJECT: Sivir" -> "PROJECT Sivir".
    return re.sub(r"\s+", " ", _WINDOWS_FORBIDDEN_RE.sub("", name)).strip()


def normalize_champion_name(name: str) -> str:
    # Jarvan IV -> JarvanIV, Miss Fortune -> MissFortune, Kha'Zix -> KhaZix.
    return re.sub(r"[^0-9A-Za-z]", "", name)


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def wad_client_base_name(path: Path) -> str:
    name = path.name
    if name.lower().endswith(WAD_CLIENT_SUFFIX):
        return name[:-len(WAD_CLIENT_SUFFIX)]
    return path.stem


def load_config() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return {}


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def ensure_lol_path() -> Path:
    """Return the LoL Champions directory, prompting and persisting if needed."""
    cfg = load_config()
    lol_root = cfg.get("lol_path")

    if lol_root:
        champions_dir = Path(lol_root) / LOL_CHAMPIONS_REL
        if champions_dir.is_dir():
            return champions_dir
        log(f"warning: saved lol_path no longer valid: {lol_root}")

    while True:
        raw = input(
            "Enter League of Legends path (e.g. C:\\Riot Games\\League of Legends): "
        ).strip().strip('"')
        if not raw:
            continue
        champions_dir = Path(raw) / LOL_CHAMPIONS_REL
        if champions_dir.is_dir():
            cfg["lol_path"] = raw
            save_config(cfg)
            log(f"saved LoL path to {CONFIG_PATH.name}")
            return champions_dir
        log(
            f"Champions directory not found: {champions_dir}\n"
            f"Make sure the path contains Game\\DATA\\FINAL\\Champions"
        )


_STEP_DIR_NAMES = {"step1", "step2", "step3", "step4"}


def find_input_units(skin_dir: Path, only_units: set[str] | None = None) -> list[tuple[Path, Path, str]]:
    # Each non-step subfolder is one unit (main champion or a summon model);
    # returns (base_bin, target_bin, unit_name) per unit, sorted by unit name.
    unit_dirs = [
        p for p in skin_dir.iterdir()
        if p.is_dir() and p.name not in _STEP_DIR_NAMES
    ]
    if only_units is not None:
        wanted = {u.lower() for u in only_units}
        unit_dirs = [p for p in unit_dirs if p.name.lower() in wanted]
    if not unit_dirs:
        if only_units is None:
            sys.exit(
                f"no unit subfolder under {skin_dir}; expected "
                f"{skin_dir}/<Unit>/skin0.bin and skin<N>.bin"
            )
        sys.exit(f"none of the requested unit folders exist under {skin_dir}: {sorted(only_units)}")

    results: list[tuple[Path, Path, str]] = []
    for unit_dir in sorted(unit_dirs, key=lambda p: p.name):
        base: Path | None = None
        target: Path | None = None
        for p in unit_dir.iterdir():
            if not p.is_file():
                continue
            m = re.fullmatch(r"skin(\d+)\.bin", p.name, re.IGNORECASE)
            if not m:
                continue
            if int(m.group(1)) == 0:
                base = p
            else:
                if target is not None:
                    sys.exit(f"multiple non-base skin bins in {unit_dir}: {target.name}, {p.name}")
                target = p
        if base is None:
            sys.exit(f"skin0.bin not found in {unit_dir}")
        if target is None:
            sys.exit(f"skin<N>.bin (N != 0) not found in {unit_dir}")
        results.append((base, target, unit_dir.name))
    return results


def pick_main_champion(unit_names: list[str]) -> str:
    # Shortest wins (tiebreak: lex). LoL packs summons under the main champion's
    # WAD, and summon names follow <Champion><suffix> (Annie+Tibbers,
    # Swain+RavenSpawn, Yorick+Ghoul1/2/3), so the shortest name is the main.
    return min(unit_names, key=lambda n: (len(n), n))


def run_ritobin(src: Path, dst: Path, in_fmt: str, out_fmt: str) -> None:
    if not RITOBIN_CLI.exists():
        sys.exit(f"ritobin_cli.exe not found at {RITOBIN_CLI}")
    cmd = [str(RITOBIN_CLI), "-i", in_fmt, "-o", out_fmt, str(src), str(dst)]
    log(f"$ {' '.join(cmd)}", "ritobin")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stdout.strip():
        log(f"stdout: {result.stdout.strip()}", "ritobin")
    if result.stderr.strip():
        log(f"stderr: {result.stderr.strip()}", "ritobin")
    if result.returncode != 0:
        sys.exit(f"ritobin_cli exited with code {result.returncode}")


def run_wad_make(src_dir: Path, dst_wad: Path) -> None:
    if not WAD_MAKE.exists():
        sys.exit(f"wad-make.exe not found at {WAD_MAKE}")
    cmd = [str(WAD_MAKE), str(src_dir), str(dst_wad)]
    log(f"$ {' '.join(cmd)}", "wad-make")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stdout.strip():
        log(f"stdout: {result.stdout.strip()}", "wad-make")
    if result.stderr.strip():
        log(f"stderr: {result.stderr.strip()}", "wad-make")
    if result.returncode != 0:
        sys.exit(f"wad-make exited with code {result.returncode}")


def load_skin_ids() -> dict[str, str]:
    if not SKIN_IDS_PATH.exists():
        sys.exit(f"skin_ids.json not found at {SKIN_IDS_PATH}")
    data = json.loads(SKIN_IDS_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        sys.exit("skin_ids.json must be a JSON object of skin-id -> skin-name strings")
    return {str(k): str(v) for k, v in data.items()}


def resolve_skin_name(skin_name: str) -> list[tuple[str, str]]:
    """Look up a skin + its chromas by display name (case-insensitive).

    Returns list of (skin_id, display_name) — the base skin plus any
    entries whose name starts with it followed by " (".
    """
    skin_ids = load_skin_ids()
    name_lower = skin_name.lower()
    # Find exact match first.
    base_sid: str | None = None
    for sid, dname in skin_ids.items():
        if sid.endswith("000"):
            continue
        if dname.lower() == name_lower:
            base_sid = sid
            break
    if base_sid is None:
        sys.exit(f"skin not found: {skin_name!r}")
    # Collect base + chromas (names starting with base name + " (").
    prefix = name_lower + " ("
    results: list[tuple[str, str]] = []
    for sid, dname in skin_ids.items():
        if sid.endswith("000"):
            continue
        dl = dname.lower()
        if dl == name_lower or dl.startswith(prefix):
            results.append((sid, dname))
    return sorted(results, key=lambda x: int(x[0]))


def find_source_wad(champion_unit: str, champions_dir: Path | None = None) -> Path:
    candidates: list[Path] = []

    # Search in LoL Champions directory first (preferred source).
    if champions_dir is not None and champions_dir.is_dir():
        for wad_path in champions_dir.iterdir():
            if not wad_path.is_file():
                continue
            if not wad_path.name.lower().endswith(WAD_CLIENT_SUFFIX):
                continue
            if normalize_champion_name(wad_client_base_name(wad_path)).lower() == champion_unit.lower():
                candidates.append(wad_path)

    # Fallback: also search project directory.
    for wad_path in SCRIPT_DIR.rglob(f"*{WAD_CLIENT_SUFFIX}"):
        if is_relative_to(wad_path, INPUT_ROOT) or is_relative_to(wad_path, OUTPUT_ROOT):
            continue
        if normalize_champion_name(wad_client_base_name(wad_path)).lower() == champion_unit.lower():
            candidates.append(wad_path)

    if not candidates:
        search_locations = [str(SCRIPT_DIR)]
        if champions_dir is not None:
            search_locations.insert(0, str(champions_dir))
        sys.exit(
            f"source WAD for {champion_unit!r} not found in:\n"
            + "\n".join(f"  - {loc}" for loc in search_locations)
        )

    # Prefer LoL install dir, then project root, then largest file.
    def sort_key(p: Path) -> tuple:
        in_lol = champions_dir is not None and is_relative_to(p, champions_dir)
        in_root = p.parent == SCRIPT_DIR
        return (not in_lol, not in_root, -p.stat().st_size, str(p).lower())

    candidates.sort(key=sort_key)
    return candidates[0]


def run_wad_extract_to_temp(
    wad_path: Path, skin_numbers: list[int],
) -> dict[int, list[tuple[str, bytes, bytes]]]:
    """Extract WAD once and find units for each skin number.

    Returns {skin_number: [(unit_name, base_bytes, target_bytes), ...]}.
    """
    if not WAD_EXTRACT.exists():
        sys.exit(f"wad-extract.exe not found at {WAD_EXTRACT}")
    with tempfile.TemporaryDirectory(prefix=".wad-extract-", dir=SCRIPT_DIR) as temp_name:
        temp_dir = Path(temp_name)
        temp_wad = temp_dir / wad_path.name
        shutil.copy2(wad_path, temp_wad)

        cmd = [str(WAD_EXTRACT), f".\\{temp_wad.name}"]
        log(f"$ {' '.join(cmd)}  (cwd={temp_dir})", "wad-extract")
        result = subprocess.run(cmd, cwd=temp_dir, capture_output=True, text=True)
        if result.returncode != 0:
            tail = "\n".join((result.stdout + result.stderr).splitlines()[-30:])
            sys.exit(f"wad-extract exited with code {result.returncode}\n{tail}")

        extracted_dir = temp_dir / f"{wad_client_base_name(temp_wad)}.wad"
        if not extracted_dir.is_dir():
            extracted_dirs = [p for p in temp_dir.iterdir() if p.is_dir() and p.name.endswith(".wad")]
            if len(extracted_dirs) != 1:
                sys.exit(f"could not find wad-extract output folder under {temp_dir}")
            extracted_dir = extracted_dirs[0]

        characters_dir = extracted_dir / "data" / "characters"
        if not characters_dir.is_dir():
            sys.exit(f"no data/characters directory found after extracting {wad_path.name}")

        out: dict[int, list[tuple[str, bytes, bytes]]] = {}
        for sn in skin_numbers:
            hits: list[tuple[str, bytes, bytes]] = []
            for char_dir in sorted(characters_dir.iterdir()):
                if not char_dir.is_dir():
                    continue
                skins_dir = char_dir / "skins"
                if not skins_dir.is_dir():
                    continue
                base_bin = skins_dir / "skin0.bin"
                target_bin = skins_dir / f"skin{sn}.bin"
                if base_bin.is_file() and target_bin.is_file():
                    log(f"  found unit: {char_dir.name} (skin0 + skin{sn})", "wad-extract")
                    hits.append((char_dir.name, base_bin.read_bytes(), target_bin.read_bytes()))
            if not hits:
                log(f"  warning: no unit found for skin{sn}", "wad-extract")
            out[sn] = hits

        return out


def prepare_skins(
    skins: list[tuple[str, str]], champions_dir: Path | None = None,
) -> tuple[dict[Path, set[str]], str]:
    """Prepare input folders for one or more skins (base + chromas).

    Extracts the WAD once and grabs all needed skin numbers in one pass.
    Returns ({skin_dir: found_units}, champion_display_name) for each skin.
    """
    skin_ids = load_skin_ids()
    first_id = skins[0][0]
    champion_id = int(first_id[:-3])
    champion_key = f"{champion_id}000"
    champion_display_name = skin_ids.get(champion_key, "")
    if not champion_display_name:
        sys.exit(f"champion base id {champion_key!r} not found in {SKIN_IDS_PATH}")
    champion_unit = normalize_champion_name(champion_display_name)

    wad_path = find_source_wad(champion_unit, champions_dir)
    try:
        rel = wad_path.relative_to(SCRIPT_DIR)
    except ValueError:
        rel = wad_path
    log(f"source WAD = {rel}")

    # Map skin_number -> (skin_id, display_name) for all skins.
    sn_map: dict[int, tuple[str, str]] = {}
    for sid, dname in skins:
        sn_map[int(sid[-3:])] = (sid, dname)

    log(f"extracting {len(sn_map)} skin(s) for {champion_display_name}...")
    all_units = run_wad_extract_to_temp(wad_path, list(sn_map.keys()))

    result: dict[Path, set[str]] = {}
    for sn, (sid, dname) in sn_map.items():
        disk_name = sanitize_for_windows(dname)
        skin_dir = INPUT_ROOT / disk_name
        units = all_units.get(sn, [])
        if not units:
            log(f"  skip {dname}: no matching bins in WAD")
            continue
        found: set[str] = set()
        for unit_name, base_data, target_data in units:
            unit_dir = skin_dir / unit_name
            unit_dir.mkdir(parents=True, exist_ok=True)
            (unit_dir / "skin0.bin").write_bytes(base_data)
            (unit_dir / f"skin{sn}.bin").write_bytes(target_data)
            found.add(unit_name)
        log(f"  {dname}: {len(found)} unit(s)")
        result[skin_dir] = found

    return result, champion_display_name


REPLACEMENT_PATTERNS: list[tuple[str, str]] = [
    (r" = SkinCharacterDataProperties \{$", "SkinCharacterDataProperties entry key"),
    (r"^\s*ChampionSkinName: string = ",    "ChampionSkinName"),
    (r"^\s*mResourceResolver: link = ",     "mResourceResolver"),
    (r"^    \S[^:]* = ResourceResolver \{$", "ResourceResolver entry key"),
]


def replace_line_from_base(base_text: str, target_text: str, pattern: str, label: str) -> str:
    base_lines = base_text.splitlines()
    target_lines = target_text.splitlines()

    base_hits = [i for i, ln in enumerate(base_lines) if re.search(pattern, ln)]
    target_hits = [i for i, ln in enumerate(target_lines) if re.search(pattern, ln)]

    if len(base_hits) != 1:
        sys.exit(f"[{label}] expected 1 match in base, got {len(base_hits)} for pattern {pattern!r}")
    if len(target_hits) != 1:
        sys.exit(f"[{label}] expected 1 match in target, got {len(target_hits)} for pattern {pattern!r}")

    base_line = base_lines[base_hits[0]]
    tgt_idx = target_hits[0]
    old_line = target_lines[tgt_idx]

    if base_line == old_line:
        log(f"  {label}: line {tgt_idx + 1} already identical, skip")
    else:
        log(f"  {label}: line {tgt_idx + 1}")
        log(f"    -  {old_line}")
        log(f"    +  {base_line}")
        target_lines[tgt_idx] = base_line

    trailing = "\n" if target_text.endswith("\n") else ""
    return "\n".join(target_lines) + trailing


def modify_py(base_text: str, target_text: str) -> str:
    text = target_text
    for pattern, label in REPLACEMENT_PATTERNS:
        text = replace_line_from_base(base_text, text, pattern, label)
    return text


def load_versions() -> dict[str, str]:
    if not VERSIONS_PATH.exists():
        return {}
    data = json.loads(VERSIONS_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        sys.exit(f"versions.json must be a JSON object of skin-name -> version strings")
    return data


def fresh_dir(path: Path) -> Path:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    return path


def process_skin(
    skin_dir: Path,
    display_name: str,
    version: str,
    champion_name: str,
    base_skin_name: str,
    only_units: set[str] | None = None,
) -> None:
    # display_name: the authoritative skin name (may contain ':' etc.) — used
    # for info.json "Name". skin_dir.name is the sanitized on-disk form and is
    # used for zip filenames and output folder paths.
    disk_name = skin_dir.name
    log(f"=== processing: {display_name} (version {version}) ===")

    units = find_input_units(skin_dir, only_units)
    main_champion = pick_main_champion([u[2] for u in units])
    log(f"units         = {[u[2] for u in units]}")
    log(f"main champion = {main_champion}  (used for step3 internal paths)")

    step1 = fresh_dir(skin_dir / "step1")
    step2 = fresh_dir(skin_dir / "step2")
    step3 = fresh_dir(skin_dir / "step3")
    step4 = fresh_dir(skin_dir / "step4")

    for base_bin, target_bin, unit in units:
        log(f"--- unit: {unit} ---")
        unit_step1 = step1 / unit
        unit_step2 = step2 / unit
        unit_step1.mkdir()
        unit_step2.mkdir()

        log(f"  step 1 ({unit}): .bin -> .py")
        base_py = unit_step1 / f"{base_bin.stem}.py"
        target_py = unit_step1 / f"{target_bin.stem}.py"
        run_ritobin(base_bin, base_py, "bin", "text")
        run_ritobin(target_bin, target_py, "bin", "text")

        base_text = base_py.read_text(encoding="utf-8")
        target_text = target_py.read_text(encoding="utf-8")

        log(f"  step 2 ({unit}): modify target .py")
        modified_text = modify_py(base_text, target_text)
        modified_py = unit_step2 / f"{target_bin.stem}_modified.py"
        modified_py.write_text(modified_text, encoding="utf-8")
        log(f"    wrote {modified_py.relative_to(skin_dir)}")

        log(f"  step 3 ({unit}): modified .py -> data/.../skin0.bin")
        # wad-make (step 4) hashes paths relative to its src argument, so
        # `data/` must sit one level inside step3 — then the in-game path
        # `data/characters/<unit>/skins/skin0.bin` survives. All units share
        # one step3 tree so a single wad-make call packs them together.
        data_dir = step3 / "data" / "characters" / unit.lower() / "skins"
        data_dir.mkdir(parents=True)
        final_bin = data_dir / "skin0.bin"
        run_ritobin(modified_py, final_bin, "text", "bin")
        log(f"    placed {final_bin.relative_to(skin_dir)}")

    log(f"--- step 4: build WAD + META (main = {main_champion}) ---")
    wad_dir = step4 / "WAD"
    wad_dir.mkdir()
    wad_path = wad_dir / f"{normalize_champion_name(champion_name)}.wad.client"
    run_wad_make(step3, wad_path)
    log(f"wad = {wad_path.relative_to(skin_dir)}")

    meta_dir = step4 / "META"
    meta_dir.mkdir()
    info = {
        "Author": AUTHOR,
        "Name": display_name,
        "Version": version,
    }
    info_path = meta_dir / "info.json"
    info_path.write_text(
        json.dumps(info, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    log(f"wrote {info_path.relative_to(skin_dir)}")

    log("--- zip WAD + META ---")
    champion_folder = sanitize_for_windows(champion_name)
    base_folder = sanitize_for_windows(base_skin_name)
    if disk_name == base_folder:
        # Base skin: zip goes directly in the skin folder.
        zip_dir = OUTPUT_ROOT / champion_folder / base_folder
    else:
        # Chroma: zip goes in a subfolder under the base skin.
        zip_dir = OUTPUT_ROOT / champion_folder / base_folder / disk_name
    zip_dir.mkdir(parents=True, exist_ok=True)
    zip_path = zip_dir / f"{disk_name}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for folder in (wad_dir, meta_dir):
            for item in folder.rglob("*"):
                if item.is_file():
                    zf.write(item, item.relative_to(step4))
    log(f"final zip = {zip_path.relative_to(SCRIPT_DIR)}")


def main() -> None:
    log(f"script dir    = {SCRIPT_DIR}")
    log(f"input root    = {INPUT_ROOT}")
    log(f"output root   = {OUTPUT_ROOT}")
    log(f"versions.json = {VERSIONS_PATH}")
    log(f"skin_ids.json = {SKIN_IDS_PATH}")

    INPUT_ROOT.mkdir(parents=True, exist_ok=True)
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

    champions_dir = ensure_lol_path()
    log(f"champions dir = {champions_dir}")

    raw_input = ""
    while not raw_input:
        raw_input = input("Enter skin name (comma-separated for multiple), e.g. Lunar Beast Annie: ").strip()

    prepared_map: dict[Path, set[str]] = {}
    champion_names: dict[Path, str] = {}
    base_skin_names: dict[Path, str] = {}
    for name in raw_input.split(","):
        name = name.strip()
        if not name:
            continue
        matches = resolve_skin_name(name)
        base_skin_display = matches[0][1]  # first match is the base skin
        log(f"matched {len(matches)} skin(s): {[d for _, d in matches]}")
        skin_units, champ_name = prepare_skins(matches, champions_dir)
        prepared_map.update(skin_units)
        for skin_dir in skin_units:
            champion_names[skin_dir] = champ_name
            base_skin_names[skin_dir] = base_skin_display

    versions = load_versions()

    # Windows folders can't contain characters like ':'; versions.json keys are
    # the authoritative display names (e.g. "PROJECT: Sivir") while the folder
    # on disk is the sanitized form ("PROJECT Sivir"). Build a lookup from
    # sanitized name -> (display_name, version) and detect collisions early.
    disk_to_entry: dict[str, tuple[str, str]] = {}
    for display_name, version in versions.items():
        disk_key = sanitize_for_windows(display_name)
        if disk_key in disk_to_entry:
            other, _ = disk_to_entry[disk_key]
            sys.exit(
                f"versions.json has two entries that sanitize to the same folder "
                f"name {disk_key!r}: {other!r} and {display_name!r}"
            )
        disk_to_entry[disk_key] = (display_name, version)

    candidates = sorted(p for p in INPUT_ROOT.iterdir() if p.is_dir())
    if not candidates:
        sys.exit(f"no skin folders under {INPUT_ROOT}")

    pending: list[Path] = []
    for skin_dir in candidates:
        # zip may live at output/<Champion>/<base>/<name>.zip or
        # output/<Champion>/<base>/<chroma>/<name>.zip; search with recursive glob.
        existing = list(OUTPUT_ROOT.glob(f"**/{skin_dir.name}/{skin_dir.name}.zip"))
        if existing and skin_dir not in prepared_map:
            log(f"skip (already has zip): {skin_dir.name}")
            continue
        pending.append(skin_dir)

    if not pending:
        log("nothing to do — all skins already zipped")
        return

    log(f"pending: {[p.name for p in pending]}")

    missing = [p.name for p in pending if p.name not in disk_to_entry]
    if missing:
        log(f"versions.json has no entries for: {missing} — using empty version")
        for name in missing:
            disk_to_entry[name] = (name, "")

    for skin_dir in pending:
        display_name, version = disk_to_entry[skin_dir.name]
        only_units = prepared_map.get(skin_dir)
        champ_name = champion_names.get(skin_dir, skin_dir.name)
        base_skin = base_skin_names.get(skin_dir, display_name)
        process_skin(skin_dir, display_name, version, champ_name, base_skin, only_units)

    log("all done.")


if __name__ == "__main__":
    main()
