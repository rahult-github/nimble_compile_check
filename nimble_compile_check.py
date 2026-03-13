#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Espressif Systems (Shanghai) CO LTD
# SPDX-License-Identifier: Apache-2.0
"""
NimBLE Compilation Coverage Checker

Parses NimBLE Kconfig.in, auto-groups boolean flags by feature area,
generates sdkconfig variants toggling flag groups, and builds bleprph/blecent
with each variant to catch compilation issues from untested config combos.

Usage:
    python nimble_compile_check.py --list-flags
    python nimble_compile_check.py --list-groups
    python nimble_compile_check.py --groups ext_adv,security --targets esp32c3
    python nimble_compile_check.py --targets esp32c3,esp32s3 --all-examples
    python nimble_compile_check.py --all-targets --all-examples
    python nimble_compile_check.py --custom BT_NIMBLE_AOA_AOD=n,BT_NIMBLE_EXT_ADV=y
    python nimble_compile_check.py  # full run
"""

from __future__ import annotations

import argparse
import datetime
import errno
import itertools
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import OrderedDict
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures import as_completed
from typing import TextIO

# All ESP chips with BLE (NimBLE) support
BLE_SUPPORTED_TARGETS = [
    'esp32',
    'esp32c2',
    'esp32c3',
    'esp32c5',
    'esp32c6',
    'esp32c61',
    'esp32h2',
    'esp32s3',
]

# Example-specific required NimBLE configs.
# Variants that explicitly conflict with these requirements are skipped.
EXAMPLE_REQUIRED_CONFIGS: dict[str, dict[str, str]] = {
    'bleprph': {
        'BT_NIMBLE_GATT_SERVER': 'y',
        'BT_NIMBLE_ROLE_PERIPHERAL': 'y',
    },
    'blecent': {
        'BT_NIMBLE_GATT_CLIENT': 'y',
        'BT_NIMBLE_ROLE_CENTRAL': 'y',
    },
    'ble_ancs': {
        'BT_NIMBLE_GATT_CLIENT': 'y',
        'BT_NIMBLE_GAP_SERVICE': 'y',
    },
    'ble_gattc_gatts_coex': {
        'BT_NIMBLE_GATT_CLIENT': 'y',
        'BT_NIMBLE_GATT_SERVER': 'y',
    },
    'ble_dynamic_service': {
        'BT_NIMBLE_GAP_SERVICE': 'y',
        'BT_NIMBLE_ANS_SERVICE': 'y',
        'BT_NIMBLE_GATT_SERVER': 'y',
    },
    'ble_periodic_adv': {
        'BT_NIMBLE_ENABLE_PERIODIC_ADV': 'y',
    },
    'ble_periodic_sync': {
        'BT_NIMBLE_ENABLE_PERIODIC_ADV': 'y',
        'BT_NIMBLE_EXT_ADV': 'y',
        'BT_NIMBLE_50_FEATURE_SUPPORT': 'y',
    },
    'blecsc': {
        'BT_NIMBLE_GATT_SERVER': 'y',
    },
    'blehr': {
        'BT_NIMBLE_GATT_SERVER': 'y',
    },
    'bleprph_wifi_coex': {
        'BT_NIMBLE_GATT_SERVER': 'y',
    },
    'power_save': {
        'BT_NIMBLE_GAP_SERVICE': 'y',
    },
    'blufi': {
        'BT_NIMBLE_BLUFI_ENABLE': 'y',
        'BT_NIMBLE_ENABLED': 'y',
        'BT_BLUEDROID_ENABLED': 'n',
        'BT_NIMBLE_EXT_ADV': 'n',
        'BT_NIMBLE_ENABLE_PERIODIC_ADV': 'n',
    },
    'ble_multi_adv': {
        'BT_NIMBLE_50_FEATURE_SUPPORT': 'y',
        'BT_NIMBLE_EXT_ADV': 'y',
    },
    # Alias for typo in user-provided example name.
    'ble_perioidic_adv': {
        'BT_NIMBLE_ENABLE_PERIODIC_ADV': 'y',
    },
}

# Optional per-example hard target allowlist override.
# If present, build scheduling will only allow listed targets.
EXAMPLE_TARGET_ALLOWLIST: dict[str, list[str]] = {
    'ble_chan_sound_initiator': ['esp32c6'],
    'ble_chan_sound_reflector': ['esp32c6'],
    'ble_cte/ble_periodic_adv_with_cte': ['esp32c5', 'esp32c61', 'esp32h2'],
    'ble_cte/ble_periodic_sync_with_cte': ['esp32c5', 'esp32c61', 'esp32h2'],
    'ble_multi_conn/ble_multi_conn_cent': ['esp32c5', 'esp32c6', 'esp32c61', 'esp32h2'],
    'ble_multi_conn/ble_multi_conn_prph': ['esp32c5', 'esp32c6', 'esp32c61', 'esp32h2'],
    'ble_pawr_adv/ble_pawr_adv': ['esp32c5', 'esp32c6', 'esp32c61', 'esp32h2'],
    'ble_pawr_adv/ble_pawr_sync': ['esp32c5', 'esp32c6', 'esp32c61', 'esp32h2'],
    'ble_pawr_adv_conn/ble_pawr_adv_conn': ['esp32c5', 'esp32c6', 'esp32c61', 'esp32h2'],
    'ble_pawr_adv_conn/ble_pawr_sync_conn': ['esp32c5', 'esp32c6', 'esp32c61', 'esp32h2'],
}

EXAMPLE_TARGET_DENYLIST: dict[str, list[str]] = {
    'ble_enc_adv_data/enc_adv_data_cent': ['esp32'],
    'ble_enc_adv_data/enc_adv_data_prph': ['esp32'],
    'ble_multi_adv': ['esp32'],
    'ble_periodic_adv': ['esp32'],
    'ble_periodic_sync': ['esp32'],
    'ble_phy/phy_cent': ['esp32'],
    'ble_phy/phy_prph': ['esp32'],
    'bleprph_wifi_coex': ['esp32h2'],
}

EXCLUDED_EXAMPLES: set[str] = {
    'blemesh',
}

# Target-specific required NimBLE configs.
TARGET_REQUIRED_CONFIGS: dict[str, dict[str, str]] = {
    'esp32': {
        'BT_NIMBLE_LEGACY_VHCI_ENABLE': 'y',
        'BT_NIMBLE_50_FEATURE_SUPPORT': 'n',
        'BT_NIMBLE_EXT_ADV': 'n',
        'BT_NIMBLE_ENABLE_PERIODIC_ADV': 'n',
    },
    'esp32c3': {
        'BT_NIMBLE_LEGACY_VHCI_ENABLE': 'y',
    },
    'esp32s3': {
        'BT_NIMBLE_LEGACY_VHCI_ENABLE': 'y',
    },
}

ROLE_FLAGS = [
    'BT_NIMBLE_ROLE_CENTRAL',
    'BT_NIMBLE_ROLE_PERIPHERAL',
    'BT_NIMBLE_ROLE_BROADCASTER',
    'BT_NIMBLE_ROLE_OBSERVER',
]


# ---------------------------------------------------------------------------
# Kconfig parser
# ---------------------------------------------------------------------------


class KconfigFlag:
    __slots__ = ('name', 'type', 'default', 'depends_on', 'if_conditions', 'in_choice')

    def __init__(self, name: str) -> None:
        self.name = name
        self.type: str | None = None
        self.default: str | None = None
        self.depends_on: list[str] = []
        self.if_conditions: list[str] = []
        self.in_choice = False

    def __repr__(self) -> str:
        return (
            f'KconfigFlag({self.name}, type={self.type}, default={self.default}, '
            f'depends={self.depends_on}, if_conds={self.if_conditions}, choice={self.in_choice})'
        )


def parse_kconfig(path: str) -> list[KconfigFlag]:
    """Parse Kconfig.in and return list of KconfigFlag for all bool configs."""
    flags: list[KconfigFlag] = []
    if_stack: list[str] = []  # stack of condition strings from enclosing if blocks
    choice_depth = 0  # nesting depth of choice/endchoice
    current: KconfigFlag | None = None  # KconfigFlag being built

    with open(path) as f:
        lines = f.readlines()

    def _flush() -> None:
        nonlocal current
        if current and current.type == 'bool':
            current.if_conditions = list(if_stack)
            current.in_choice = choice_depth > 0
            flags.append(current)
        current = None

    for raw_line in lines:
        line = raw_line.strip()

        # Strip inline comments (# ...) for keyword matching
        line_no_comment = re.sub(r'\s*#.*$', '', line).strip()

        # Track if/endif blocks
        if re.match(r'^if\s+', line_no_comment):
            cond = line_no_comment[3:].strip()
            if_stack.append(cond)
            continue
        if line_no_comment == 'endif':
            if if_stack:
                if_stack.pop()
            continue

        # Track choice/endchoice
        if re.match(r'^choice\b', line_no_comment):
            _flush()
            choice_depth += 1
            continue
        if line_no_comment == 'endchoice':
            _flush()
            choice_depth = max(0, choice_depth - 1)
            continue

        # Start of a new config or menuconfig block
        m = re.match(r'^(config|menuconfig)\s+(\w+)', line_no_comment)
        if m:
            _flush()
            current = KconfigFlag(m.group(2))
            current.in_choice = choice_depth > 0
            continue

        # Skip lines not inside a config block
        if current is None:
            continue

        # menu / endmenu boundaries end the current config
        if re.match(r'^(menu\s|endmenu)', line_no_comment):
            _flush()
            continue

        # Type declaration
        if re.match(r'^bool\b', line_no_comment):
            current.type = 'bool'
            continue

        # Default value (take first unconditional or first listed)
        m_def = re.match(r'^default\s+(\S+)', line_no_comment)
        if m_def and current.default is None:
            val = m_def.group(1)
            if val in ('y', 'n'):
                current.default = val
            continue

        # depends on
        m_dep = re.match(r'^depends\s+on\s+(.+)', line_no_comment)
        if m_dep:
            current.depends_on.append(m_dep.group(1).strip())
            continue

    _flush()
    return flags


# ---------------------------------------------------------------------------
# Flag grouper
# ---------------------------------------------------------------------------

# Each rule is (group_name, list_of_regex_patterns).
# Patterns are matched against the flag name (without BT_NIMBLE_ prefix).
# Use word-boundary-style patterns to avoid substring false positives.
GROUP_RULES = OrderedDict(
    [
        ('ext_adv', [r'EXT_ADV', r'PERIODIC', r'EXT_SCAN', r'AOA_AOD', r'MONITOR_ADV']),
        (
            'security',
            [
                r'SECURITY',
                r'SM_SC\b',
                r'SM_LEGACY',
                r'SM_SC_DEBUG',
                r'SM_SIGN',
                r'SM_LVL',
                r'SM_SC_ONLY',
                r'SMP_ID_RESET',
                r'LL_CFG_FEAT_LE_ENCRYPTION',
                r'NVS_PERSIST',
                r'STATIC_PASSKEY',
                r'HANDLE_REPEAT_PAIRING',
                r'CRYPTO_STACK',
                r'_PVCY',
                r'HOST_BASED_PRIVACY',
            ],
        ),
        (
            'gatt',
            [
                r'GATT',
                r'ATT_PREFERRED',
                r'ATT_MAX_PREP',
                r'BLOB_TRANSFER',
                r'INCL_SVC_DISCOVERY',
                r'DYNAMIC_SERVICE',
                r'CPFD_CAFD',
                r'RECONFIG_MTU',
            ],
        ),
        ('l2cap', [r'L2CAP', r'_EATT_', r'EATT_CHAN', r'_COC_', r'L2CAP_ENHANCED_COC', r'_SUBRATE\b']),
        ('mesh', [r'MESH']),
        (
            'services',
            [
                r'PROX_SERVICE',
                r'ANS_SERVICE',
                r'CTS_SERVICE',
                r'HTP_SERVICE',
                r'IPSS_SERVICE',
                r'TPS_SERVICE',
                r'IAS_SERVICE',
                r'LLS_SERVICE',
                r'SPS_SERVICE',
                r'HR_SERVICE',
                r'HID_SERVICE',
                r'SVC_HID',
                r'BAS_SERVICE',
                r'SVC_BAS',
                r'DIS_SERVICE',
                r'SVC_DIS',
                r'GAP_SERVICE',
                r'SVC_GAP',
                r'SVC_GAP_',
            ],
        ),
        ('iso', [r'_ISO\b', r'ISO_']),
        ('debug', [r'DEBUG\b', r'LOG_LEVEL', r'MEM_DEBUG', r'PRINT_ERR', r'DTM_MODE_TEST', r'TEST_THROUGHPUT']),
        ('memory', [r'MEM_ALLOC', r'MSYS', r'MEMPOOL', r'MEM_OPTIMIZATION', r'STATIC_TO_DYNAMIC', r'MSYS_BUF']),
        ('ble5x', [r'50_FEATURE', r'2M_PHY', r'CODED_PHY', r'POWER_CONTROL', r'CHANNEL_SOUNDING', r'60_FEATURE']),
    ]
)


def _match_group(flag_name: str) -> str:
    # Strip BT_NIMBLE_ prefix for matching clarity, but also check full name
    short = flag_name
    if short.startswith('BT_NIMBLE_'):
        short = short[len('BT_NIMBLE_') :]

    for group, patterns in GROUP_RULES.items():
        for pat in patterns:
            if re.search(pat, short) or re.search(pat, flag_name):
                return group
    return 'misc'


def group_flags(flags: list[KconfigFlag]) -> OrderedDict[str, list[KconfigFlag]]:
    """Return dict of group_name -> list of KconfigFlag."""
    groups: OrderedDict[str, list[KconfigFlag]] = OrderedDict()
    for g in list(GROUP_RULES.keys()) + ['misc']:
        groups[g] = []

    for f in flags:
        g = _match_group(f.name)
        groups[g].append(f)

    # Remove empty groups
    return OrderedDict((k, v) for k, v in groups.items() if v)


# ---------------------------------------------------------------------------
# Sdkconfig generator
# ---------------------------------------------------------------------------

SDKCONFIG_BASE = '# Auto-generated by nimble_compile_check.py\nCONFIG_BT_ENABLED=y\nCONFIG_BT_NIMBLE_ENABLED=y\n'
BASE_CONFIG_DEFAULTS: dict[str, str] = {
    'BT_ENABLED': 'y',
    'BT_NIMBLE_ENABLED': 'y',
}


def _config_line(name: str, val: str) -> str:
    prefix = 'CONFIG_' if not name.startswith('CONFIG_') else ''
    if val == 'y':
        return f'{prefix}{name}=y\n'
    else:
        return f'# {prefix}{name} is not set\n'


def _normalize_cfg_name(name: str) -> str:
    return name[7:] if name.startswith('CONFIG_') else name


def _is_ignored_flag(name: str) -> bool:
    norm = _normalize_cfg_name(name)
    return norm.startswith('BT_NIMBLE_MESH')


def _find_required_conflicts(
    toggles: list[tuple[str, str]],
    required_configs: dict[str, str],
    flag_defaults: dict[str, str],
    example_defaults: dict[str, str] | None = None,
) -> list[tuple[str, str, str]]:
    """Return conflicts as (config_name, required_val, effective_val)."""
    conflicts: list[tuple[str, str, str]] = []
    for req_name, req_val in required_configs.items():
        req_norm = _normalize_cfg_name(req_name)
        effective_val = _effective_config_value(req_norm, toggles, flag_defaults, example_defaults)
        if effective_val is not None and effective_val != req_val:
            conflicts.append((req_norm, req_val, effective_val))
    return conflicts


def _violates_global_constraints(
    toggles: list[tuple[str, str]],
    flag_defaults: dict[str, str],
) -> list[str]:
    """Return global-constraint violation messages for this variant."""
    toggle_map = {_normalize_cfg_name(name): val for name, val in toggles}
    role_vals: list[str] = []
    for role in ROLE_FLAGS:
        default = flag_defaults.get(role)
        if default not in ('y', 'n'):
            continue
        role_vals.append(toggle_map.get(role, default))
    if role_vals and all(v == 'n' for v in role_vals):
        return ['At least one NimBLE role must be enabled.']
    return []


def _effective_config_value(
    config_name: str,
    toggles: list[tuple[str, str]],
    flag_defaults: dict[str, str],
    example_defaults: dict[str, str] | None = None,
) -> str | None:
    norm = _normalize_cfg_name(config_name)
    toggle_map = {_normalize_cfg_name(name): val for name, val in toggles}
    if norm in toggle_map:
        return toggle_map[norm]
    if norm in BASE_CONFIG_DEFAULTS:
        return BASE_CONFIG_DEFAULTS[norm]
    if example_defaults and norm in example_defaults:
        return example_defaults[norm]
    return flag_defaults.get(norm)


def _violates_example_constraints(
    example_name: str,
    toggles: list[tuple[str, str]],
    flag_defaults: dict[str, str],
    example_defaults: dict[str, str] | None = None,
) -> list[str]:
    """Return per-example/folder constraint violations for this variant."""
    normalized = normalize_example_name(example_name)
    if normalized == 'hci' or normalized.startswith('hci/'):
        bt_enabled = _effective_config_value('BT_ENABLED', toggles, flag_defaults, example_defaults)
        nimble_enabled = _effective_config_value('BT_NIMBLE_ENABLED', toggles, flag_defaults, example_defaults)
        if bt_enabled == 'y' or nimble_enabled == 'y':
            return ['HCI examples must keep BT_ENABLED=n and BT_NIMBLE_ENABLED=n.']
        for cfg_name in flag_defaults:
            if not cfg_name.startswith('BT_NIMBLE'):
                continue
            if _effective_config_value(cfg_name, toggles, flag_defaults, example_defaults) == 'y':
                return ['HCI examples must keep all BT_NIMBLE* options disabled.']
    return []


def _normalize_variant_toggles(
    toggles: list[tuple[str, str]],
    flag_defaults: dict[str, str],
    service_flag_names: list[str],
) -> list[tuple[str, str]]:
    """Apply cross-flag constraints to a variant's toggle list.

    Rule:
      - If all service flags are effectively 'n', force BT_NIMBLE_GATT_SERVER='n'
      - If any service flag is effectively 'y', force BT_NIMBLE_GATT_SERVER='y'
    """
    # Keep insertion order while allowing override.
    toggle_map: OrderedDict[str, str] = OrderedDict()
    for name, val in toggles:
        toggle_map[name] = val

    effective_service_vals: list[str] = []
    for name in service_flag_names:
        default = flag_defaults.get(name)
        if default not in ('y', 'n'):
            continue
        effective_service_vals.append(toggle_map.get(name, default))

    if effective_service_vals and 'BT_NIMBLE_GATT_SERVER' in flag_defaults:
        gatt_server_val = 'y' if any(v == 'y' for v in effective_service_vals) else 'n'
        toggle_map['BT_NIMBLE_GATT_SERVER'] = gatt_server_val

    return list(toggle_map.items())


def generate_sdkconfig(toggles: list[tuple[str, str]], path: str) -> None:
    """Write an sdkconfig file with base NimBLE + toggle lines.

    toggles: list of (config_name, 'y'|'n')
    """
    with open(path, 'w') as f:
        f.write(SDKCONFIG_BASE)
        f.write('\n# Flag toggles\n')
        for name, val in toggles:
            f.write(_config_line(name, val))


def build_variants(
    flags: list[KconfigFlag], groups: OrderedDict[str, list[KconfigFlag]]
) -> list[tuple[str, list[tuple[str, str]]]]:
    """Return list of (variant_name, toggles) for all groups + max variants."""
    variants: list[tuple[str, list[tuple[str, str]]]] = []

    for gname, gflags in groups.items():
        # enable variant: flip disabled-by-default to y
        enable_toggles = [
            (f.name, 'y') for f in gflags if f.default == 'n' and not f.in_choice and not _is_ignored_flag(f.name)
        ]
        if enable_toggles:
            variants.append((f'{gname}_enable', enable_toggles))

        # disable variant: flip enabled-by-default to n
        disable_toggles = [
            (f.name, 'n') for f in gflags if f.default == 'y' and not f.in_choice and not _is_ignored_flag(f.name)
        ]
        if disable_toggles:
            variants.append((f'{gname}_disable', disable_toggles))

    # max_enable: all disabled-by-default bools → y (skip choice items)
    max_en = [(f.name, 'y') for f in flags if f.default == 'n' and not f.in_choice and not _is_ignored_flag(f.name)]
    if max_en:
        variants.append(('max_enable', max_en))

    # max_disable: all enabled-by-default bools → n (skip choice items)
    max_dis = [(f.name, 'n') for f in flags if f.default == 'y' and not f.in_choice and not _is_ignored_flag(f.name)]
    if max_dis:
        variants.append(('max_disable', max_dis))

    return variants


def _flip_from_default(flag: KconfigFlag) -> tuple[str, str] | None:
    if flag.default == 'n':
        return (flag.name, 'y')
    if flag.default == 'y':
        return (flag.name, 'n')
    return None


def build_flag_combo_variants(
    groups: OrderedDict[str, list[KconfigFlag]],
    combo_max_flags: int,
    combo_max_size: int | None,
) -> list[tuple[str, list[tuple[str, str]]]]:
    """Return variants that first toggle one flag at a time, then combinations.

    Flags are toggled away from their defaults:
      - default=n -> y
      - default=y -> n
    """
    candidate_flags: list[KconfigFlag] = []
    for gflags in groups.values():
        for f in gflags:
            if f.in_choice:
                continue
            if f.default not in ('y', 'n'):
                continue
            if _is_ignored_flag(f.name):
                continue
            candidate_flags.append(f)

    # Preserve first-seen ordering while de-duplicating.
    deduped: list[KconfigFlag] = []
    seen: set[str] = set()
    for f in candidate_flags:
        if f.name not in seen:
            seen.add(f.name)
            deduped.append(f)
    candidate_flags = deduped

    if not candidate_flags:
        return []

    if len(candidate_flags) > combo_max_flags:
        raise ValueError(
            f'Flag-combo mode selected {len(candidate_flags)} toggleable flags, '
            f'which exceeds --combo-max-flags={combo_max_flags}. '
            f'Reduce scope with --groups or increase --combo-max-flags.'
        )

    max_size = len(candidate_flags) if combo_max_size is None else combo_max_size
    if max_size < 1:
        raise ValueError('--combo-max-size must be >= 1 when provided.')
    max_size = min(max_size, len(candidate_flags))

    variants: list[tuple[str, list[tuple[str, str]]]] = []

    # Step 1: one-by-one toggles
    for idx, flag in enumerate(candidate_flags, start=1):
        flip = _flip_from_default(flag)
        if flip is None:
            continue
        variants.append((f'flag_single_{idx:03d}_{flag.name.lower()}', [flip]))

    # Step 2: combinations
    combo_idx = 1
    for r in range(2, max_size + 1):
        for combo in itertools.combinations(candidate_flags, r):
            toggles: list[tuple[str, str]] = []
            for f in combo:
                t = _flip_from_default(f)
                if t is not None:
                    toggles.append(t)
            if not toggles:
                continue
            variants.append((f'flag_combo_{r:02d}_{combo_idx:06d}', toggles))
            combo_idx += 1

    return variants


# ---------------------------------------------------------------------------
# Build runner
# ---------------------------------------------------------------------------


def discover_examples(idf_path: str) -> list[str]:
    """Find buildable example directories covered by this checker."""
    nimble_examples_dir = os.path.join(idf_path, 'examples', 'bluetooth', 'nimble')
    examples: list[str] = []
    if not os.path.isdir(nimble_examples_dir):
        return examples
    for entry in sorted(os.listdir(nimble_examples_dir)):
        entry_path = os.path.join(nimble_examples_dir, entry)
        if os.path.isdir(entry_path) and os.path.isfile(os.path.join(entry_path, 'CMakeLists.txt')):
            examples.append(entry)

    # Include selected non-nimble-folder examples that are part of NimBLE coverage.
    blufi_path = os.path.join(idf_path, 'examples', 'bluetooth', 'blufi')
    if os.path.isdir(blufi_path) and os.path.isfile(os.path.join(blufi_path, 'CMakeLists.txt')):
        examples.append('blufi')
    return examples


def normalize_example_name(example_name: str) -> str:
    normalized = example_name.strip().strip('/')
    if normalized.startswith('examples/bluetooth/nimble/'):
        return normalized[len('examples/bluetooth/nimble/') :]
    if normalized.startswith('examples/bluetooth/'):
        return normalized[len('examples/bluetooth/') :]
    return normalized


def resolve_example_path(idf_path: str, example_name: str) -> str:
    """Resolve an example name to an absolute path."""
    normalized = normalize_example_name(example_name)

    # 1) Default NimBLE location.
    nimble_path = os.path.join(idf_path, 'examples', 'bluetooth', 'nimble', normalized)
    if os.path.isdir(nimble_path):
        return nimble_path

    # 2) Bluetooth examples outside nimble/ (e.g. blufi).
    bt_path = os.path.join(idf_path, 'examples', 'bluetooth', normalized)
    if os.path.isdir(bt_path):
        return bt_path

    # 3) Repo-relative explicit path fallback.
    repo_rel = os.path.join(idf_path, normalized)
    if os.path.isdir(repo_rel):
        return repo_rel

    # Keep previous behavior fallback for error messaging.
    return nimble_path


def _parse_sdkconfig_defaults_file(path: str) -> dict[str, str]:
    vals: dict[str, str] = {}
    if not os.path.isfile(path):
        return vals
    try:
        with open(path) as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                m_set = re.match(r'^CONFIG_(\w+)=(y|n)$', line)
                if m_set:
                    vals[m_set.group(1)] = m_set.group(2)
                    continue
                m_unset = re.match(r'^#\s*CONFIG_(\w+)\s+is\s+not\s+set$', line)
                if m_unset:
                    vals[m_unset.group(1)] = 'n'
    except OSError:
        return vals
    return vals


def get_example_defaults_values(example_path: str, target: str) -> dict[str, str]:
    vals = _parse_sdkconfig_defaults_file(os.path.join(example_path, 'sdkconfig.defaults'))
    vals.update(_parse_sdkconfig_defaults_file(os.path.join(example_path, f'sdkconfig.defaults.{target}')))
    if os.path.basename(example_path) == 'blufi':
        # Force NimBLE BLUFI enablement across targets for this checker workflow.
        vals['BT_NIMBLE_BLUFI_ENABLE'] = 'y'
        vals['BT_NIMBLE_ENABLED'] = 'y'
        vals['BT_BLUEDROID_ENABLED'] = 'n'
        vals['BT_NIMBLE_EXT_ADV'] = 'n'
        vals['BT_NIMBLE_ENABLE_PERIODIC_ADV'] = 'n'
    return vals


def get_example_required_configs(example_name: str) -> dict[str, str]:
    base = dict(EXAMPLE_REQUIRED_CONFIGS.get(normalize_example_name(example_name), {}))
    if base.get('BT_NIMBLE_GATT_SERVER') == 'y':
        base.setdefault('BT_NIMBLE_ANS_SERVICE', 'y')
        base.setdefault('BT_NIMBLE_GAP_SERVICE', 'y')
    return base


def get_example_supported_targets(idf_path: str, example_name: str) -> list[str] | None:
    """Parse the README.md of a NimBLE example to extract supported targets.

    Returns a list of target names in idf.py format (e.g. ['esp32c6']) or None
    if the README is missing or the supported targets table cannot be parsed.
    """
    example_path = resolve_example_path(idf_path, example_name)
    readme_path = os.path.join(example_path, 'README.md')
    try:
        with open(readme_path) as f:
            first_line = f.readline()
    except OSError:
        return None

    # Expected format: | Supported Targets | ESP32 | ESP32-C6 | ... |
    if 'Supported Targets' not in first_line:
        return None

    parts = [p.strip() for p in first_line.split('|')]
    # Filter out empty strings and the "Supported Targets" header
    targets: list[str] = []
    for part in parts:
        if not part or part == 'Supported Targets':
            continue
        # Convert README format (ESP32-C6) to idf.py format (esp32c6)
        targets.append(part.lower().replace('-', ''))
    return targets if targets else None


def get_example_target_allowlist(example_name: str) -> list[str] | None:
    return EXAMPLE_TARGET_ALLOWLIST.get(normalize_example_name(example_name))


def get_target_required_configs(target: str) -> dict[str, str]:
    return TARGET_REQUIRED_CONFIGS.get(target, {})


def get_example_target_denylist(example_name: str) -> list[str]:
    return EXAMPLE_TARGET_DENYLIST.get(normalize_example_name(example_name), [])


def is_example_excluded(example_name: str) -> bool:
    return normalize_example_name(example_name) in EXCLUDED_EXAMPLES


def _get_idf_env(idf_path: str) -> dict[str, str]:
    """Get the full environment with ESP-IDF tools and Python venv on PATH.

    Sources export.sh and captures the resulting environment. Falls back
    to the current environment if export.sh is unavailable.
    """
    export_sh = os.path.join(idf_path, 'export.sh')
    if not os.path.isfile(export_sh):
        return os.environ.copy()

    try:
        # Source export.sh in a subshell and dump the environment
        cmd = f'set -e; source "{export_sh}" >/dev/null 2>&1; env -0'
        result = subprocess.run(
            ['bash', '-c', cmd],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and result.stdout:
            env = {}
            for entry in result.stdout.split('\0'):
                if '=' in entry:
                    k, v = entry.split('=', 1)
                    env[k] = v
            env['IDF_PATH'] = idf_path
            return env
    except Exception:
        pass

    # Fallback: current env + IDF_PATH
    env = os.environ.copy()
    env['IDF_PATH'] = idf_path
    return env


# Cached IDF environment (populated once in main, shared across builds)
_idf_env_cache = None

# Cap per-build log body to avoid exhausting disk with massive build output.
MAX_LOG_BODY_CHARS = 1_000_000


def run_build(
    idf_path: str, example_path: str, sdkconfig_path: str | None, target: str, build_dir: str
) -> tuple[bool, str]:
    """Run idf.py build for a single variant. Returns (success, stdout+stderr)."""
    global _idf_env_cache
    if _idf_env_cache is None:
        _idf_env_cache = _get_idf_env(idf_path)

    defaults_files: list[str] = []
    example_defaults = os.path.join(example_path, 'sdkconfig.defaults')
    target_defaults = os.path.join(example_path, f'sdkconfig.defaults.{target}')
    if os.path.isfile(example_defaults):
        defaults_files.append(example_defaults)
    if os.path.isfile(target_defaults):
        defaults_files.append(target_defaults)
    if os.path.basename(example_path) == 'blufi':
        # Force NimBLE BLUFI for all targets in this checker.
        forced_dir = os.path.dirname(sdkconfig_path) if sdkconfig_path else os.path.dirname(build_dir)
        os.makedirs(forced_dir, exist_ok=True)
        forced_defaults = os.path.join(forced_dir, f'sdkconfig.forced.blufi.{target}')
        with open(forced_defaults, 'w') as f:
            f.write('CONFIG_BT_NIMBLE_BLUFI_ENABLE=y\n')
            f.write('CONFIG_BT_NIMBLE_ENABLED=y\n')
            f.write('# CONFIG_BT_BLUEDROID_ENABLED is not set\n')
            f.write('CONFIG_BT_NIMBLE_EXT_ADV=n\n')
            f.write('CONFIG_BT_NIMBLE_ENABLE_PERIODIC_ADV=n\n')
        defaults_files.append(forced_defaults)
    if sdkconfig_path is not None:
        defaults_files.append(sdkconfig_path)
    sdkconfig_defaults = ';'.join(defaults_files)

    # Use 'python' from the IDF environment
    python = shutil.which('python', path=_idf_env_cache.get('PATH', '')) or 'python'
    idf_py = os.path.join(idf_path, 'tools', 'idf.py')

    cmd = [
        python,
        idf_py,
        '-C',
        example_path,
        '-B',
        build_dir,
        f'-DSDKCONFIG_DEFAULTS={sdkconfig_defaults}',
        'set-target',
        target,
    ]

    # Run set-target
    result = subprocess.run(cmd, capture_output=True, text=True, env=_idf_env_cache, timeout=300)
    if result.returncode != 0:
        return False, result.stdout + result.stderr

    # Run build
    cmd_build = [
        python,
        idf_py,
        '-C',
        example_path,
        '-B',
        build_dir,
        'build',
    ]
    result = subprocess.run(cmd_build, capture_output=True, text=True, env=_idf_env_cache, timeout=600)
    return result.returncode == 0, result.stdout + result.stderr


def _extract_errors(output: str) -> list[str]:
    """Extract error lines from build output."""
    errors: list[str] = []
    for line in output.splitlines():
        line_lower = line.lower()
        if (
            'error:' in line_lower
            or 'error[' in line_lower
            or 'cmake error' in line_lower
            or 'failed' in line_lower
            and 'build' in line_lower
            or 'fatal error' in line_lower
        ):
            stripped = line.strip()
            if stripped and stripped not in errors:
                errors.append(stripped)
    if not errors:
        # No specific error lines found — grab last 15 non-empty lines as context
        tail = [line.strip() for line in output.splitlines() if line.strip()][-15:]
        errors = tail
    return errors[:15]


def _do_single_build(
    args: tuple[str, str, str, str, str, str | None, str, str, bool],
) -> tuple[str, str, str, bool, list[str], str]:
    """Wrapper for parallel execution. args is a tuple of build params."""
    variant_name, example_name, target, idf_path, example_path, sdkconfig_path, build_dir, logs_dir, keep_builds = args
    log_file = os.path.join(logs_dir, f'{target}_{example_name}_{variant_name}.log')

    # Read the sdkconfig toggles so they can be included in the log
    if sdkconfig_path is None:
        sdkconfig_content = '# default example settings (no generated sdkconfig toggles)\n'
    else:
        try:
            with open(sdkconfig_path) as f:
                sdkconfig_content = f.read()
        except OSError:
            sdkconfig_content = '# (could not read sdkconfig)\n'

    def _trim_log_body(body: str) -> str:
        if len(body) <= MAX_LOG_BODY_CHARS:
            return body
        trimmed = body[-MAX_LOG_BODY_CHARS:]
        return f'[log truncated: keeping last {MAX_LOG_BODY_CHARS} chars out of {len(body)} chars]\n\n{trimmed}'

    def _write_log(body: str) -> str:
        try:
            with open(log_file, 'w') as f:
                sdkconfig_label = sdkconfig_path if sdkconfig_path is not None else '<example sdkconfig.defaults only>'
                f.write(f'=== sdkconfig toggles ({sdkconfig_label}) ===\n')
                f.write(sdkconfig_content)
                f.write('\n=== build output ===\n')
                f.write(_trim_log_body(body))
            return log_file
        except OSError as e:
            # Do not crash the whole run because one log file can't be written.
            if e.errno == errno.ENOSPC:
                return f'{log_file} (write failed: no space left on device)'
            return f'{log_file} (write failed: {e})'

    try:
        success, output = run_build(idf_path, example_path, sdkconfig_path, target, build_dir)
        reported_log = _write_log(output)
        errors = _extract_errors(output) if not success else []
        return variant_name, example_name, target, success, errors, reported_log
    except subprocess.TimeoutExpired:
        reported_log = _write_log('Build timed out\n')
        return variant_name, example_name, target, False, ['Build timed out'], reported_log
    except Exception as e:
        reported_log = _write_log(f'Exception: {e}\n')
        return variant_name, example_name, target, False, [str(e)], reported_log
    finally:
        # Free disk space aggressively by removing each build directory as soon as done.
        if not keep_builds:
            shutil.rmtree(build_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Reporter
# ---------------------------------------------------------------------------


def print_report(
    results: list[tuple[str, str, str, bool, list[str], str]],
    targets: list[str],
    output_file: TextIO | None = None,
    variant_toggles: dict[str, list[tuple[str, str]]] | None = None,
) -> None:
    """Print tabular summary of build results."""
    out = output_file or sys.stdout

    # Group results by target, then by example
    by_target: OrderedDict[str, OrderedDict[str, list[tuple[str, bool, list[str], str]]]] = OrderedDict()
    for variant, example, target, success, errors, log_file in results:
        by_target.setdefault(target, OrderedDict())
        by_target[target].setdefault(example, []).append((variant, success, errors, log_file))

    total = len(results)
    failed = sum(1 for r in results if not r[3])

    lines = []
    lines.append('')
    lines.append('NimBLE Compilation Coverage Report')
    lines.append('=' * 50)

    for target, by_example in by_target.items():
        for example, entries in by_example.items():
            lines.append(f'\nExample: {example} | Target: {target}')
            lines.append(f'  {"Variant":<30} {"Result":<8} {"Errors":<6}')
            lines.append(f'  {"-" * 30} {"-" * 8} {"-" * 6}')
            for variant, success, errors, log_file in entries:
                status = 'PASS' if success else 'FAIL'
                lines.append(f'  {variant:<30} {status:<8} {len(errors):<6}')
            lines.append('')

            # Show error details for failures
            for variant, success, errors, log_file in entries:
                if not success:
                    lines.append(f'  --- {variant} FAILED ---')
                    lines.append(f'    Log: {log_file}')
                    if variant_toggles and variant in variant_toggles:
                        toggles = variant_toggles[variant]
                        lines.append(f'    Config toggles ({len(toggles)}):')
                        for flag_name, val in toggles:
                            lines.append(f'      CONFIG_{flag_name}={val}')
                    if errors:
                        for e in errors[:5]:
                            lines.append(f'    {e}')
                    lines.append('')

    lines.append(f'Failed builds: {failed}/{total}')
    lines.append('')

    text = '\n'.join(lines)
    out.write(text)

    if output_file and output_file is not sys.stdout:
        # Also print to stdout
        print(text)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description='NimBLE compilation coverage checker - builds NimBLE examples '
        'with different config flag combinations to catch compilation issues.'
    )
    parser.add_argument(
        '--idf-path', default=os.environ.get('IDF_PATH', ''), help='Path to ESP-IDF (default: $IDF_PATH)'
    )
    parser.add_argument(
        '--target', default=None, help='Build target (default: esp32c3). Alias for --targets with a single value.'
    )
    parser.add_argument('--targets', default=None, help='Comma-separated list of build targets (default: esp32c3)')
    parser.add_argument(
        '--all-targets',
        action='store_true',
        help='Build for all BLE-supported chips: ' + ', '.join(BLE_SUPPORTED_TARGETS),
    )
    parser.add_argument('--groups', default=None, help='Comma-separated list of groups to test (default: all)')
    parser.add_argument(
        '--examples', default='bleprph,blecent', help='Comma-separated example names (default: bleprph,blecent)'
    )
    parser.add_argument(
        '--all-examples', action='store_true', help='Build all NimBLE examples found under examples/bluetooth/nimble/'
    )
    parser.add_argument(
        '--list-flags', action='store_true', help='Parse Kconfig.in and print all bool flags, then exit'
    )
    parser.add_argument('--list-groups', action='store_true', help='Show auto-detected flag groups, then exit')
    parser.add_argument('--list-examples', action='store_true', help='List all available NimBLE examples, then exit')
    parser.add_argument('--list-targets', action='store_true', help='List all BLE-supported target chips, then exit')
    parser.add_argument(
        '--custom',
        default=None,
        help='Build a single custom variant with explicit flag values. '
        'Comma-separated KEY=VAL pairs, e.g. --custom BT_NIMBLE_AOA_AOD=n,BT_NIMBLE_EXT_ADV=y',
    )
    parser.add_argument('--keep-builds', action='store_true', help="Don't delete temp build dirs on completion")
    parser.add_argument('--parallel', type=int, default=1, help='Number of parallel builds (default: 1)')
    parser.add_argument('--output', default=None, help='Write report to file (default: stdout)')
    parser.add_argument(
        '--variant-mode',
        choices=['group', 'flag-combos'],
        default='group',
        help='Variant generation strategy: "group" (default) or "flag-combos". '
        '"flag-combos" does single-flag toggles first, then multi-flag combinations.',
    )
    parser.add_argument(
        '--combo-max-flags',
        type=int,
        default=10,
        help='Maximum number of toggleable flags allowed in --variant-mode flag-combos (default: 10).',
    )
    parser.add_argument(
        '--combo-max-size',
        type=int,
        default=None,
        help='Maximum size of flag combinations in --variant-mode flag-combos (default: all sizes).',
    )
    parser.add_argument(
        '--default-only',
        action='store_true',
        help='Build examples with default settings only (single default variant, no generated toggles).',
    )
    args = parser.parse_args()

    # Resolve IDF path
    idf_path = os.path.abspath(args.idf_path) if args.idf_path else ''
    if not idf_path:
        # Try to infer from script location (tools/bt/ inside IDF)
        script_dir = os.path.dirname(os.path.abspath(__file__))
        candidate = os.path.normpath(os.path.join(script_dir, '..', '..'))
        if os.path.isfile(os.path.join(candidate, 'tools', 'idf.py')):
            idf_path = candidate

    if not idf_path or not os.path.isdir(idf_path):
        print('Error: Cannot determine IDF_PATH. Use --idf-path or set $IDF_PATH.', file=sys.stderr)
        sys.exit(1)

    # --list-targets
    if args.list_targets:
        print(f'\nBLE-supported targets ({len(BLE_SUPPORTED_TARGETS)}):\n')
        for t in BLE_SUPPORTED_TARGETS:
            print(f'  {t}')
        print(f'\nUsage: --targets {",".join(BLE_SUPPORTED_TARGETS[:3])},...  or  --all-targets')
        return

    # --list-examples
    if args.list_examples:
        examples = discover_examples(idf_path)
        print(f'\nAvailable NimBLE examples ({len(examples)} found):\n')
        for e in examples:
            print(f'  {e}')
        print(f'\nUsage: --examples {",".join(examples[:3])},...  or  --all-examples')
        return

    kconfig_path = os.path.join(idf_path, 'components', 'bt', 'host', 'nimble', 'Kconfig.in')
    if not os.path.isfile(kconfig_path):
        print(f'Error: Kconfig.in not found at {kconfig_path}', file=sys.stderr)
        sys.exit(1)

    # Parse
    flags = parse_kconfig(kconfig_path)
    groups = group_flags(flags)
    flag_defaults = {f.name: f.default for f in flags if f.default in ('y', 'n')}
    service_flag_names = [f.name for f in groups.get('services', []) if not f.in_choice and f.default in ('y', 'n')]

    # --list-flags
    if args.list_flags:
        print(f'\nNimBLE boolean config flags ({len(flags)} found):\n')
        print(f'  {"Name":<50} {"Default":<10} {"Choice":<8} Dependencies')
        print(f'  {"-" * 50} {"-" * 10} {"-" * 8} {"-" * 30}')
        for f in flags:
            dep_str = ' && '.join(f.depends_on + f.if_conditions) if (f.depends_on or f.if_conditions) else '-'
            default = f.default or '?'
            choice = 'yes' if f.in_choice else 'no'
            print(f'  {f.name:<50} {default:<10} {choice:<8} {dep_str}')
        return

    # --list-groups
    if args.list_groups:
        print(f'\nNimBLE flag groups ({len(groups)} groups):\n')
        for gname, gflags in groups.items():
            defaults_on = [f.name for f in gflags if f.default == 'y' and not f.in_choice]
            defaults_off = [f.name for f in gflags if f.default == 'n' and not f.in_choice]
            choice_flags = [f.name for f in gflags if f.in_choice]
            print(f'  {gname} ({len(gflags)} flags)')
            if defaults_on:
                print(f'    default=y ({len(defaults_on)}): {", ".join(defaults_on)}')
            if defaults_off:
                print(f'    default=n ({len(defaults_off)}): {", ".join(defaults_off)}')
            if choice_flags:
                print(f'    in choice ({len(choice_flags)}): {", ".join(choice_flags)}')
            no_default = [f.name for f in gflags if f.default is None and not f.in_choice]
            if no_default:
                print(f'    no default ({len(no_default)}): {", ".join(no_default)}')
            print()
        return

    # Filter groups if requested
    if args.groups:
        selected = [g.strip() for g in args.groups.split(',')]
        unknown = [g for g in selected if g not in groups]
        if unknown:
            print(f'Error: Unknown groups: {", ".join(unknown)}', file=sys.stderr)
            print(f'Available groups: {", ".join(groups.keys())}', file=sys.stderr)
            sys.exit(1)
        groups = OrderedDict((k, v) for k, v in groups.items() if k in selected)

    # Build variants
    variants: list[tuple[str, list[tuple[str, str]]]]
    if args.default_only:
        variants = [('default', [])]
    elif args.custom:
        # Parse --custom KEY=VAL,KEY=VAL,... into a single custom variant
        custom_toggles: list[tuple[str, str]] = []
        for pair in args.custom.split(','):
            pair = pair.strip()
            if '=' not in pair:
                print(f'Error: Invalid --custom format: {pair!r}. Expected KEY=y or KEY=n.', file=sys.stderr)
                sys.exit(1)
            key, val = pair.split('=', 1)
            key = key.strip()
            val = val.strip()
            if val not in ('y', 'n'):
                print(f'Error: Invalid value for {key}: {val!r}. Must be y or n.', file=sys.stderr)
                sys.exit(1)
            if _is_ignored_flag(key):
                continue
            custom_toggles.append((key, val))
        variants = [('custom', custom_toggles)]
    else:
        if args.variant_mode == 'flag-combos':
            try:
                variants = build_flag_combo_variants(groups, args.combo_max_flags, args.combo_max_size)
            except ValueError as e:
                print(f'Error: {e}', file=sys.stderr)
                sys.exit(1)
        else:
            variants = build_variants(flags, groups)

    # Apply cross-flag constraints so generated variants stay semantically valid.
    variants = [
        (vname, _normalize_variant_toggles(toggles, flag_defaults, service_flag_names)) for vname, toggles in variants
    ]

    # Resolve target list
    if args.all_targets:
        target_list = list(BLE_SUPPORTED_TARGETS)
    elif args.targets:
        target_list = [t.strip() for t in args.targets.split(',')]
    elif args.target:
        target_list = [args.target.strip()]
    else:
        target_list = ['esp32c3']

    # Validate targets
    invalid = [t for t in target_list if t not in BLE_SUPPORTED_TARGETS]
    if invalid:
        print(f'Warning: Unrecognized targets (may fail): {", ".join(invalid)}', file=sys.stderr)

    # Resolve example list
    if args.all_examples:
        example_names = discover_examples(idf_path)
        if not example_names:
            print('Error: No NimBLE examples found.', file=sys.stderr)
            sys.exit(1)
    else:
        example_names = [normalize_example_name(e) for e in args.examples.split(',')]

    excluded_in_request = [e for e in example_names if is_example_excluded(e)]
    if excluded_in_request:
        print(f'  Excluded examples: {", ".join(excluded_in_request)}')
        example_names = [e for e in example_names if not is_example_excluded(e)]

    print('\nNimBLE Compilation Coverage Check')
    print(f'  Targets: {", ".join(target_list)}')
    print(f'  Examples: {", ".join(example_names)}')
    print(f'  Variants: {len(variants)}')
    print(f'  Parallel: {args.parallel}')
    print()

    # Prepare directories for sdkconfigs, builds (in /tmp), and logs (in ./results)
    tmp_base = tempfile.mkdtemp(prefix='nimble_cc_')
    sdkconfig_dir = os.path.join(tmp_base, 'sdkconfigs')
    builds_dir = os.path.join(tmp_base, 'builds')
    os.makedirs(sdkconfig_dir)
    os.makedirs(builds_dir)

    # Logs go into ./results/<timestamp>/ so each run is preserved
    timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    logs_dir = os.path.join(os.getcwd(), 'results', timestamp)
    os.makedirs(logs_dir, exist_ok=True)

    print(f'  Logs dir: {logs_dir}')
    print()

    try:
        # Generate sdkconfig files and build toggles lookup
        sdkconfig_paths: dict[str, str | None] = {}
        toggles_map: dict[str, list[tuple[str, str]]] = {}
        if args.default_only:
            sdkconfig_paths['default'] = None
            toggles_map['default'] = []
        else:
            for vname, toggles in variants:
                path = os.path.join(sdkconfig_dir, f'sdkconfig.{vname}')
                generate_sdkconfig(toggles, path)
                sdkconfig_paths[vname] = path
                toggles_map[vname] = toggles

        # Pre-compute supported targets for each example
        example_targets: dict[str, list[str] | None] = {}
        example_default_vals: dict[tuple[str, str], dict[str, str]] = {}
        for ename in example_names:
            example_targets[ename] = get_example_supported_targets(idf_path, ename)

        # Build all target/variant/example combinations
        build_tasks = []
        skipped = 0
        skipped_deny = 0
        skipped_conflicts = 0
        skipped_global_constraints = 0
        skipped_example_constraints = 0
        conflict_examples: list[str] = []
        global_constraint_examples: list[str] = []
        example_constraint_examples: list[str] = []
        for target in target_list:
            for vname, _ in variants:
                for ename in example_names:
                    example_path = resolve_example_path(idf_path, ename)
                    if not os.path.isdir(example_path):
                        print(f'Warning: Example not found: {example_path}', file=sys.stderr)
                        continue
                    supported = example_targets[ename]
                    override_allowlist = get_example_target_allowlist(ename)
                    if override_allowlist is not None:
                        supported = override_allowlist
                    if supported is not None and target not in supported:
                        skipped += 1
                        continue
                    denylist = get_example_target_denylist(ename)
                    if target in denylist:
                        skipped_deny += 1
                        continue
                    defaults_key = (ename, target)
                    if defaults_key not in example_default_vals:
                        example_default_vals[defaults_key] = get_example_defaults_values(example_path, target)
                    per_example_defaults = example_default_vals[defaults_key]
                    if not args.default_only:
                        violations = _violates_global_constraints(toggles_map[vname], flag_defaults)
                        if violations:
                            skipped_global_constraints += 1
                            if len(global_constraint_examples) < 8:
                                global_constraint_examples.append(f'{target}/{ename}/{vname}: {violations[0]}')
                            continue
                        example_violations = _violates_example_constraints(
                            ename, toggles_map[vname], flag_defaults, per_example_defaults
                        )
                        if example_violations:
                            skipped_example_constraints += 1
                            if len(example_constraint_examples) < 8:
                                example_constraint_examples.append(f'{target}/{ename}/{vname}: {example_violations[0]}')
                            continue
                        required_cfg = get_example_required_configs(ename)
                        target_required_cfg = get_target_required_configs(target)
                        if target_required_cfg:
                            required_cfg = {**required_cfg, **target_required_cfg}
                        if required_cfg:
                            conflicts = _find_required_conflicts(
                                toggles_map[vname], required_cfg, flag_defaults, per_example_defaults
                            )
                            if conflicts:
                                skipped_conflicts += 1
                                if len(conflict_examples) < 8:
                                    cfg, req_val, actual_val = conflicts[0]
                                    conflict_examples.append(
                                        f'{target}/{ename}/{vname}: '
                                        f'CONFIG_{cfg} requires {req_val}, '
                                        f'variant sets {actual_val}'
                                    )
                                continue
                    build_dir = os.path.join(builds_dir, f'{target}_{vname}_{ename}')
                    build_tasks.append(
                        (
                            vname,
                            ename,
                            target,
                            idf_path,
                            example_path,
                            sdkconfig_paths[vname],
                            build_dir,
                            logs_dir,
                            args.keep_builds,
                        )
                    )
        print(f'  Total builds: {len(build_tasks)}')
        if skipped:
            print(f'  Skipped: {skipped} (target not supported by example)')
        if skipped_deny:
            print(f'  Skipped: {skipped_deny} (target denied by explicit rules)')
        if skipped_global_constraints:
            print(f'  Skipped: {skipped_global_constraints} (global NimBLE constraints)')
            for line in global_constraint_examples:
                print(f'    - {line}')
            if skipped_global_constraints > len(global_constraint_examples):
                print(f'    - ... and {skipped_global_constraints - len(global_constraint_examples)} more')
        if skipped_example_constraints:
            print(f'  Skipped: {skipped_example_constraints} (example-specific constraints)')
            for line in example_constraint_examples:
                print(f'    - {line}')
            if skipped_example_constraints > len(example_constraint_examples):
                print(f'    - ... and {skipped_example_constraints - len(example_constraint_examples)} more')
        if skipped_conflicts:
            print(f'  Skipped: {skipped_conflicts} (conflicts with example-required configs)')
            for line in conflict_examples:
                print(f'    - {line}')
            if skipped_conflicts > len(conflict_examples):
                print(f'    - ... and {skipped_conflicts - len(conflict_examples)} more')
        print()

        results = []
        completed = 0

        def _print_progress(r: tuple[str, str, str, bool, list[str], str], completed: int, total: int) -> None:
            status = 'PASS' if r[3] else 'FAIL'
            line = f'  [{completed}/{total}] {r[2]}/{r[1]}/{r[0]}: {status}'
            if not r[3]:
                line += f'  -> {r[5]}'
            print(line)

        def _print_starting(
            task: tuple[str, str, str, str, str, str | None, str, str, bool], idx: int, total: int
        ) -> None:
            vname, ename, target = task[0], task[1], task[2]
            ts = datetime.datetime.now().strftime('%H:%M:%S')
            print(f'{ts} [{idx}/{total}] Building {target}/{ename}/{vname} ...', flush=True)

        if args.parallel > 1:
            with ProcessPoolExecutor(max_workers=args.parallel) as pool:
                futures = {}
                for idx, task in enumerate(build_tasks, start=1):
                    _print_starting(task, idx, len(build_tasks))
                    futures[pool.submit(_do_single_build, task)] = task
                for future in as_completed(futures):
                    r = future.result()
                    results.append(r)
                    completed += 1
                    _print_progress(r, completed, len(build_tasks))
        else:
            for task in build_tasks:
                completed += 1
                _print_starting(task, completed, len(build_tasks))
                r = _do_single_build(task)
                results.append(r)
                _print_progress(r, completed, len(build_tasks))

        # Sort results by target, variant, example for consistent output
        results.sort(key=lambda x: (x[2], x[0], x[1]))

        # Report
        out_file = None
        if args.output:
            out_file = open(args.output, 'w')

        print_report(results, target_list, out_file, variant_toggles=toggles_map)

        if out_file:
            out_file.close()

        # Exit code: non-zero if any build failed
        if any(not r[3] for r in results):
            sys.exit(1)

    finally:
        if not args.keep_builds:
            shutil.rmtree(tmp_base, ignore_errors=True)
        else:
            print(f'\nBuild artifacts kept at: {tmp_base}')
        print(f'Build logs: {logs_dir}')


if __name__ == '__main__':
    main()
