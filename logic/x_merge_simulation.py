#!/usr/bin/env python3
"""
x_merge_simulation.py

X-parameter merge stage.
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
from pathlib import Path

from path_utils import resolve_source_target
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from merger import (  # type: ignore
    flatten_circuit_data,
    get_memo_class,
    get_project_output_dir,
    get_resolved_dir,
)

X_SETTING_KEYS = [
    "z0",
    "simulation_input_ports",
    "simulation_output_ports",
    "simulation_freq_start",
    "simulation_freq_stop",
    "simulation_freq_points",
    "simulation_sweep_type",
    "simulation_figure_title",
    "simulation_variables",
    "multimode",
    "multimode_plot",
    "multimode_symplectic_tolerance",
    "x-params",
    "x_dc_ports",
    "x_dc_currents",
    "x_pump_frequencies",
    "x_pump_ports",
    "x_pump_currents",
    "x_modulation_harmonics",
    "x_pump_harmonics",
    "x_threewave_mixing",
    "x_fourwave_mixing",
    "x_input_ports",
    "x_output_ports",
]

GROUND_LABELS = {"0", "P_0", "GND", "gnd", "ground"}
Endpoint = Tuple[str, str]


class UnionFind:
    def __init__(self) -> None:
        self.parent: Dict[str, str] = {}

    def find(self, item: str) -> str:
        if self.parent.setdefault(item, item) == item:
            return item
        self.parent[item] = self.find(self.parent[item])
        return self.parent[item]

    def union(self, a: str, b: str) -> None:
        ra = self.find(a)
        rb = self.find(b)
        if ra == rb:
            return
        if rb == "0":
            self.parent[ra] = rb
        elif ra == "0":
            self.parent[rb] = ra
        else:
            self.parent[rb] = ra


def load_json(path: Path) -> Dict[str, Any]:
    with open(path, "r") as f:
        return json.load(f)


def save_json(path: Path, data: Dict[str, Any]) -> None:
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def bool_true(value: Any) -> bool:
    return value is True or str(value).lower() == "true"


def endpoint_key(uid: str, port: Any) -> str:
    return f"{uid}:{port}"


def is_p_instance(inst: Dict[str, Any]) -> bool:
    return str(inst.get("type_name", "")).lower() == "p"


def is_r_instance(inst: Dict[str, Any]) -> bool:
    return str(inst.get("type_name", "")).lower() in {"r", "resistor"}


def get_primary_value(obj: Dict[str, Any], default: Any = None) -> Any:
    order = obj.get("parameter_order", []) or []
    if not order:
        return default
    key = order[0]
    params = obj.get("parameters", {}) or {}
    if key in params and params[key] not in ["", None]:
        return params[key]
    return default


def extract_p_number(inst: Dict[str, Any]) -> int:
    value = get_primary_value(inst, default=None)
    if value is None:
        raise ValueError(f"P instance {inst.get('uid')} has no port number")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"P instance {inst.get('uid')} has non-integer port number {value!r}"
        ) from exc


def numeric_float(value: Any, context: str) -> float:
    def strip_outer_parens(s: str) -> str:
        s = s.strip()
        while s.startswith("(") and s.endswith(")"):
            depth = 0
            balanced = True
            for i, ch in enumerate(s):
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0 and i != len(s) - 1:
                        balanced = False
                        break
            if balanced:
                s = s[1:-1].strip()
            else:
                break
        return s

    try:
        return float(strip_outer_parens(str(value)))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Expected numeric value for {context}, got {value!r}") from exc


def resistor_value(inst: Dict[str, Any]) -> Any:
    value = get_primary_value(inst, default=None)
    if value is None:
        raise ValueError(f"R instance {inst.get('uid')} has no resistance value")
    return value


def build_alias_resolver(aliases: Dict[Tuple[str, str], Endpoint]):
    def resolve(uid: str, port: Any) -> Endpoint:
        seen: Set[Endpoint] = set()
        curr = (uid, str(port))
        while curr in aliases:
            if curr in seen:
                raise ValueError(f"Alias cycle detected at {curr[0]}:{curr[1]}")
            seen.add(curr)
            next_uid, next_port = aliases[curr]
            curr = (next_uid, str(next_port))
        return curr

    return resolve


def normalize_top_pins(original_data: Dict[str, Any], resolve_alias) -> List[Dict[str, Any]]:
    pins = []
    for pin in original_data.get("pins", []) or []:
        uid = pin.get("instance_uid")
        if not uid:
            continue
        resolved_uid, resolved_port = resolve_alias(uid, pin.get("port"))
        new_pin = dict(pin)
        new_pin["instance_uid"] = resolved_uid
        new_pin["port"] = resolved_port
        pins.append(new_pin)
    return pins


def build_union_find(data: Dict[str, Any]) -> UnionFind:
    uf = UnionFind()

    for inst in data.get("instances", []) or []:
        uid = inst.get("uid")
        if not uid:
            continue
        port_names = inst.get("port_names", []) or []
        if not port_names:
            port_names = [f"p{port}" for port in range(1, int(inst.get("port_count", 2)) + 1)]
        for port in port_names:
            uf.find(endpoint_key(uid, port))

    for wire in data.get("wires", []) or []:
        uf.union(
            endpoint_key(wire["source_instance_uid"], wire.get("source_port")),
            endpoint_key(wire["target_instance_uid"], wire.get("target_port")),
        )

    label_groups: Dict[str, List[str]] = {}
    for label in data.get("labels", []) or []:
        uid = label.get("instance_uid")
        if not uid:
            continue
        key = endpoint_key(uid, label.get("port"))
        name = str(label.get("name", ""))
        if name in GROUND_LABELS:
            uf.union(key, "0")
        elif name:
            label_groups.setdefault(name, []).append(key)

    for keys in label_groups.values():
        first = keys[0]
        for other in keys[1:]:
            uf.union(first, other)

    return uf


def instance_by_uid(data: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {inst["uid"]: inst for inst in data.get("instances", []) if inst.get("uid")}


def p_uids_on_root(data: Dict[str, Any], uf: UnionFind, root: str) -> Set[str]:
    out: Set[str] = set()
    for inst in data.get("instances", []) or []:
        if not is_p_instance(inst):
            continue
        uid = inst["uid"]
        port_names = inst.get("port_names", []) or []
        if not port_names:
            port_names = [f"p{port}" for port in range(1, int(inst.get("port_count", 2)) + 1)]
        for port in port_names:
            if uf.find(endpoint_key(uid, port)) == root:
                out.add(uid)
    return out


def hierarchical_prefix_score(endpoint_uid: str, candidate_uid: str) -> int:
    endpoint_parts = str(endpoint_uid).split("_")
    candidate_parts = str(candidate_uid).split("_")
    score = 0
    for left, right in zip(endpoint_parts, candidate_parts):
        if left != right:
            break
        score += 1
    return score


def p_uid_for_endpoint_or_net(data: Dict[str, Any], uf: UnionFind, endpoint: Endpoint) -> Optional[str]:
    uid, port = endpoint
    inst = instance_by_uid(data).get(uid)

    if inst and is_p_instance(inst):
        return uid

    root = uf.find(endpoint_key(uid, port))
    candidates = p_uids_on_root(data, uf, root)
    if not candidates:
        return None
    if len(candidates) != 1:
        scored = [
            (hierarchical_prefix_score(uid, candidate), candidate)
            for candidate in candidates
        ]
        best_score = max(score for score, _candidate in scored)
        best_candidates = [candidate for score, candidate in scored if score == best_score and score > 0]
        if len(best_candidates) == 1:
            return best_candidates[0]
        raise ValueError(
            f"Joined endpoint {uid}:{port} must resolve to exactly one P block on its net; "
            f"found {sorted(candidates)}"
        )
    return next(iter(candidates))


def remove_ground_instances(data: Dict[str, Any]) -> Dict[str, Any]:
    ground_uids = {
        inst["uid"]
        for inst in data.get("instances", []) or []
        if "gnd" in str(inst.get("type_name", "")).lower()
        or "ground" in str(inst.get("type_name", "")).lower()
    }
    if not ground_uids:
        return data

    out = dict(data)
    out["instances"] = [
        inst for inst in data.get("instances", []) or [] if inst.get("uid") not in ground_uids
    ]

    labels = list(data.get("labels", []) or [])
    wires = []
    for wire in data.get("wires", []) or []:
        src = wire.get("source_instance_uid")
        tgt = wire.get("target_instance_uid")
        src_g = src in ground_uids
        tgt_g = tgt in ground_uids

        if src_g and tgt_g:
            continue
        if src_g:
            labels.append({"name": "0", "instance_uid": tgt, "port": wire.get("target_port")})
        elif tgt_g:
            labels.append({"name": "0", "instance_uid": src, "port": wire.get("source_port")})
        else:
            wires.append(wire)

    out["wires"] = wires
    out["labels"] = labels
    return out


def find_parallel_z0_resistor_uid(
    data: Dict[str, Any],
    uf: UnionFind,
    p_uid: str,
    z0_value: float,
) -> Optional[str]:
    if p_uid not in instance_by_uid(data):
        raise ValueError(f"Internal P block {p_uid} was not found")

    p_inst = instance_by_uid(data)[p_uid]
    p_ports = p_inst.get("port_names", []) or [
        f"p{port}" for port in range(1, int(p_inst.get("port_count", 2)) + 1)
    ]
    p_roots = {
        uf.find(endpoint_key(p_uid, port))
        for port in p_ports
    }
    if len(p_roots) != 2:
        raise ValueError(f"P block {p_uid} has both terminals on the same net")

    matches = []
    for inst in data.get("instances", []) or []:
        if not is_r_instance(inst):
            continue
        uid = inst["uid"]
        if str(uid).startswith("XR_"):
            continue
        r_ports = inst.get("port_names", []) or [
            f"p{port}" for port in range(1, int(inst.get("port_count", 2)) + 1)
        ]
        r_roots = {
            uf.find(endpoint_key(uid, port))
            for port in r_ports
        }
        if r_roots == p_roots:
            try:
                r_value = numeric_float(resistor_value(inst), f"resistor {uid}")
            except ValueError:
                continue
            if math.isclose(r_value, z0_value, rel_tol=1e-9, abs_tol=1e-12):
                matches.append(uid)

    return matches[0] if matches else None


def raw_endpoint_from_top_wire_endpoint(
    wire: Dict[str, Any],
    which: str,
) -> Endpoint:
    if which == "source":
        return wire["source_instance_uid"], str(wire.get("source_port"))
    if which == "target":
        return wire["target_instance_uid"], str(wire.get("target_port"))
    raise ValueError(f"Unknown endpoint side {which!r}")
def internal_join_pairs_from_top_wires(
    original_data: Dict[str, Any],
    merged_data: Dict[str, Any],
    aliases: Dict[Tuple[str, str], Endpoint],
    resolve_alias,
) -> List[Dict[str, Any]]:
    """
    Return the internal P-block pairs that became internal because of top-level
    wires.

    This is generic; it does not know anything about TWPAs. Each top-level wire
    means:
        left subcircuit boundary endpoint <-> right subcircuit boundary endpoint

    For that join we identify:
        - the flattened endpoint on each side,
        - the electrical root of the joined net,
        - the boundary P block belonging to each side.

    Later removal uses these join records to reconnect only the joined signal
    net. It does not reconnect every net touched by the removed P/R blocks.
    """
    uf = build_union_find(merged_data)
    joins: List[Dict[str, Any]] = []

    for wire in original_data.get("wires", []) or []:
        if (
            str(wire.get("source_instance_uid", "")).startswith(("XP_", "XR_"))
            or str(wire.get("target_instance_uid", "")).startswith(("XP_", "XR_"))
        ):
            continue

        src_top = (
            wire["source_instance_uid"],
            str(wire.get("source_port")),
        )
        tgt_top = (
            wire["target_instance_uid"],
            str(wire.get("target_port")),
        )

        # Resolve through hierarchy to real flattened primitive endpoints.
        # Alias edges cross hierarchy only; they do not cross electrical wires.
        src_ep = resolve_alias_to_flat_endpoint(aliases, merged_data, *src_top)
        tgt_ep = resolve_alias_to_flat_endpoint(aliases, merged_data, *tgt_top)

        src_root = uf.find(endpoint_key(*src_ep))
        tgt_root = uf.find(endpoint_key(*tgt_ep))

        if src_root != tgt_root:
            raise ValueError(
                f"Top-level joined endpoints are not electrically connected after flattening: "
                f"{src_ep[0]}:{src_ep[1]} root={src_root}, "
                f"{tgt_ep[0]}:{tgt_ep[1]} root={tgt_root}"
            )

        src_p = p_uid_for_endpoint_or_net(merged_data, uf, src_ep)
        tgt_p = p_uid_for_endpoint_or_net(merged_data, uf, tgt_ep)

        if src_p is None and tgt_p is None:
            continue

        if (
            (src_p is not None and str(src_p).startswith("XP_"))
            or (tgt_p is not None and str(tgt_p).startswith("XP_"))
        ):
            continue

        joins.append(
            {
                "wire": dict(wire),
                "source_endpoint": src_ep,
                "target_endpoint": tgt_ep,
                "source_p_uid": src_p,
                "target_p_uid": tgt_p,
                "join_root": src_root,
            }
        )

    return joins

def resolve_alias_to_flat_endpoint(
    aliases: Dict[Tuple[str, str], Endpoint],
    flattened_data: Dict[str, Any],
    uid: str,
    port: Any,
) -> Endpoint:
    """
    Resolve a hierarchical endpoint to an actual flattened instance endpoint.

    This follows only alias edges created by hierarchy flattening. It does not
    follow electrical wires, so it still preserves which side of a top-level
    join the endpoint came from.

    Why this is needed:
        With the current port model, a parent numeric port N maps through the
        child pins[N - 1] entry. For nested HB top blocks this may require more
        than one alias hop:

            TWPA1:1
              -> TWPA1_IT1:3
              -> TWPA1_IT1_P1:2

        The old one-step resolver stopped at TWPA1_IT1:3, which is not a real
        flattened primitive instance. Then net lookup could not find the P
        block on that endpoint.
    """
    flat_uids = {
        inst["uid"]
        for inst in flattened_data.get("instances", []) or []
        if inst.get("uid")
    }

    seen: Set[Endpoint] = set()
    curr = (uid, str(port))

    while curr in aliases:
        if curr in seen:
            raise ValueError(f"Alias cycle detected at {curr[0]}:{curr[1]}")
        seen.add(curr)
        next_uid, next_port = aliases[curr]
        curr = (next_uid, str(next_port))

        if curr[0] in flat_uids:
            return curr

    if curr[0] not in flat_uids:
        raise ValueError(
            f"Endpoint {uid}:{port} resolved through hierarchy to {curr[0]}:{curr[1]}, "
            "but that uid is not present in the flattened circuit instances."
        )

    return curr

def root_members_by_endpoint(data: Dict[str, Any], uf: UnionFind) -> Dict[str, List[Endpoint]]:
    members: Dict[str, List[Endpoint]] = {}

    for inst in data.get("instances", []) or []:
        uid = inst.get("uid")
        if not uid:
            continue

        port_names = inst.get("port_names", []) or []
        if not port_names:
            port_names = [f"p{port}" for port in range(1, int(inst.get("port_count", 2)) + 1)]
        for port in port_names:
            root = uf.find(endpoint_key(uid, port))
            members.setdefault(root, []).append((uid, port))

    return members


def root_has_ground(data: Dict[str, Any], uf: UnionFind, root: str) -> bool:
    if root == "0":
        return True

    for label in list(data.get("labels", []) or []) + list(data.get("pins", []) or []):
        uid = label.get("instance_uid")
        if not uid:
            continue

        name = str(label.get("name", ""))
        if name not in GROUND_LABELS:
            continue

        if uf.find(endpoint_key(uid, label.get("port"))) == root:
            return True

    return False


def append_ground_labels_for_removed_ground_roots(
    data: Dict[str, Any],
    uf: UnionFind,
    root_members: Dict[str, List[Endpoint]],
    remove_uids: Set[str],
    kept_labels: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    labels = list(kept_labels)
    existing_label_keys = {
        (label.get("name"), label.get("instance_uid"), str(label.get("port")))
        for label in labels
    }

    for root, members in sorted(root_members.items()):
        if not any(uid in remove_uids for uid, _port in members):
            continue
        if not root_has_ground(data, uf, root):
            continue

        remaining = sorted(set((uid, port) for uid, port in members if uid not in remove_uids))
        for uid, port in remaining:
            key = ("0", uid, str(port))
            if key in existing_label_keys:
                continue
            labels.append({
                "name": "0",
                "instance_uid": uid,
                "port": port,
            })
            existing_label_keys.add(key)

    return labels


def make_wire(a: Endpoint, b: Endpoint) -> Dict[str, Any]:
    return {
        "source_instance_uid": a[0],
        "source_port": a[1],
        "target_instance_uid": b[0],
        "target_port": b[1],
        "name": "",
    }


def remove_internal_p_blocks_and_z0_resistors(
    data: Dict[str, Any],
    internal_joins: List[Dict[str, Any]],
    z0_value: float,
) -> Dict[str, Any]:
    """
    Remove internal boundary P blocks and their parallel z0 resistors while
    preserving only the nets that correspond to actual top-level joins.

    Important:
        Do not reconnect every net touched by removed P/R blocks. That is too
        broad and can turn ground/reference or shunt nets into artificial
        repeated-group boundaries.

    Generic behavior:
        For each top-level join, identify the joined electrical root before
        removal. After removing the two P blocks and their z0 shunt resistors,
        reconnect only the remaining endpoints on that joined root.

    This does not hardcode TWPAs. The top-level wires define which subcircuit
    boundaries are being merged.
    """
    if not internal_joins:
        return data

    uf = build_union_find(data)

    internal_p_uids: Set[str] = set()
    join_roots: Set[str] = set()

    for join in internal_joins:
        if join.get("source_p_uid") is not None:
            internal_p_uids.add(str(join["source_p_uid"]))
        if join.get("target_p_uid") is not None:
            internal_p_uids.add(str(join["target_p_uid"]))
        join_roots.add(str(join["join_root"]))

    resistor_uids = {
        uid for uid in (
            find_parallel_z0_resistor_uid(data, uf, p_uid, z0_value)
            for p_uid in sorted(internal_p_uids)
        )
        if uid is not None
    }
    if resistor_uids:
        print(
            "      -> [WARNING] Existing z0 resistor(s) found in parallel with "
            f"merged internal P block(s); removing them with the replaced ports: {sorted(resistor_uids)}"
        )

    remove_uids = set(internal_p_uids) | resistor_uids
    root_members = root_members_by_endpoint(data, uf)

    kept_wires = [
        wire for wire in data.get("wires", []) or []
        if wire.get("source_instance_uid") not in remove_uids
        and wire.get("target_instance_uid") not in remove_uids
    ]

    kept_labels = [
        label for label in data.get("labels", []) or []
        if label.get("instance_uid") not in remove_uids
    ]

    existing_wire_keys = {
        (
            wire.get("source_instance_uid"),
            str(wire.get("source_port")),
            wire.get("target_instance_uid"),
            str(wire.get("target_port")),
        )
        for wire in kept_wires
    }
    existing_wire_keys |= {
        (b, bp, a, ap)
        for (a, ap, b, bp) in existing_wire_keys
    }

    existing_label_keys = {
        (label.get("name"), label.get("instance_uid"), str(label.get("port")))
        for label in kept_labels
    }

    replacement_wires: List[Dict[str, Any]] = []
    replacement_labels: List[Dict[str, Any]] = []

    for root in sorted(join_roots):
        remaining = [
            ep for ep in root_members.get(root, [])
            if ep[0] not in remove_uids
        ]

        remaining = sorted(set(remaining))

        if root_has_ground(data, uf, root):
            for uid, port in remaining:
                key = ("0", uid, str(port))
                if key not in existing_label_keys:
                    replacement_labels.append({
                        "name": "0",
                        "instance_uid": uid,
                        "port": port,
                    })
                    existing_label_keys.add(key)

            # A top-level join on ground/reference is represented by labels,
            # not by artificial signal wires.
            continue

        if len(remaining) < 2:
            raise ValueError(
                f"Top-level join root {root!r} has fewer than two remaining "
                f"endpoints after removing internal P/z0 blocks. Remaining={remaining}"
            )

        # Reconnect only the joined net. A star is fine here because every
        # endpoint in `remaining` was already on the same electrical net before
        # removal. We intentionally do this only for join_roots, not all roots
        # touched by removed P/R elements.
        anchor = remaining[0]
        for other in remaining[1:]:
            key = (anchor[0], str(anchor[1]), other[0], str(other[1]))
            if key in existing_wire_keys:
                continue

            replacement_wires.append(make_wire(anchor, other))
            existing_wire_keys.add(key)
            existing_wire_keys.add((other[0], str(other[1]), anchor[0], str(anchor[1])))

    out = dict(data)
    out["instances"] = [
        inst for inst in data.get("instances", []) or [] if inst.get("uid") not in remove_uids
    ]
    out["wires"] = kept_wires + replacement_wires
    out["pins"] = [
        pin for pin in data.get("pins", []) or [] if pin.get("instance_uid") not in remove_uids
    ]
    out["labels"] = append_ground_labels_for_removed_ground_roots(
        data,
        uf,
        root_members,
        remove_uids,
        kept_labels + replacement_labels,
    )

    out["x_removed_internal_p_blocks"] = sorted(internal_p_uids)
    out["x_removed_internal_z0_resistors"] = sorted(resistor_uids)
    out["x_internal_join_records"] = internal_joins
    out["x_replacement_wires_after_internal_port_removal"] = replacement_wires
    out["x_replacement_ground_labels_after_internal_port_removal"] = [
        label for label in out["labels"] if label not in kept_labels
    ]
    return out


def remove_replaced_boundary_p_blocks(
    data: Dict[str, Any],
    z0_value: float,
) -> Dict[str, Any]:
    """
    X rewrite adds XP_* P blocks at explicit X input/output labels. If that
    label lands on an existing HB subcircuit P block, remove the older non-XP
    P block and its z0 shunt so there is a single JosephsonCircuits field for
    the boundary.
    """
    uf = build_union_find(data)
    remove_uids: Set[str] = set()
    xp_roots: Set[str] = set()

    for inst in data.get("instances", []) or []:
        uid = inst.get("uid")
        if not uid or not is_p_instance(inst) or not str(uid).startswith("XP_"):
            continue

        xp_root = uf.find(endpoint_key(uid, "p1"))
        root_remove_before = set(remove_uids)
        for other in data.get("instances", []) or []:
            other_uid = other.get("uid")
            if (
                not other_uid
                or other_uid == uid
                or str(other_uid).startswith("XP_")
                or not is_p_instance(other)
            ):
                continue

            port_names = other.get("port_names", []) or [
                f"p{port}" for port in range(1, int(other.get("port_count", 2)) + 1)
            ]
            if any(uf.find(endpoint_key(other_uid, port)) == xp_root for port in port_names):
                remove_uids.add(str(other_uid))
        if remove_uids != root_remove_before:
            xp_roots.add(xp_root)

    if not remove_uids:
        return data

    resistor_uids = {
        uid for uid in (
            find_parallel_z0_resistor_uid(data, uf, p_uid, z0_value)
            for p_uid in sorted(remove_uids)
        )
        if uid is not None
    }
    if resistor_uids:
        print(
            "      -> [WARNING] Existing z0 resistor(s) found in parallel with "
            f"replaced X boundary P block(s); removing them with the replaced ports: {sorted(resistor_uids)}"
        )
    remove_uids |= resistor_uids

    root_members = root_members_by_endpoint(data, uf)

    kept_wires = [
        wire for wire in data.get("wires", []) or []
        if wire.get("source_instance_uid") not in remove_uids
        and wire.get("target_instance_uid") not in remove_uids
    ]
    kept_labels = [
        label for label in data.get("labels", []) or [] if label.get("instance_uid") not in remove_uids
    ]

    existing_wire_keys = {
        (
            wire.get("source_instance_uid"),
            str(wire.get("source_port")),
            wire.get("target_instance_uid"),
            str(wire.get("target_port")),
        )
        for wire in kept_wires
    }
    existing_wire_keys |= {
        (b, bp, a, ap)
        for (a, ap, b, bp) in existing_wire_keys
    }

    replacement_wires: List[Dict[str, Any]] = []
    for root in sorted(xp_roots):
        remaining = sorted(set(ep for ep in root_members.get(root, []) if ep[0] not in remove_uids))
        xp_endpoints = [ep for ep in remaining if str(ep[0]).startswith("XP_")]
        if len(xp_endpoints) != 1:
            raise ValueError(
                f"Boundary replacement root {root!r} must contain exactly one XP_* endpoint; "
                f"found {xp_endpoints}"
            )

        anchor = xp_endpoints[0]
        for other in remaining:
            if other == anchor:
                continue
            key = (anchor[0], str(anchor[1]), other[0], str(other[1]))
            if key in existing_wire_keys:
                continue
            replacement_wires.append(make_wire(anchor, other))
            existing_wire_keys.add(key)
            existing_wire_keys.add((other[0], str(other[1]), anchor[0], str(anchor[1])))

    out = dict(data)
    out["instances"] = [
        inst for inst in data.get("instances", []) or [] if inst.get("uid") not in remove_uids
    ]
    out["wires"] = kept_wires + replacement_wires
    out["pins"] = [
        pin for pin in data.get("pins", []) or [] if pin.get("instance_uid") not in remove_uids
    ]
    out["labels"] = append_ground_labels_for_removed_ground_roots(
        data,
        uf,
        root_members,
        remove_uids,
        kept_labels,
    )
    out["x_removed_replaced_boundary_p_blocks"] = sorted(
        uid for uid in remove_uids if uid not in resistor_uids
    )
    out["x_removed_replaced_boundary_z0_resistors"] = sorted(resistor_uids)
    out["x_replacement_wires_after_boundary_port_removal"] = replacement_wires
    out["x_replacement_ground_labels_after_boundary_port_removal"] = [
        label for label in out["labels"] if label not in kept_labels
    ]
    return out


def remove_added_xr_when_existing_parallel_resistor(data: Dict[str, Any]) -> Dict[str, Any]:
    uf = build_union_find(data)
    remove_uids: Set[str] = set()
    dc_port_names = set(data.get("x_dc_ports", []) or [])

    for inst in data.get("instances", []) or []:
        xp_uid = inst.get("uid")
        if not xp_uid or not is_p_instance(inst) or not str(xp_uid).startswith("XP_"):
            continue

        xp_ports = inst.get("port_names", []) or [
            f"p{port}" for port in range(1, int(inst.get("port_count", 2)) + 1)
        ]
        if len(xp_ports) < 2:
            continue

        xp_roots = {
            uf.find(endpoint_key(xp_uid, xp_ports[0])),
            uf.find(endpoint_key(xp_uid, xp_ports[1])),
        }
        existing_parallel = []
        added_xr = []

        for candidate in data.get("instances", []) or []:
            uid = candidate.get("uid")
            if not uid or not is_r_instance(candidate):
                continue

            port_names = candidate.get("port_names", []) or [
                f"p{port}" for port in range(1, int(candidate.get("port_count", 2)) + 1)
            ]
            if len(port_names) < 2:
                continue

            roots = {
                uf.find(endpoint_key(uid, port_names[0])),
                uf.find(endpoint_key(uid, port_names[1])),
            }
            if roots != xp_roots:
                continue

            if str(uid).startswith("XR_"):
                added_xr.append(str(uid))
            else:
                existing_parallel.append(str(uid))

        if existing_parallel and added_xr:
            xp_label = str(xp_uid)[len("XP_"):]
            if xp_label in dc_port_names:
                raise ValueError(
                    f"DC X port {xp_label!r} already has resistor(s) across the same "
                    f"two-terminal port reference: {sorted(existing_parallel)}. "
                    "The current X merge cannot DC-bias a port that also has an "
                    "existing port-reference resistor."
                )
            print(
                f"      -> [WARNING] X port {xp_uid!r} already has resistor(s) "
                f"across the same two terminal port reference: {sorted(existing_parallel)}. "
                f"Removing added z0 shunt(s) {sorted(added_xr)} so the port has one resistor."
            )
            remove_uids.update(added_xr)

    if not remove_uids:
        return data

    out = dict(data)
    out["instances"] = [
        inst for inst in data.get("instances", []) or [] if inst.get("uid") not in remove_uids
    ]
    out["wires"] = [
        wire for wire in data.get("wires", []) or []
        if wire.get("source_instance_uid") not in remove_uids
        and wire.get("target_instance_uid") not in remove_uids
    ]
    out["labels"] = [
        label for label in data.get("labels", []) or [] if label.get("instance_uid") not in remove_uids
    ]
    out["x_removed_added_z0_shunts_due_to_existing_parallel_resistor"] = sorted(remove_uids)
    return out

def derive_pin_to_p_block_map(data: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    uf = build_union_find(data)
    insts = instance_by_uid(data)
    root_to_p_matches: Dict[str, List[Dict[str, Any]]] = {}
    required_names: Set[str] = set()
    for key in ["x_input_ports", "x_output_ports", "x_pump_ports", "x_dc_ports"]:
        required_names.update(str(item) for item in data.get(key, []) or [])

    for inst in data.get("instances", []) or []:
        if not is_p_instance(inst):
            continue
        uid = inst["uid"]
        p_number = extract_p_number(inst)
        port_names = inst.get("port_names", []) or []
        if not port_names:
            port_names = [f"p{port}" for port in range(1, int(inst.get("port_count", 2)) + 1)]
        for port in port_names:
            root = uf.find(endpoint_key(uid, port))
            root_to_p_matches.setdefault(root, []).append(
                {
                    "p_instance_uid": uid,
                    "p_local_port": port,
                    "p_port_number": p_number,
                }
            )

    out: Dict[str, Dict[str, Any]] = {}
    for pin in data.get("pins", []) or []:
        name = pin.get("name")
        uid = pin.get("instance_uid")
        if not name or not uid:
            continue
        if uid not in insts:
            raise ValueError(f"Pin {name!r} points to removed/missing instance {uid!r}")
        root = uf.find(endpoint_key(uid, pin.get("port")))
        matches = root_to_p_matches.get(root, [])
        if len(matches) > 1:
            direct_matches = [match for match in matches if match["p_instance_uid"] == uid]
            if len(direct_matches) == 1:
                matches = direct_matches
        if not matches and str(name) not in required_names:
            continue
        if len(matches) != 1:
            raise ValueError(
                f"Pin label {name!r} must resolve to exactly one remaining P block; "
                f"found {len(matches)} matches: {matches}"
            )
        match = matches[0]
        out[str(name)] = {
            "instance_uid": uid,
            "port": pin.get("port"),
            **match,
        }

    return out

def derive_boundary_pins_from_outgoing_pins(
    data: Dict[str, Any],
    pin_to_p: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Derive the final external boundary conditions from surviving outgoing pins.

    This intentionally does not assume a two-port device.

    Examples:
      - two 2-port blocks connected in series -> two surviving outgoing pins.
      - two 4-port blocks with three ports joined internally -> two surviving
        outgoing pins.
      - an N-port network -> N surviving outgoing pins.

    The source of truth is data["pins"] after internal joined P blocks have
    been removed. Top-level wires describe internal joins; top-level pins
    describe the remaining external boundary.
    """
    seen_names: Set[str] = set()
    boundaries: List[Dict[str, Any]] = []

    for pin in data.get("pins", []) or []:
        name = pin.get("name")
        if not name:
            continue

        name = str(name)

        if name in seen_names:
            raise ValueError(f"Duplicate surviving outgoing pin name {name!r}")

        if name not in pin_to_p:
            continue

        seen_names.add(name)
        info = pin_to_p[name]
        boundaries.append(
            {
                "name": name,
                "instance_uid": info["instance_uid"],
                "port": info["port"],
                "p_instance_uid": info["p_instance_uid"],
                "p_local_port": info["p_local_port"],
                "p_port_number": int(info["p_port_number"]),
            }
        )

    return boundaries


def validate_requested_x_labels_against_boundaries(
    data: Dict[str, Any],
    boundary_pins: List[Dict[str, Any]],
) -> None:
    """
    Check X labels against the final external boundary, not against an assumed
    two-port topology.
    """
    boundary_names = {entry["name"] for entry in boundary_pins}

    for key in ["x_input_ports", "x_pump_ports", "x_dc_ports", "x_output_ports"]:
        values = data.get(key, []) or []
        if not isinstance(values, list):
            raise ValueError(f"{key} must be a list of pin-label strings")

        for value in values:
            if not isinstance(value, str):
                raise ValueError(f"{key} must contain pin-label strings, got {value!r}")

            if value not in boundary_names:
                raise ValueError(
                    f"{key} label {value!r} is not a surviving outgoing boundary pin. "
                    f"Surviving boundary pins: {sorted(boundary_names)}"
                )



def labels_to_fields(
    data: Dict[str, Any],
    pin_to_p: Dict[str, Dict[str, Any]],
    source_key: str,
    target_key: str,
    *,
    required: bool = False,
) -> None:
    labels = data.get(source_key, []) or []
    if required and not labels:
        raise ValueError(f"Missing required X setting {source_key}")
    if not isinstance(labels, list):
        raise ValueError(f"{source_key} must be a list of pin-label strings")

    fields = []
    for label in labels:
        if not isinstance(label, str):
            raise ValueError(f"{source_key} must contain pin-label strings, got {label!r}")
        if label not in pin_to_p:
            raise ValueError(
                f"{source_key} label {label!r} does not correspond to a remaining P block. "
                f"Available labels: {sorted(pin_to_p)}"
            )
        fields.append(int(pin_to_p[label]["p_port_number"]))

    data[target_key] = fields


def validate_x_list_lengths(data: Dict[str, Any]) -> None:
    pump_keys = ["x_pump_ports", "x_pump_frequencies", "x_pump_currents", "x_pump_harmonics"]
    pump_lengths = [
        len(data.get(key) or [])
        for key in pump_keys
        if key in data
    ]
    pump_count = max(pump_lengths + [0])
    for key in pump_keys:
        if key in data and pump_count > 1 and len(data.get(key) or []) == 1:
            data[key] = list(data.get(key) or []) * pump_count

    pairs = [
        ("x_dc_ports", "x_dc_currents"),
        ("x_pump_ports", "x_pump_frequencies"),
        ("x_pump_ports", "x_pump_currents"),
        ("x_pump_ports", "x_pump_harmonics"),
    ]

    for a, b in pairs:
        if a in data and b in data:
            if len(data.get(a) or []) != len(data.get(b) or []):
                raise ValueError(
                    f"X setting length mismatch: len({a})={len(data.get(a) or [])}, "
                    f"len({b})={len(data.get(b) or [])}"
                )

    for key in ["x_input_ports", "x_output_ports", "x_pump_ports", "x_dc_ports"]:
        values = data.get(key, []) or []
        if not isinstance(values, list):
            raise ValueError(f"{key} must be a list of pin-label strings")
        if key in {"x_input_ports", "x_output_ports"} and len(values) > 1:
            raise ValueError(
                f"{key} currently supports exactly one port label. "
                "Multiple X input/output ports are not yet implemented."
            )


def collapse_and_save_x_hbsolve(
    file_path: Path,
    builtin_dir: Path,
    script_dir: Path,
    memo: Dict[str, Any],
    output_dir: Path,
    resolved_dir: Optional[Path] = None,
) -> None:
    original_data = load_json(file_path)

    if not bool_true(original_data.get("x-params")):
        raise ValueError(
            f"Cannot X-merge {file_path.name}: expected top JSON to have x-params=true"
        )

    validate_x_list_lengths(original_data)
    x_settings = {key: original_data[key] for key in X_SETTING_KEYS if key in original_data}
    z0_value = numeric_float(original_data.get("z0"), "top-level z0")

    print(f"    [Action] X-MERGE: Flattening '{file_path.name}' into one X/HB circuit...")

    flat_insts, flat_wires, _flat_pins, flat_labels, aliases = flatten_circuit_data(
        original_data,
        file_path.parent,
        builtin_dir,
        script_dir,
        memo,
        resolved_dir=resolved_dir,
    )

    resolve_alias = build_alias_resolver(aliases)
    top_pins = normalize_top_pins(original_data, resolve_alias)

    merged = dict(original_data)
    merged["instances"] = flat_insts
    merged["wires"] = flat_wires
    merged["pins"] = top_pins
    merged["labels"] = flat_labels
    merged.update(x_settings)

    merged = remove_ground_instances(merged)

    internal_joins = internal_join_pairs_from_top_wires(
        original_data,
        merged,
        aliases,
        resolve_alias,
    )

    merged = remove_internal_p_blocks_and_z0_resistors(
        merged,
        internal_joins,
        z0_value,
    )
    merged = remove_replaced_boundary_p_blocks(merged, z0_value)
    merged = remove_added_xr_when_existing_parallel_resistor(merged)

    internal_p_uids = set(merged.get("x_removed_internal_p_blocks", []))

    pin_to_p = derive_pin_to_p_block_map(merged)
    merged["x_exposed_pin_to_p_block"] = pin_to_p

    boundary_pins = derive_boundary_pins_from_outgoing_pins(merged, pin_to_p)
    validate_requested_x_labels_against_boundaries(merged, boundary_pins)
    merged["x_boundary_pins"] = boundary_pins
    merged["x_boundary_pin_count"] = len(boundary_pins)

    labels_to_fields(merged, pin_to_p, "x_pump_ports", "x_pump_fields", required=True)
    labels_to_fields(merged, pin_to_p, "x_dc_ports", "x_dc_fields", required=False)
    labels_to_fields(merged, pin_to_p, "x_input_ports", "x_input_fields", required=True)
    labels_to_fields(merged, pin_to_p, "x_output_ports", "x_out_fields", required=True)

    if merged.get("x_input_fields"):
        merged["hb_input_field"] = int(merged["x_input_fields"][0])
        merged["hb_input_pin_name"] = merged.get("x_input_ports", [None])[0]

    if merged.get("x_out_fields"):
        merged["hb_output_field"] = int(merged["x_out_fields"][0])
        merged["hb_output_pin_name"] = merged.get("x_output_ports", [None])[0]

    out_file = output_dir / file_path.name
    save_json(out_file, merged)

    print(
        f"      -> Saved X-merged circuit to: {out_file} "
        f"(Instances: {len(merged.get('instances', []))}, Wires: {len(merged.get('wires', []))})"
    )
    print(f"      -> Removed internal P blocks: {sorted(internal_p_uids)}")
    print(f"      -> Boundary pins: {merged.get('x_boundary_pins')}")
    print(f"      -> X pump fields: {merged.get('x_pump_fields')}")
    print(f"      -> X output fields: {merged.get('x_out_fields')}")


def save_unchanged_block(file_path: Path, output_dir: Path) -> None:
    out_file = output_dir / file_path.name
    if file_path.resolve() != out_file.resolve():
        shutil.copy2(file_path, out_file)
    print(f"    [Action] UNCHANGED: Copied '{file_path.name}' to {out_file}")


def execute_x_merge(
    file_path: Path,
    builtin_dir: Path,
    script_dir: Path,
    memo: Dict[str, Any],
    output_dir: Path,
    resolved_dir: Optional[Path] = None,
) -> None:
    node_class = get_memo_class(file_path, memo)

    if node_class not in {"hbsolve_block", "sparam_block", "mixture"}:
        raise ValueError(
            f"X merge expects a top-level circuit/block, got class {node_class!r} for {file_path.name}"
        )

    collapse_and_save_x_hbsolve(
        file_path,
        builtin_dir,
        script_dir,
        memo,
        output_dir,
        resolved_dir,
    )


def run_x_merger(target_files: Sequence[str]) -> None:
    print("==================================================")
    print(" Running X-Parameter Merge Engine                 ")
    print("==================================================\n")

    script_dir = Path(__file__).parent.resolve() if "__file__" in globals() else Path.cwd().resolve()
    builtin_dir = script_dir / "built-in"

    for target in target_files:
        target_path = Path(target)
        project_name = target_path.parent.name if target_path.parent.name else "default_project"

        project_output_dir = get_project_output_dir(script_dir, project_name)
        resolved_dir = get_resolved_dir(script_dir, project_name)
        merge_output_dir = script_dir / "outputs" / project_name / "merged"
        merge_output_dir.mkdir(parents=True, exist_ok=True)

        memo_path = project_output_dir / "classification_memo.json"
        if not memo_path.exists():
            raise FileNotFoundError(
                f"Memo for {project_name} not found at {memo_path}. Run classifier first."
            )

        memo = load_json(memo_path)

        original_file_path = resolve_source_target(script_dir, target)
        if not original_file_path.exists():
            raise FileNotFoundError(f"Could not find target file {original_file_path}")

        resolved_path = resolved_dir / original_file_path.name
        file_path = resolved_path.resolve() if resolved_path.exists() else original_file_path

        print(f"Project: {project_name} | Target: {target_path.name}")
        print(f"    Input source: {file_path}")
        print(f"    X merge output: {merge_output_dir}")

        execute_x_merge(
            file_path,
            builtin_dir,
            script_dir,
            memo,
            merge_output_dir,
            resolved_dir=resolved_dir,
        )

        # The merged circuit is a flat HB circuit; update the memo so downstream
        # stages (validator, netlist) treat it as hbsolve_block, not sparam_block.
        # Update every memo entry whose filename matches the target so the
        # filename-based fallback in get_memo_class also returns the correct class.
        target_name = original_file_path.name
        hb_entry = {"class": "hbsolve_block", "hb_elements": [], "sp_elements": []}
        updated = False
        for k in list(memo.keys()):
            if Path(k).name == target_name:
                memo[k] = hb_entry
                updated = True
        if not updated:
            memo[str((merge_output_dir / target_name).resolve())] = hb_entry
        with open(memo_path, "w") as _f:
            json.dump(memo, _f, indent=2)

        print("-" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run X-parameter merge for one JSON file.")
    parser.add_argument("file", help="Path to the top-level JSON file")
    args = parser.parse_args()

    try:
        run_x_merger([args.file])
    except Exception as exc:
        print(f"[X-MERGE ERROR] {exc}", file=sys.stderr)
        raise
