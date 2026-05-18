# slicer_sync

Compares Creality Print profiles against your OrcaSlicer user presets, then lets you selectively mirror Creality's values across. Built around the Creality K2 Pro but the printer-family constants at the top of `slicer_sync.py` make it adaptable to any printer.

## What it solves

Creality Print pushes profile updates periodically (tuning changes, new fields for CFS features, etc.). OrcaSlicer's bundled Creality profiles lag behind. Manually diffing the JSONs is tedious because:

- OrcaSlicer stores values as `[N, N]` tuples (normal/silent mode); Creality Print stores `N`.
- Creality Print serialises every default explicitly; OrcaSlicer leaves them to inherit.
- Both sides use `inherits` chains, so a flat key-by-key diff is full of false positives.
- Real preferences (KAMP bed mesh bounds, chamber temp control, network creds) must never be overwritten.

This script resolves both inheritance chains, normalises format-only differences, protects KAMP/identity/user-specific fields, and walks the actual signal interactively.

## Requirements

- macOS (paths in `slicer_sync.py` are macOS-specific; trivially adaptable to Linux/Windows by changing the four path constants at the top)
- Python 3.10+ (uses `tuple[...]` / `str | None` syntax)
- OrcaSlicer and Creality Print both installed with their profile data present

No external Python packages. Pure stdlib.

## Setup

```sh
git clone <this repo>
cd slicer_sync
chmod +x slicer_sync.py
./slicer_sync.py --list   # smoke test - shows discovered presets on both sides
```

## Usage

**Batch mode** (default when no `--name` is passed) walks every user printer profile and every filament profile applicable to your printer:

```sh
./slicer_sync.py                       # batch: printer + filaments
./slicer_sync.py --skip-filament       # batch: printer only
./slicer_sync.py --skip-printer        # batch: filaments only
./slicer_sync.py --type machine        # same as --skip-filament
./slicer_sync.py --type filament       # same as --skip-printer
```

**Single-profile mode**:

```sh
./slicer_sync.py --name "Creality K2 Pro 0.4 nozzle - Sam"
./slicer_sync.py --type filament --name "Creality Generic PLA @K2-all"
./slicer_sync.py --name "..." --cp-name "Generic PLA @Creality K2 Pro 0.6 nozzle"
```

**Preview-only** (just print the diffs, no prompts):

```sh
./slicer_sync.py --preview-only
./slicer_sync.py --name "..." --preview-only --show-protected
```

**Filter behaviour**:

```sh
./slicer_sync.py --no-skip          # show cosmetic/format-only/user-preference diffs too
./slicer_sync.py --include-new      # show "CP has, Orca doesn't" fields too (usually noise)
./slicer_sync.py --no-skip --include-new   # kitchen sink view
```

**Per-profile prompt** (batch mode only): after the summary and preview list for each profile, you get `[a/i/s/q]`:

- `a` apply all eligible diffs to this profile, move to next
- `i` inspect field-by-field (drops into per-field `y/n/a/s/q/d` walk)
- `s` skip this profile, move to next
- `q` quit batch entirely

## Filtering rationale

The default view filters aggressively to show only real signal. Three filters, each independently disable-able:

| Filter | Default | Disable with | What it hides |
|---|---|---|---|
| Protection | always on | (not configurable; safety) | KAMP-K2 bed mesh, network creds, identity fields |
| Skip-list | on | `--no-skip` | Cosmetic UI defaults, format-only differences, known user preferences |
| New-only | on | `--include-new` | Fields CP serialises that OrcaSlicer inherits |

The protected and skip-list sets are defined at the top of `slicer_sync.py`:

- `KAMP_FIELDS`: bed_mesh bounds + start_gcode (start_gcode only protected if it contains `LINE_PURGE`)
- `USER_FIELDS`: print_host, printer_agent, printhost_apikey, etc.
- `IDENTITY_FIELDS`: name, inherits, from, setting_id, version, etc.
- `SKIP_FIELDS`: cosmetic and known-preference fields, with per-field reason
- `FILAMENT_PROTECTED_FIELDS`: compatible_printers, filament_id, etc. (filament mode only)

## Safety

- **Dated backup** of every target file before write: `<file>.bak.YYYYMMDD_HHMMSS` next to the original. Disable with `--no-backup` (not recommended).
- **OrcaSlicer-running check** before any write. Prompts to continue if Orca is detected.
- **KAMP guard**: fields that would clobber your adaptive bed mesh setup (`bed_mesh_min`, `bed_mesh_max`, `bed_mesh_probe_distance`, `adaptive_bed_mesh_margin`) are protected unconditionally. The 5-line KAMP `machine_start_gcode` is detected by its `LINE_PURGE` marker.
- **Both user-preset copies** (default/ + cloud UUID/) are updated together so the cloud-synced and offline-fallback copies stay consistent.
- **`--dry-run`** writes nothing, just reports what it would do.

## Adapting for a different printer

Edit the constants in the configurable block near the top of `slicer_sync.py`:

```python
PRINTER_ANCHOR_NAMES = [
    "Creality K2 Pro 0.4 nozzle - Sam",   # your user override
    "Creality K2 Pro 0.4 nozzle",         # bundled system fallback
]
CP_PRINTER_MODEL = "Creality K2 Pro"     # how CP names the printer in filament filenames
CP_NOZZLE = "0.4"
FILAMENT_SUFFIXES = ("@K2-all", "@K2")   # OrcaSlicer's per-family filament suffixes
ORCA_BRAND_PREFIX = "Creality "          # stripped when mapping to CP names
BATCH_SKIP_PATTERNS = ["NO RETRACTIONS"] # profile-name substrings to skip in batch
```

For a non-macOS platform also adjust:

```python
CP_SYSTEM = HOME / "Library/Application Support/Creality/Creality Print/7.0/system/Creality"
OS_BUNDLED = Path("/Applications/OrcaSlicer.app/Contents/Resources/profiles/Creality")
OS_USER = HOME / "Library/Application Support/OrcaSlicer/user"
```

## Limitations

- **Filament import not implemented**: only diffs and updates filaments that already exist on the OrcaSlicer side. Creality Print has many filaments OrcaSlicer doesn't (Hyper PLA, CR-PETG, HP-TPU, Soleyin variants, etc.); the script won't offer to create new Orca filaments from them.
- **Process profiles: protected list is intentionally minimal**: only identity + extruder bindings are auto-protected. User tweaks (speeds, infill density/pattern, wall generator, prime tower) show up in the diff so you can review per-field. Use `--preview-only` first if you're worried about clobbering tunes.
- **Process name mapping assumes CP's "Standard" convention**: Orca's varied descriptors (`Detail`/`Optimal`/`Standard`/`Draft`/`SuperDraft`) all map to CP's `<height> Standard ...`. If CP ever ships non-Standard process profiles you'd need to extend `cp_process_name_for()`.
- **macOS paths only out of the box**: change the path constants for other platforms.
- **`--auto-yes` will over-sync**: applies every eligible diff including any explicit-default noise that slipped through the filter. Use interactive mode if you care about each change.

## Flag reference

| Flag | Effect |
|---|---|
| `--list` | List discovered presets on both sides; exit |
| `--name X` | Single-profile mode for preset name X |
| `--type {machine,filament,process}` | Profile type; in batch mode also scopes which type to process |
| `--cp-name X` | Override the CP-side name (single-profile mode only) |
| `--cp-printer-model X` | Printer-model name used to derive CP filament names (default `CP_PRINTER_MODEL`) |
| `--cp-nozzle X` | Nozzle size used to derive CP filament names (default `CP_NOZZLE`) |
| `--bundled` | Compare against bundled OrcaSlicer profile read-only; auto-on for filaments |
| `--skip-printer` | Batch mode: skip printer profiles |
| `--skip-filament` | Batch mode: skip filament profiles |
| `--skip-process` | Batch mode: skip process (print quality) profiles |
| `--preview-only` | Print the diff and exit before any prompts |
| `--no-skip` | Surface cosmetic/format/preference-only diffs the default filter hides |
| `--include-new` | Surface fields CP has and Orca doesn't (usually noise) |
| `--show-protected` | Print the protected-fields list as informational output |
| `--auto-yes` | Apply every eligible diff without prompting |
| `--dry-run` | Don't write; report what would be written |
| `--no-backup` | Skip dated backup before writing (not recommended) |
