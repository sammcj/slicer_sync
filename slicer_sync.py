#!/usr/bin/env python3
"""slicer_sync: compare Creality Print profiles against OrcaSlicer user presets
and selectively mirror Creality's values across.

Two modes:
  Batch (no --name): walks every user printer profile + every filament profile
    that applies to the configured printer model. Per-profile prompts let you
    apply all / inspect field-by-field / skip / quit. Use --skip-printer or
    --skip-filament to limit scope.
  Single (--name X): processes just that profile, type inferred or set by --type.

Per-profile flow:
  1. Find the OrcaSlicer user preset (default/ + <UUID>/ cloud dirs); for filaments
     without a user override, fall back to the bundled system profile read-only.
  2. Resolve the inheritance chain on both sides for an apples-to-apples diff.
  3. Filter out KAMP-K2, user-specific, identity, and known-noise fields.
  4. Interactive walk: y / n / a / s / q / d=show-full per field.
  5. Dated backup of every target file before writing.

Examples:
  ./slicer_sync.py                                        # batch: printer + filaments
  ./slicer_sync.py --skip-filament                        # batch: printer only
  ./slicer_sync.py --skip-printer                         # batch: filaments only
  ./slicer_sync.py --list                                 # list available presets
  ./slicer_sync.py --name "Creality K2 Pro 0.4 nozzle - Sam"   # single profile
  ./slicer_sync.py --type filament --name "Creality Generic PLA @K2-all"
  ./slicer_sync.py --preview-only                         # just show the diffs

Adapting to a different printer:
  Edit the PRINTER_ANCHOR_NAMES / CP_PRINTER_MODEL / CP_NOZZLE / FILAMENT_SUFFIXES
  / ORCA_BRAND_PREFIX constants near the top of this file. Paths to the slicer
  data dirs (CP_SYSTEM / OS_BUNDLED / OS_USER) are also configurable there.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

HOME = Path.home()
CP_SYSTEM = HOME / "Library/Application Support/Creality/Creality Print/7.0/system/Creality"
OS_BUNDLED = Path("/Applications/OrcaSlicer.app/Contents/Resources/profiles/Creality")
OS_USER = HOME / "Library/Application Support/OrcaSlicer/user"

# ---------------------------------------------------------------------------
# Printer-family settings -- adjust these for your printer if not on K2 Pro.
# ---------------------------------------------------------------------------

# Batch mode anchors filament discovery on the first of these printer profiles
# that exists. Put your user override first, bundled system name second.
PRINTER_ANCHOR_NAMES = [
    "Creality K2 Pro 0.4 nozzle - Sam",
    "Creality K2 Pro 0.4 nozzle",
]

# Used in filament mode to derive the matching Creality Print filename --
# CP filaments are named '<Material> @<CP_PRINTER_MODEL> <CP_NOZZLE> nozzle'.
# Also the default for --cp-printer-model / --cp-nozzle CLI flags.
CP_PRINTER_MODEL = "Creality K2 Pro"
CP_NOZZLE = "0.4"

# OrcaSlicer's per-family filament suffixes. Used by bundled-fallback
# filament discovery (when the printer model has no default_materials list).
FILAMENT_SUFFIXES = ("@K2-all", "@K2")

# Brand prefix OrcaSlicer uses on bundled Creality filament names. Stripped
# when deriving the matching CP name, which doesn't include it.
ORCA_BRAND_PREFIX = "Creality "

# Profile names containing any of these substrings (case-insensitive) are
# skipped in batch mode. Useful for special-purpose profiles you never want
# auto-synced (e.g. retraction-disabled variants, calibration profiles).
# Single-profile mode (--name) ignores this list.
BATCH_SKIP_PATTERNS = [
    "NO RETRACTIONS",
]

# KAMP-K2 critical fields. machine_start_gcode is protected only when the
# current value contains the KAMP marker, so users who've reverted KAMP can
# still sync the stock start gcode.
KAMP_FIELDS = {
    "bed_mesh_min",
    "bed_mesh_max",
    "bed_mesh_probe_distance",
    "adaptive_bed_mesh_margin",
    "machine_start_gcode",
}

# Network connection and printer identity inside OrcaSlicer.
USER_FIELDS = {
    "print_host",
    "printer_agent",
    "printhost_apikey",
    "printhost_authorization_type",
    "printhost_ssl_ignore_revoke",
    "printer_settings_id",
    "bbl_use_printhost",
}

# Preset identity / structural fields -- never sync.
IDENTITY_FIELDS = {
    "name",
    "inherits",
    "from",
    "setting_id",
    "version",
    "type",
    "instantiation",
}

# Fields hidden by default -- pass --no-skip to surface. Maps field name to
# a short reason shown in the summary. Covers both cosmetic format differences
# and known user preferences where the user has deliberately set a value
# different from CP's default and doesn't want to be asked every sync.
SKIP_FIELDS = {
    "default_filament_profile": "cosmetic UI default",
    "default_print_profile": "cosmetic UI default",
    "thumbnails": "format-only difference",
    "support_chamber_temp_control": "user preference (enclosed printer)",
}

# Filament-specific protected fields (only checked when --type filament).
# Anything that defines the SCOPE of a filament (which printers/processes it
# applies to) or carries vendor/user identity that shouldn't propagate.
FILAMENT_PROTECTED_FIELDS = {
    "compatible_printers": "printer scope -- per-printer compatibility",
    "compatible_printers_condition": "printer scope -- compatibility expression",
    "compatible_prints": "process scope",
    "compatible_prints_condition": "process scope",
    "filament_id": "vendor internal id",
    "filament_settings_id": "filament preset identity",
    "filament_notes": "user notes field",
    "default_filament_colour": "cosmetic colour",
}

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"


def c(text: Any, colour: str) -> str:
    if sys.stdout.isatty():
        return f"{colour}{text}{RESET}"
    return str(text)


def normalise(v: Any) -> str:
    """Stringify for comparison. Collapses single-element lists and uniform
    multi-element lists, because OrcaSlicer often stores [N, N] (normal,
    silent mode) where Creality Print stores a single N -- semantically
    identical for single-extruder, single-mode use."""
    if isinstance(v, list) and len(v) >= 1:
        first = str(v[0])
        if all(str(x) == first for x in v):
            return first
    if isinstance(v, list):
        return ",".join(str(x) for x in v)
    return str(v)


def is_orca_running() -> bool:
    try:
        r = subprocess.run(
            ["pgrep", "-x", "OrcaSlicer"],
            capture_output=True,
            timeout=5,
        )
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def find_user_copies(kind: str, name: str) -> list[Path]:
    """Every OrcaSlicer user preset file with this name (default + any cloud UUID dirs)."""
    out: list[Path] = []
    if not OS_USER.exists():
        return out
    for sub in sorted(OS_USER.iterdir()):
        if not sub.is_dir():
            continue
        candidate = sub / kind / f"{name}.json"
        if candidate.exists():
            out.append(candidate)
    return out


def find_cp_preset(kind: str, name: str) -> Path | None:
    candidate = CP_SYSTEM / kind / f"{name}.json"
    return candidate if candidate.exists() else None


def find_orca_system_preset(kind: str, name: str) -> Path | None:
    candidate = OS_BUNDLED / kind / f"{name}.json"
    return candidate if candidate.exists() else None


def list_presets(kind: str) -> None:
    print(c(f"\n=== Creality Print {kind} presets ===", BOLD))
    cp_dir = CP_SYSTEM / kind
    if cp_dir.exists():
        for p in sorted(cp_dir.glob("*.json")):
            print(f"  {p.stem}")
    else:
        print(c(f"  (not found: {cp_dir})", DIM))

    print(c(f"\n=== OrcaSlicer user {kind} presets ===", BOLD))
    if OS_USER.exists():
        for sub in sorted(OS_USER.iterdir()):
            if not sub.is_dir():
                continue
            kdir = sub / kind
            if not kdir.exists():
                continue
            print(c(f"  [{sub.name}]", DIM))
            for p in sorted(kdir.glob("*.json")):
                print(f"    {p.stem}")
    else:
        print(c(f"  (not found: {OS_USER})", DIM))


def resolve_inheritance(
    path: Path,
    search_dirs: list[Path],
    visited: set[Path] | None = None,
) -> dict:
    """Load JSON and recursively merge in parent values via the `inherits` field.
    Parent values are overridden by child values."""
    if visited is None:
        visited = set()
    rpath = path.resolve()
    if rpath in visited:
        return {}
    visited.add(rpath)

    with open(path) as f:
        data = json.load(f)

    parent_name = data.get("inherits")
    if not parent_name:
        return data

    for d in search_dirs:
        parent = d / f"{parent_name}.json"
        if parent.exists():
            parent_data = resolve_inheritance(parent, search_dirs, visited)
            return {**parent_data, **data}

    # Parent not found; return child only and warn once.
    print(c(f"  warning: parent '{parent_name}' not found for {path.name}", YELLOW))
    return data


def categorise(field: str, orca_value: Any, profile_type: str = "machine") -> str | None:
    """Returns a reason string if the field is protected, else None."""
    if field in IDENTITY_FIELDS:
        return "identity"
    if profile_type == "filament" and field in FILAMENT_PROTECTED_FIELDS:
        return FILAMENT_PROTECTED_FIELDS[field]
    if profile_type == "machine":
        if field in USER_FIELDS:
            return "user-specific"
        if field in KAMP_FIELDS:
            if field == "machine_start_gcode":
                if orca_value is not None and "LINE_PURGE" in normalise(orca_value):
                    return "KAMP start_gcode"
                return None
            return "KAMP setting"
    return None


def cp_filament_name_for(orca_name: str, printer_model: str, nozzle: str) -> str:
    """Derive the matching Creality Print filament name from an Orca name.

    Orca uses '<ORCA_BRAND_PREFIX><Material> @<family>' (one file per material,
    covers multiple nozzles via compatible_printers). CP uses
    '<Material> @<printer> <nozzle> nozzle' (one file per material per nozzle).
    Mapping strips the @<...> suffix, drops the brand prefix, and appends the
    CP-style printer/nozzle suffix."""
    base = orca_name.split(" @", 1)[0]
    if base.startswith(ORCA_BRAND_PREFIX):
        base = base[len(ORCA_BRAND_PREFIX):]
    return f"{base} @{printer_model} {nozzle} nozzle"


def is_noise_diff(field: str, ov: Any, cv: Any) -> str | None:
    """Detect format-only / cosmetic / known-preference differences.
    Returns a short reason, or None. Works on normalised values, not raw
    types, so it catches cases like Orca [0, 0] (uniform list -> "0") vs
    CP "0,0" (comma string)."""
    if field in SKIP_FIELDS:
        return SKIP_FIELDS[field]
    ov_norm = normalise(ov)
    cv_norm = normalise(cv)
    # Orca's [normal, silent] two-value tuple where CP's value matches normal mode.
    if isinstance(ov, list) and len(ov) == 2 and str(ov[0]) == cv_norm:
        return "orca [normal,silent] matches cp single"
    # CP serialises "X,X,..." while Orca has a single X.
    if "," in cv_norm and "," not in ov_norm:
        parts = [p.strip() for p in cv_norm.split(",")]
        if len(parts) > 1 and all(p == ov_norm for p in parts):
            return "cp comma-string matches orca scalar"
    # Mirror: Orca serialises comma-string, CP has scalar.
    if "," in ov_norm and "," not in cv_norm:
        parts = [p.strip() for p in ov_norm.split(",")]
        if len(parts) > 1 and all(p == cv_norm for p in parts):
            return "orca comma-string matches cp scalar"
    return None


def diff_profiles(orca: dict, cp: dict) -> dict[str, tuple[str, Any, Any]]:
    """Returns {field: (kind, orca_value, cp_value)} where kind is one of:
    'differ', 'orca-missing', 'cp-missing'."""
    out: dict[str, tuple[str, Any, Any]] = {}
    for field in sorted(set(orca) | set(cp)):
        if field in orca and field in cp:
            if normalise(orca[field]) != normalise(cp[field]):
                out[field] = ("differ", orca[field], cp[field])
        elif field in cp:
            out[field] = ("orca-missing", None, cp[field])
        else:
            out[field] = ("cp-missing", orca[field], None)
    return out


def backup(path: Path) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = path.with_name(path.name + f".bak.{ts}")
    shutil.copy2(path, bak)
    return bak


def fmt(v: Any, maxlen: int = 90) -> str:
    if v is None:
        return c("(absent)", DIM)
    s = normalise(v)
    if "\n" in s:
        # Multi-line: show first line + count of remaining
        lines = s.split("\n")
        s = f"{lines[0]}  +{len(lines)-1} more lines"
    if len(s) > maxlen:
        s = s[: maxlen - 3] + "..."
    return s


def prompt_field(idx: int, total: int, field: str, kind: str, ov: Any, cv: Any) -> str:
    """Return one of: y, n, a (all remaining yes), s (skip rest), q (abort), d (show full diff)."""
    label = {"differ": "DIFFER", "orca-missing": "NEW   "}[kind]
    print(f"\n  {c(f'[{idx}/{total}]', DIM)} {c(label, YELLOW)}  {c(field, BOLD)}")
    print(f"    {c('orca:', BLUE)} {fmt(ov)}")
    print(f"    {c('cp:  ', YELLOW)} {fmt(cv)}")
    while True:
        choice = input(
            f"    Apply CP value? [{c('y', GREEN)}/n/a/s/q/d=show-full] "
        ).strip().lower()
        if choice == "":
            choice = "n"
        if choice == "d":
            print(f"\n    {c('orca full:', BLUE)}")
            print(_indent(normalise(ov) if ov is not None else "(absent)", "      "))
            print(f"\n    {c('cp full:  ', YELLOW)}")
            print(_indent(normalise(cv) if cv is not None else "(absent)", "      "))
            continue
        if choice in ("y", "n", "a", "s", "q"):
            return choice
        print(c("    invalid choice", RED))


def _indent(text: str, prefix: str) -> str:
    return "\n".join(prefix + line for line in text.split("\n"))


def write_changes(target: Path, changes: dict[str, Any], dry_run: bool, do_backup: bool) -> None:
    with open(target) as f:
        data = json.load(f)
    for field, cv in changes.items():
        data[field] = cv

    if dry_run:
        print(f"  {c('[dry-run]', DIM)} would write {len(changes)} field(s) to {target}")
        return

    if do_backup:
        bak = backup(target)
        print(f"  backed up to {c(bak.name, DIM)}")

    with open(target, "w") as f:
        json.dump(data, f, indent="\t", ensure_ascii=False, sort_keys=True)
        f.write("\n")
    print(f"  {c('wrote', GREEN)} {len(changes)} field(s) to {target}")


def discover_user_printer_profiles() -> list[str]:
    """Find every unique printer profile name across the user dirs."""
    names: set[str] = set()
    if not OS_USER.exists():
        return []
    for sub in sorted(OS_USER.iterdir()):
        if not sub.is_dir():
            continue
        machine_dir = sub / "machine"
        if not machine_dir.exists():
            continue
        for f in machine_dir.glob("*.json"):
            names.add(f.stem)
    return sorted(names)


def discover_relevant_filaments(printer_profile_path: Path) -> list[str]:
    """Read the printer's model file `default_materials` field for the canonical
    list of filaments that apply. Falls back to globbing bundled filaments
    matching FILAMENT_SUFFIXES if the model file isn't found or has no
    default_materials."""
    if not printer_profile_path.exists():
        return []

    # Walk inherits to find a profile that declares a printer_model.
    search = [OS_BUNDLED / "machine"]
    if OS_USER.exists():
        for sub in OS_USER.iterdir():
            if sub.is_dir():
                search.append(sub / "machine")
    resolved = resolve_inheritance(printer_profile_path, search)
    printer_model = resolved.get("printer_model")
    if printer_model:
        model_file = OS_BUNDLED / "machine" / f"{printer_model}.json"
        if model_file.exists():
            try:
                with open(model_file) as f:
                    model_data = json.load(f)
                materials = model_data.get("default_materials", "")
                if materials:
                    return [m.strip() for m in materials.split(";") if m.strip()]
            except (OSError, json.JSONDecodeError):
                pass

    # Fallback: bundled filaments matching the configured suffixes.
    fdir = OS_BUNDLED / "filament"
    if not fdir.exists():
        return []
    names: set[str] = set()
    for suffix in FILAMENT_SUFFIXES:
        for f in fdir.glob(f"*{suffix}.json"):
            names.add(f.stem)
    return sorted(names)


def filter_batch_names(names: list[str], kind: str) -> list[str]:
    """Drop names matching BATCH_SKIP_PATTERNS, announcing each skip."""
    kept = []
    for name in names:
        matched = [p for p in BATCH_SKIP_PATTERNS if p.lower() in name.lower()]
        if matched:
            print(c(f"  skipping {kind} '{name}' (matches BATCH_SKIP_PATTERNS: {', '.join(matched)})", DIM))
        else:
            kept.append(name)
    return kept


def prompt_profile_action(label: str, eligible_count: int) -> str:
    """Per-profile prompt in batch mode. Returns 'a'/'i'/'s'/'q'."""
    print(
        f"\n  {c(label, BOLD)}: {eligible_count} eligible diff(s). "
        f"[{c('a', GREEN)}=apply all / {c('i', BOLD)}=inspect field-by-field / "
        f"{c('s', YELLOW)}=skip this profile / {c('q', RED)}=quit batch]"
    )
    while True:
        choice = input("  > ").strip().lower()
        if choice in ("a", "i", "s", "q"):
            return choice
        if choice == "":
            return "i"  # default to inspect
        print(c("    invalid choice", RED))


def run(args: argparse.Namespace) -> int:
    if args.list:
        list_presets(args.type or "machine")
        if not args.type:
            list_presets("filament")
        return 0

    # Single-profile mode: --name given.
    if args.name:
        code, _ = process_profile(args, args.type or "machine", args.name, batch_mode=False)
        return code

    # Batch mode: walk printers then filaments.
    if not args.dry_run and is_orca_running():
        print(c("WARNING: OrcaSlicer appears to be running.", RED))
        print("If it's open, it may overwrite your edits on exit. Quit it first.")
        ans = input("Continue anyway? [y/N] ").strip().lower()
        if ans != "y":
            return 1

    did_any = False
    # --type narrows batch mode just like the explicit --skip-* flags.
    skip_printer = args.skip_printer or args.type == "filament"
    skip_filament = args.skip_filament or args.type == "machine"

    if not skip_printer:
        printer_names = filter_batch_names(discover_user_printer_profiles(), "printer")
        if not printer_names:
            print(c("No user printer profiles found -- skipping printer batch.", DIM))
        for i, name in enumerate(printer_names, 1):
            print(c(f"\n{'='*70}", BOLD))
            print(c(f"[printer {i}/{len(printer_names)}] {name}", BOLD))
            print(c("="*70, BOLD))
            code, quit_batch = process_profile(args, "machine", name, batch_mode=True)
            did_any = True
            if quit_batch:
                return 0

    if not skip_filament:
        # Anchor filament discovery on the first PRINTER_ANCHOR_NAMES entry
        # that exists (user override preferred, bundled system as fallback).
        anchor_paths: list[Path] = []
        for candidate in PRINTER_ANCHOR_NAMES:
            anchor_paths = find_user_copies("machine", candidate)
            if anchor_paths:
                break
            bundled = find_orca_system_preset("machine", candidate)
            if bundled:
                anchor_paths = [bundled]
                break
        if not anchor_paths:
            print(c(
                f"Couldn't find any of {PRINTER_ANCHOR_NAMES} to anchor filament "
                "discovery; skipping filament batch.",
                DIM,
            ))
        else:
            filaments = filter_batch_names(discover_relevant_filaments(anchor_paths[0]), "filament")
            if not filaments:
                print(c("No filaments discovered for the K2 Pro; skipping filament batch.", DIM))
            for i, name in enumerate(filaments, 1):
                print(c(f"\n{'='*70}", BOLD))
                print(c(f"[filament {i}/{len(filaments)}] {name}", BOLD))
                print(c("="*70, BOLD))
                code, quit_batch = process_profile(args, "filament", name, batch_mode=True)
                did_any = True
                if quit_batch:
                    return 0

    if not did_any:
        print(c("\nNothing to do (both --skip-printer and --skip-filament passed?).", YELLOW))
        return 1

    return 0


def process_profile(args: argparse.Namespace, profile_type: str, name: str, batch_mode: bool = False) -> tuple[int, bool]:
    """Run the sync workflow for a single profile. Returns (exit_code, quit_batch)."""
    read_only = False
    user_copies = find_user_copies(profile_type, name)
    if not user_copies:
        if args.bundled or profile_type == "filament":
            # Filaments rarely have user overrides; fall back to the bundled
            # system profile in read-only mode (diff only, no write).
            bundled = find_orca_system_preset(profile_type, name)
            if bundled:
                user_copies = [bundled]
                read_only = True
            else:
                if not batch_mode:
                    print(c(f"No preset found anywhere named '{name}'", RED))
                    print(c(f"Searched user dirs ({OS_USER}/<*>/{profile_type}/) and bundled ({OS_BUNDLED}/{profile_type}/).", DIM))
                    print(c("Run with --list to see available names.", DIM))
                else:
                    print(c(f"  no preset found, skipping", DIM))
                return 1, False
        else:
            print(c(f"No OrcaSlicer user preset found: {name}", RED))
            print(c(f"Searched: {OS_USER}/<*>/{profile_type}/", DIM))
            print(c("Pass --bundled to compare against the bundled system profile read-only.", DIM))
            print(c("Run with --list to see available names.", DIM))
            return 1, False

    primary = user_copies[0]
    with open(primary) as f:
        user_data = json.load(f)

    if args.cp_name and not batch_mode:
        cp_name = args.cp_name
    elif profile_type == "filament":
        cp_name = cp_filament_name_for(name, args.cp_printer_model, args.cp_nozzle)
    else:
        cp_name = user_data.get("inherits") or name
    cp_path = find_cp_preset(profile_type, cp_name)
    if not cp_path:
        if batch_mode:
            print(c(f"  no CP equivalent for '{cp_name}', skipping", DIM))
        else:
            print(c(f"No Creality Print preset found: {cp_name}", RED))
            print(c(f"Searched: {CP_SYSTEM}/{profile_type}/", DIM))
            if profile_type == "filament" and not args.cp_name:
                print(c("Auto-derived CP name from --name + --cp-printer-model + --cp-nozzle.", DIM))
                print(c("Override with --cp-name if your CP filament uses a different naming convention.", DIM))
        return 1, False

    src_label = "OrcaSlicer bundled" if read_only else "OrcaSlicer user"
    print(f"{c(src_label + ' preset:', BLUE)} {primary}")
    if len(user_copies) > 1:
        print(c(f"  (will also sync to {len(user_copies)-1} other copy/copies):", DIM))
        for p in user_copies[1:]:
            print(c(f"    {p}", DIM))
    print(f"{c('Creality Print preset:', YELLOW)} {cp_path}")
    if read_only:
        print(c("  read-only: bundled OrcaSlicer profile won't be modified.", DIM))

    if not batch_mode and not args.dry_run and is_orca_running():
        print(c("\nWARNING: OrcaSlicer appears to be running.", RED))
        print("If it's open, it may overwrite your edits on exit. Quit it first.")
        ans = input("Continue anyway? [y/N] ").strip().lower()
        if ans != "y":
            return 1, False

    # Inheritance search paths (parents typically live in OS_BUNDLED, but a
    # user override could theoretically introduce a parent chain too).
    orca_search = [OS_BUNDLED / profile_type]
    if OS_USER.exists():
        for sub in OS_USER.iterdir():
            if sub.is_dir():
                orca_search.append(sub / profile_type)
    cp_search = [CP_SYSTEM / profile_type]

    print(c("\nResolving inheritance...", DIM))
    orca_resolved = resolve_inheritance(primary, orca_search)
    cp_resolved = resolve_inheritance(cp_path, cp_search)
    print(c(f"  orca: {len(orca_resolved)} fields  cp: {len(cp_resolved)} fields", DIM))

    diffs = diff_profiles(orca_resolved, cp_resolved)
    if not diffs:
        print(c("No differences. Profiles in sync.", GREEN))
        return 0, False

    eligible: dict[str, tuple[str, Any, Any]] = {}
    new_only: dict[str, tuple[str, Any, Any]] = {}
    noise: dict[str, tuple[Any, Any, str]] = {}
    protected: dict[str, tuple[Any, Any, str]] = {}
    orca_only: dict[str, Any] = {}

    for field, (kind, ov, cv) in diffs.items():
        if kind == "cp-missing":
            # Orca has a value CP doesn't -- usually means a KAMP/Sam-only field.
            # Categorise so KAMP entries surface in the protected list as info.
            reason = categorise(field, ov, profile_type)
            if reason:
                protected[field] = (ov, cv, reason)
            else:
                orca_only[field] = ov
            continue
        reason = categorise(field, ov, profile_type)
        if reason:
            protected[field] = (ov, cv, reason)
            continue
        noise_reason = is_noise_diff(field, ov, cv)
        if noise_reason and not args.no_skip:
            noise[field] = (ov, cv, noise_reason)
            continue
        if kind == "orca-missing":
            # CP serialises this field; Orca leaves it to inherit/default. Usually
            # noise (matches Orca's runtime default), occasionally a genuine new
            # feature. Hidden by default; --include-new surfaces them.
            new_only[field] = (kind, ov, cv)
        else:
            eligible[field] = (kind, ov, cv)

    if args.include_new:
        eligible.update(new_only)

    print(f"\n{c('Summary', BOLD)}")
    print(f"  {c(str(len(eligible)), GREEN)} eligible to sync (real value differences)")
    if not args.no_skip and noise:
        print(
            f"  {c(str(len(noise)), DIM)} cosmetic / format-only diffs hidden "
            f"-- pass {c('--no-skip', BOLD)} to surface them"
        )
    if not args.include_new and new_only:
        print(
            f"  {c(str(len(new_only)), DIM)} 'CP has, Orca doesn't' fields hidden "
            f"-- pass {c('--include-new', BOLD)} to surface them"
        )
    print(f"  {c(str(len(protected)), YELLOW)} protected (KAMP / user / identity)")
    print(f"  {c(str(len(orca_only)), DIM)} only in Orca (not offered)")

    if protected and args.show_protected:
        print(f"\n{c('Protected fields (not offered):', DIM)}")
        for field, (ov, cv, reason) in sorted(protected.items()):
            print(f"  - {c(field, DIM)} [{reason}]")
            print(f"      orca: {fmt(ov)}")
            print(f"      cp:   {fmt(cv)}")

    if not eligible:
        print(c("Nothing eligible to sync.", YELLOW))
        return 0, False

    items = sorted(eligible.items())

    # Preview: list every diff up front so the user (or a reviewer) sees the
    # whole picture before being asked field-by-field.
    print(f"\n{c('All eligible differences:', BOLD)}")
    field_w = max(len(f) for f, _ in items)
    for i, (field, (kind, ov, cv)) in enumerate(items, 1):
        tag = c("DIFFER", YELLOW) if kind == "differ" else c("NEW   ", DIM)
        ov_s = fmt(ov, maxlen=40) if ov is not None else c("(absent)", DIM)
        cv_s = fmt(cv, maxlen=40)
        print(f"  {i:>3}. [{tag}] {field:<{field_w}}  {c('orca=', BLUE)}{ov_s}  {c('cp=', YELLOW)}{cv_s}")

    if args.preview_only:
        print(c("\n--preview-only: stopping before prompts.", DIM))
        return 0, False

    selected: dict[str, Any] = {}
    apply_remaining = args.auto_yes

    # Batch mode: whole-profile prompt before per-field walk.
    if batch_mode and not apply_remaining:
        label = f"{profile_type}: {name}"
        choice = prompt_profile_action(label, len(items))
        if choice == "s":
            return 0, False
        if choice == "q":
            return 0, True
        if choice == "a":
            apply_remaining = True

    print(f"\n{c('Stepping through prompts...', BOLD)}")
    for i, (field, (kind, ov, cv)) in enumerate(items, 1):
        if apply_remaining:
            selected[field] = cv
            continue
        choice = prompt_field(i, len(items), field, kind, ov, cv)
        if choice == "y":
            selected[field] = cv
        elif choice == "n":
            pass
        elif choice == "a":
            selected[field] = cv
            apply_remaining = True
        elif choice == "s":
            break
        elif choice == "q":
            print(c("\nAborted, no changes written.", YELLOW))
            return 0, batch_mode  # in batch, quit signals full exit

    if not selected:
        print(c("Nothing selected. No changes made.", YELLOW))
        return 0, False

    if read_only:
        print(c("\nRead-only mode: showing selected changes but not writing.", YELLOW))
        print(c("To apply, create a user override in Orca's UI (Save As) then re-run without --bundled.", DIM))
        for field, cv in selected.items():
            print(f"  would set {c(field, BOLD)} = {fmt(cv)}")
        return 0, False

    print(f"\n{c('Applying:', BOLD)} {len(selected)} field(s) to {len(user_copies)} file(s)")
    for target in user_copies:
        write_changes(target, selected, args.dry_run, do_backup=not args.no_backup)

    return 0, False


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--name",
        help="OrcaSlicer user preset name (no .json extension). If omitted, runs "
        "batch mode: every user printer profile + every K2 Pro filament profile.",
    )
    p.add_argument(
        "--cp-name",
        help="Override the Creality Print preset name (single-profile mode only; "
        "default: read from user preset's 'inherits' field, or auto-derive for filament).",
    )
    p.add_argument(
        "--type",
        choices=["machine", "filament"],
        default=None,
        help="Profile type for single-profile mode (default: machine when --name given). "
        "Ignored in batch mode.",
    )
    p.add_argument("--list", action="store_true", help="List available presets and exit")
    p.add_argument(
        "--skip-printer",
        action="store_true",
        help="Batch mode: skip printer profiles, do filaments only.",
    )
    p.add_argument(
        "--skip-filament",
        action="store_true",
        help="Batch mode: skip filament profiles, do printer only.",
    )
    p.add_argument("--dry-run", action="store_true", help="Show what would change without writing")
    p.add_argument(
        "--auto-yes",
        action="store_true",
        help="Apply every eligible change without prompting (KAMP/user/identity still protected)",
    )
    p.add_argument("--no-backup", action="store_true", help="Skip dated backup (not recommended)")
    p.add_argument(
        "--bundled",
        action="store_true",
        help="Compare against the bundled OrcaSlicer system profile read-only. "
        "Useful when no user override exists yet -- you can see the diff and "
        "decide whether to create one via Orca's UI. Auto-on for --type filament.",
    )
    p.add_argument(
        "--cp-printer-model",
        default=CP_PRINTER_MODEL,
        help="Printer model used to derive the CP filament name (default: %(default)s). "
        "Filament mode only.",
    )
    p.add_argument(
        "--cp-nozzle",
        default=CP_NOZZLE,
        help="Nozzle size used to derive the CP filament name (default: %(default)s). "
        "Filament mode only.",
    )
    p.add_argument(
        "--show-protected",
        action="store_true",
        help="Print the protected-fields list as informational output",
    )
    p.add_argument(
        "--include-new",
        action="store_true",
        help="Also offer fields that CP serialises but Orca doesn't have. Usually "
        "noise (CP writing values that match Orca's runtime defaults), occasionally "
        "a genuine new feature. Default: skip these.",
    )
    p.add_argument(
        "--no-skip",
        action="store_true",
        help="Surface cosmetic, format-only, and known-user-preference differences "
        "(e.g. orca's [normal,silent] tuple matching cp's single value, cp's "
        "comma-string matching orca's scalar, default_*_profile UI defaults, "
        "thumbnail format, support_chamber_temp_control). Default: hide.",
    )
    p.add_argument(
        "--preview-only",
        action="store_true",
        help="Print the full list of differences and exit without prompting. "
        "Useful for sharing the diff for review before deciding what to sync.",
    )
    args = p.parse_args()

    try:
        return run(args)
    except KeyboardInterrupt:
        print(c("\nInterrupted. No changes written.", YELLOW))
        return 130


if __name__ == "__main__":
    sys.exit(main())
