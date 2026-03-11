# NimBLE Compilation Coverage Checker — User Guide

## Overview

`tools/bt/nimble_compile_check.py` validates NimBLE compile coverage by:

- parsing `components/bt/host/nimble/Kconfig.in`
- generating sdkconfig variants from NimBLE boolean options
- building selected NimBLE examples across selected targets
- reporting pass/fail with per-variant logs

The script now includes multiple safety and policy layers (example rules, target rules, global constraints, log size control, build-dir cleanup) that were added after the initial version of this guide.

---

## Prerequisites

1. Source ESP-IDF environment:

```bash
cd /path/to/esp-idf
. ./export.sh
```

2. Python 3.6+ (stdlib only).

3. Toolchains installed for targets you build.

---

## Location

```text
tools/bt/nimble_compile_check.py
```

---

## Quick Start

```bash
# 1) Inspect parsed groups (no build)
python tools/bt/nimble_compile_check.py --list-groups

# 2) Small test run
python tools/bt/nimble_compile_check.py --groups ext_adv --examples bleprph --target esp32c3

# 3) Full default run
python tools/bt/nimble_compile_check.py
```

---

## Command Reference

```text
python tools/bt/nimble_compile_check.py [OPTIONS]
```

### Core options

| Option | Default | Description |
|---|---|---|
| `--idf-path PATH` | `$IDF_PATH` or auto-detected | ESP-IDF root |
| `--target TARGET` | `esp32c3` | Single target alias |
| `--targets T1,T2,...` | `esp32c3` | Multiple targets |
| `--all-targets` | off | Build all BLE-supported targets |
| `--groups G1,G2,...` | all groups | Restrict tested groups |
| `--examples E1,E2,...` | `bleprph,blecent` | Example list |
| `--all-examples` | off | Discover all top-level NimBLE examples (`examples/bluetooth/nimble/*`) |
| `--parallel N` | `1` | Parallel build count |
| `--output FILE` | stdout | Write summary report to file |
| `--keep-builds` | off | Keep build artifacts under temporary root |
| `--default-only` | off | Build each selected example once with default settings only |

### Inspection options

| Option | Description |
|---|---|
| `--list-flags` | Show parsed bool flags |
| `--list-groups` | Show grouped flags |
| `--list-examples` | Show discoverable examples |
| `--list-targets` | Show BLE-supported targets |

### Variant options

| Option | Default | Description |
|---|---|---|
| `--variant-mode group|flag-combos` | `group` | Variant generation strategy |
| `--combo-max-flags N` | `10` | Safety cap in `flag-combos` mode |
| `--combo-max-size N` | all sizes | Max combination size in `flag-combos` mode |
| `--custom KEY=VAL,...` | none | Build a single explicit variant |

---

## Variant Generation

### `--default-only` mode

When `--default-only` is enabled, variant generation is skipped and only one variant is used:

- `default` (no generated sdkconfig toggles)

Use this mode for baseline compile checks on one target across many examples.

### `group` mode (default)

Generates:

- `<group>_enable`: all `default=n` (non-choice) flags in the group -> `y`
- `<group>_disable`: all `default=y` (non-choice) flags in the group -> `n`
- `max_enable`: all `default=n` non-choice flags -> `y`
- `max_disable`: all `default=y` non-choice flags -> `n`

### `flag-combos` mode

Generates in order:

1. single-flag flip variants (`flag_single_*`)
2. multi-flag combinations (`flag_combo_*`)

Flip policy is always away from default:

- `default=n -> y`
- `default=y -> n`

Safety: if selected toggleable flags exceed `--combo-max-flags`, script exits with error.

### Mesh policy

All `BT_NIMBLE_MESH*` flags are ignored globally for variant generation (including `--custom`).

---

## Constraint System (Important)

Before scheduling a build task, script validates multiple rule layers.

### 1) Global constraints

- At least one role must remain enabled across:
  - `BT_NIMBLE_ROLE_CENTRAL`
  - `BT_NIMBLE_ROLE_PERIPHERAL`
  - `BT_NIMBLE_ROLE_BROADCASTER`
  - `BT_NIMBLE_ROLE_OBSERVER`

### 2) Cross-flag normalization

Variant toggles are normalized so that:

- if **any** service flag is effectively `y` -> `BT_NIMBLE_GATT_SERVER=y`
- if **all** service flags are effectively `n` -> `BT_NIMBLE_GATT_SERVER=n`

### 3) Example-required configs

Many examples have enforced required flags (maintained in script dictionary). If a variant conflicts, it is skipped.

### 4) Target-required configs

Some targets enforce required config states (e.g. `BT_NIMBLE_LEGACY_VHCI_ENABLE=y` on selected targets).

### 5) Target allowlist / denylist per example

Script enforces explicit per-example target allow/deny rules (maintained in script dictionaries).

### 6) Excluded examples

Examples listed in exclusion set are removed from scheduling.

### 7) HCI folder policy

For `hci` and `hci/*` examples:

- `BT_ENABLED` must be `n`
- `BT_NIMBLE_ENABLED` must be `n`
- all `BT_NIMBLE*` options must be disabled

---

## Build Execution Flow

For each scheduled task, script runs:

```bash
idf.py -C <example_path> -B <build_dir> \
  -DSDKCONFIG_DEFAULTS="<example>/sdkconfig.defaults;<generated_sdkconfig>" \
  set-target <target>

idf.py -C <example_path> -B <build_dir> build
```

In `--default-only` mode, `SDKCONFIG_DEFAULTS` uses only the example's own `sdkconfig.defaults` (no generated sdkconfig is appended).

Build start lines include timestamp:

```text
HH:MM:SS [idx/total] Building <target>/<example>/<variant> ...
```

---

## Logging and Disk Behavior

### Logs

- Logs are written to `results/<timestamp>/`
- Each log starts with sdkconfig toggles, then build output
- Build output is truncated to last `MAX_LOG_BODY_CHARS` (currently `1,000,000`) to limit disk usage
- If log write hits `ENOSPC`, run continues; log path in report indicates write failure

### Build directories

- Per-task build directory is deleted immediately after task finishes (success/fail/exception)
- If `--keep-builds` is set, build directories are preserved
- Temporary root still follows final cleanup behavior (`--keep-builds` keeps it)

---

## Skip Summary Meanings

During scheduling, summary may include:

- `Skipped: X (target not supported by example)`
- `Skipped: X (target denied by explicit rules)`
- `Skipped: X (global NimBLE constraints)`
- `Skipped: X (example-specific constraints)`
- `Skipped: X (conflicts with example-required configs)`

These are expected filters, not build failures.

---

## Typical Usage Patterns

### Baseline default-settings run for one target

```bash
python tools/bt/nimble_compile_check.py --default-only --all-examples --target esp32c3
```

### Default grouped validation

```bash
python tools/bt/nimble_compile_check.py --target esp32c3
```

### Strict combo run on narrow scope

```bash
python tools/bt/nimble_compile_check.py \
  --variant-mode flag-combos \
  --groups debug,memory \
  --combo-max-flags 10 \
  --target esp32c3 \
  --examples bleprph
```

### Single custom variant

```bash
python tools/bt/nimble_compile_check.py \
  --custom BT_NIMBLE_EXT_ADV=y,BT_NIMBLE_50_FEATURE_SUPPORT=y \
  --targets esp32c5,esp32c6 \
  --examples ble_multi_adv
```

### Preserve artifacts for deep debugging

```bash
python tools/bt/nimble_compile_check.py --groups security --keep-builds
```

---

## Troubleshooting

### Cannot determine IDF path

```bash
. ./export.sh
# or
python tools/bt/nimble_compile_check.py --idf-path /path/to/esp-idf
```

### "No space left on device"

Recent script versions are hardened against this for log writes, and trim logs by size.

If you still hit disk limits:

- reduce variant scope (`--groups`, fewer examples/targets)
- reduce concurrency (`--parallel`)
- avoid `--keep-builds`
- clean old `results/*` directories

### Builds taking too long

- start with single target + single example
- use `--groups` to reduce variant count
- use `--variant-mode group` before trying `flag-combos`

---

## Maintainer Notes

Constraint dictionaries and policy sets live near the top of the script:

- `EXAMPLE_REQUIRED_CONFIGS`
- `EXAMPLE_TARGET_ALLOWLIST`
- `EXAMPLE_TARGET_DENYLIST`
- `EXCLUDED_EXAMPLES`
- `TARGET_REQUIRED_CONFIGS`

When adding new known-invalid combinations, update these dictionaries first.

Example path resolution also supports selected Bluetooth examples outside `examples/bluetooth/nimble/*` (for example `examples/bluetooth/blufi`).
