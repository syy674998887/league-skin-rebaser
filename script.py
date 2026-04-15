"""Automated the League of Legends skin rebaser.

Project layout (relative to this script):

    input/
        <skin name>/
            skin0.bin            (base skin, user-provided)
            skin<N>.bin          (target skin, user-provided, N != 0)
            step1/               skin0.py, skin<N>.py       (dumped)
            step2/               skin<N>_modified.py        (replaced)
            step3/               data/characters/<champion>/skins/skin0.bin
            step4/               WAD/<champion>.wad.client, META/info.json
    output/
        <Champion>/
            <skin name>/
                <skin name>.zip  (final deliverable — presence means done)
    versions.json                {"<skin name>": "<version>"} (user-maintained)

Behavior:
    - Iterates every folder under input/.
    - Skips a folder if output/<Champion>/<skin name>/<skin name>.zip already exists.
    - Looks up the version from versions.json by skin-folder name.
    - Author is hardcoded to "Untargetable".
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
RITOBIN_CLI = SCRIPT_DIR / "bin" / "ritobin_cli.exe"
WAD_MAKE = SCRIPT_DIR / "cslol-tools" / "wad-make.exe"
INPUT_ROOT = SCRIPT_DIR / "input"
OUTPUT_ROOT = SCRIPT_DIR / "output"
VERSIONS_PATH = SCRIPT_DIR / "versions.json"
AUTHOR = "Untargetable"


def log(msg: str) -> None:
    print(f"[rebaser] {msg}", flush=True)


_WINDOWS_FORBIDDEN_RE = re.compile(r'[<>:"/\\|?*]')


def sanitize_for_windows(name: str) -> str:
    # Windows forbids < > : " / \ | ? * in file/folder names. Strip them and
    # collapse runs of whitespace so "PROJECT: Sivir" -> "PROJECT Sivir".
    return re.sub(r"\s+", " ", _WINDOWS_FORBIDDEN_RE.sub("", name)).strip()


def find_input_bins(skin_dir: Path) -> tuple[Path, Path]:
    base: Path | None = None
    target: Path | None = None
    for p in skin_dir.iterdir():
        if not p.is_file():
            continue
        m = re.fullmatch(r"skin(\d+)\.bin", p.name, re.IGNORECASE)
        if not m:
            continue
        if int(m.group(1)) == 0:
            base = p
        else:
            if target is not None:
                sys.exit(f"multiple non-base skin bins in {skin_dir}: {target.name}, {p.name}")
            target = p
    if base is None:
        sys.exit(f"skin0.bin not found in {skin_dir}")
    if target is None:
        sys.exit(f"skin<N>.bin (N != 0) not found in {skin_dir}")
    return base, target


def run_ritobin(src: Path, dst: Path, in_fmt: str, out_fmt: str) -> None:
    if not RITOBIN_CLI.exists():
        sys.exit(f"ritobin_cli.exe not found at {RITOBIN_CLI}")
    cmd = [str(RITOBIN_CLI), "-i", in_fmt, "-o", out_fmt, str(src), str(dst)]
    log(f"$ {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stdout.strip():
        log(f"stdout: {result.stdout.strip()}")
    if result.stderr.strip():
        log(f"stderr: {result.stderr.strip()}")
    if result.returncode != 0:
        sys.exit(f"ritobin_cli exited with code {result.returncode}")


def run_wad_make(src_dir: Path, dst_wad: Path) -> None:
    if not WAD_MAKE.exists():
        sys.exit(f"wad-make.exe not found at {WAD_MAKE}")
    cmd = [str(WAD_MAKE), str(src_dir), str(dst_wad)]
    log(f"$ {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stdout.strip():
        log(f"stdout: {result.stdout.strip()}")
    if result.stderr.strip():
        log(f"stderr: {result.stderr.strip()}")
    if result.returncode != 0:
        sys.exit(f"wad-make exited with code {result.returncode}")


def detect_champion(py_text: str) -> str:
    # Dumped .py has a linked[] block like:
    #   linked: list[string] = {
    #       "DATA/Senna_Skins_Skin0_..."        <- may appear first (Senna)
    #       "DATA/Characters/Senna/Senna.bin"   <- the entry we want
    #       "DATA/Characters/Senna/Animations/Skin72.bin"
    #       ...
    #   }
    # Returns the canonical proper-case name (e.g. "Swain", "Senna", "MissFortune").
    # Caller lowercases only for filesystem paths under data/characters/.
    block_match = re.search(r'linked:\s*list\[string\]\s*=\s*\{([^}]*)\}', py_text)
    if not block_match:
        sys.exit("could not locate linked[] block in dumped .py")
    m = re.search(r'"DATA/Characters/([^/"]+)/[^"]+\.bin"', block_match.group(1))
    if not m:
        sys.exit("no DATA/Characters/<Champion>/... entry in linked[]; inspect the dumped .py")
    return m.group(1)


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
        sys.exit(
            f"versions.json not found at {VERSIONS_PATH}\n"
            f'create it with entries like: {{"Fried Chicken King Swain": "26.06"}}'
        )
    data = json.loads(VERSIONS_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        sys.exit(f"versions.json must be a JSON object of skin-name -> version strings")
    return data


def fresh_dir(path: Path) -> Path:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    return path


def process_skin(skin_dir: Path, display_name: str, version: str) -> None:
    # display_name: the authoritative skin name (may contain ':' etc.) — used
    # for info.json "Name". skin_dir.name is the sanitized on-disk form and is
    # used for zip filenames and output folder paths.
    disk_name = skin_dir.name
    log(f"=== processing: {display_name} (version {version}) ===")

    base_bin, target_bin = find_input_bins(skin_dir)
    log(f"base   (skin0) = {base_bin.name}")
    log(f"target (skinN) = {target_bin.name}")

    step1 = fresh_dir(skin_dir / "step1")
    step2 = fresh_dir(skin_dir / "step2")
    step3 = fresh_dir(skin_dir / "step3")
    step4 = fresh_dir(skin_dir / "step4")

    log("--- step 1: .bin -> .py ---")
    base_py = step1 / f"{base_bin.stem}.py"
    target_py = step1 / f"{target_bin.stem}.py"
    run_ritobin(base_bin, base_py, "bin", "text")
    run_ritobin(target_bin, target_py, "bin", "text")

    base_text = base_py.read_text(encoding="utf-8")
    target_text = target_py.read_text(encoding="utf-8")
    champion = detect_champion(target_text)
    log(f"champion = {champion}")

    log("--- step 2: modify target .py ---")
    modified_text = modify_py(base_text, target_text)
    modified_py = step2 / f"{target_bin.stem}_modified.py"
    modified_py.write_text(modified_text, encoding="utf-8")
    log(f"wrote {modified_py.relative_to(skin_dir)}")

    log("--- step 3: modified .py -> data/.../skin0.bin ---")
    # wad-make (called in step 4) hashes paths relative to its src argument,
    # so `data/` must sit one level inside step3 — then the in-game path
    # `data/characters/<champion>/skins/skin0.bin` survives.
    data_dir = step3 / "data" / "characters" / champion.lower() / "skins"
    data_dir.mkdir(parents=True)
    final_bin = data_dir / "skin0.bin"
    run_ritobin(modified_py, final_bin, "text", "bin")
    log(f"placed {final_bin.relative_to(skin_dir)}")

    log("--- step 4: build WAD + META ---")
    wad_dir = step4 / "WAD"
    wad_dir.mkdir()
    wad_path = wad_dir / f"{champion}.wad.client"
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
    zip_dir = OUTPUT_ROOT / champion / disk_name
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

    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

    if not INPUT_ROOT.is_dir():
        sys.exit(f"input folder not found: {INPUT_ROOT}")

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
        # zip lives at output/<Champion>/<disk_name>/<disk_name>.zip;
        # champion is not known yet, so search with glob.
        existing = list(OUTPUT_ROOT.glob(f"*/{skin_dir.name}/{skin_dir.name}.zip"))
        if existing:
            log(f"skip (already has zip): {skin_dir.name}")
            continue
        pending.append(skin_dir)

    if not pending:
        log("nothing to do — all skins already zipped")
        return

    log(f"pending: {[p.name for p in pending]}")

    missing = [p.name for p in pending if p.name not in disk_to_entry]
    if missing:
        example = ", ".join(f'"{n}": "26.06"' for n in missing)
        sys.exit(
            f"versions.json missing entries for: {missing}\n"
            f"add them, e.g. {{{example}}}"
        )

    for skin_dir in pending:
        display_name, version = disk_to_entry[skin_dir.name]
        process_skin(skin_dir, display_name, version)

    log("all done.")


if __name__ == "__main__":
    main()
