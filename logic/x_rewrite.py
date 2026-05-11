#!/usr/bin/env python3
"""
x_rewrite.py

Rewrite supported built-in S-parameter primitives into HB-compatible schematic
equivalents for mixed X-parameter simulations.

This rewrite stage:
1. Replaces built-in S blocks that provide x-equivalent-circuit with equivalent
   R/L/C HB schematics.
2. Does NOT add P blocks inside rewritten S-block equivalents.
3. Normalizes the final top-level schematic for X-parameter simulation by:
   - removing existing HB boundary P blocks and their 50 Ohm parallel resistors
   - adding fresh P blocks at top-level simulation input/output pins using z0
4. Leaves all topology endpoints as named ports. The dedicated
   port_resolution stage converts them to integers later.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

from path_utils import resolve_source_target
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from merger import (  # type: ignore
    get_project_output_dir,
    get_resolved_dir,
    get_stage_input_path,
    resolve_component,
)


HB_PRIMITIVES = {
    "R": ("R", "built-in/hbsolve/R.json"),
    "L": ("L", "built-in/hbsolve/L.json"),
    "C": ("C", "built-in/hbsolve/C.json"),
}

P_TYPE_NAMES = {"P", "built-in/hbsolve/P", "built-in/hbsolve/P.json"}
R_TYPE_NAMES = {"R", "built-in/hbsolve/R", "built-in/hbsolve/R.json"}


def load_json(path: Path) -> Dict[str, Any]:
    with open(path, "r") as f:
        return json.load(f)


def save_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def is_builtin_sparam(data: Dict[str, Any]) -> bool:
    return data.get("type") == "built-in" and "ssolve" in str(data.get("subtype", "")).lower()


def is_schematic(data: Dict[str, Any]) -> bool:
    return data.get("type") == "schematic"


def is_p_instance(inst: Dict[str, Any]) -> bool:
    return str(inst.get("type_name", "")) in P_TYPE_NAMES


def is_r_instance(inst: Dict[str, Any]) -> bool:
    return str(inst.get("type_name", "")) in R_TYPE_NAMES


def merge_parameters(inst: Dict[str, Any]) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for key, value in (inst.get("parameters", {}) or {}).items():
        if value not in (None, ""):
            values[key] = str(value)
    return values


def strip_outer_parens(expr: str) -> str:
    expr = expr.strip()
    changed = True
    while changed and expr.startswith("(") and expr.endswith(")"):
        changed = False
        depth = 0
        for idx, ch in enumerate(expr):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0 and idx != len(expr) - 1:
                    return expr
        if depth == 0:
            expr = expr[1:-1].strip()
            changed = True
    return expr


def split_top_level(expr: str, sep: str) -> List[str]:
    parts: List[str] = []
    depth = 0
    start = 0
    for idx, ch in enumerate(expr):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                raise ValueError(f"Unbalanced expression {expr!r}")
        elif ch == sep and depth == 0:
            parts.append(expr[start:idx])
            start = idx + 1
    parts.append(expr[start:])
    return [strip_outer_parens(part) for part in parts if part.strip()]


def is_real_expression(expr: str) -> bool:
    compact = expr.replace(" ", "")
    return "im" not in compact and "w" not in compact


def substitute_variables(expr: str, values: Dict[str, str]) -> str:
    result = str(expr)
    for name in sorted(values, key=len, reverse=True):
        value = values[name]
        result = re.sub(rf"\b{re.escape(name)}\b", f"({value})", result)
    return strip_outer_parens(result)


def parse_im_w_product(expr: str) -> Optional[str]:
    compact = strip_outer_parens(expr.replace(" ", ""))
    factors = split_top_level(compact, "*")
    normalized = [strip_outer_parens(factor) for factor in factors]

    if "im" not in normalized or "w" not in normalized:
        return None

    remaining = []
    used_im = False
    used_w = False
    for factor in normalized:
        if factor == "im" and not used_im:
            used_im = True
        elif factor == "w" and not used_w:
            used_w = True
        else:
            remaining.append(factor)

    if not used_im or not used_w or len(remaining) != 1:
        return None
    return strip_outer_parens(remaining[0])


def parse_inverse_im_w_product(expr: str) -> Optional[str]:
    compact = strip_outer_parens(expr.replace(" ", ""))
    if compact.startswith("1/"):
        denom = strip_outer_parens(compact[2:])
        return parse_im_w_product(denom)
    if compact.startswith("-im/"):
        denom = strip_outer_parens(compact[4:])
        factors = split_top_level(denom, "*")
        normalized = [strip_outer_parens(factor) for factor in factors]
        if "w" not in normalized:
            return None
        remaining = []
        used_w = False
        for factor in normalized:
            if factor == "w" and not used_w:
                used_w = True
            else:
                remaining.append(factor)
        if used_w and len(remaining) == 1:
            return strip_outer_parens(remaining[0])
    return None


def lower_impedance(expr: str, context: str) -> Tuple[str, str]:
    expr = strip_outer_parens(str(expr))
    if is_real_expression(expr):
        return "R", expr

    inductance = parse_im_w_product(expr)
    if inductance is not None:
        return "L", inductance

    capacitance = parse_inverse_im_w_product(expr)
    if capacitance is not None:
        return "C", capacitance

    raise ValueError(
        f"{context}: impedance expression {expr!r} is not supported. "
        "Use a real R, im*w*L, or 1/(im*w*C) form."
    )


def lower_admittance(expr: str, context: str) -> Tuple[str, str]:
    expr = strip_outer_parens(str(expr))
    if is_real_expression(expr):
        return "R", f"1/({expr})"

    capacitance = parse_im_w_product(expr)
    if capacitance is not None:
        return "C", capacitance

    inductance = parse_inverse_im_w_product(expr)
    if inductance is not None:
        return "L", inductance

    raise ValueError(
        f"{context}: admittance expression {expr!r} is not supported. "
        "Use a real G, im*w*C, or 1/(im*w*L) form."
    )


def primitive_instance(uid: str, primitive: str, value: str) -> Dict[str, Any]:
    param_name = HB_PRIMITIVES[primitive][0]
    return {
        "type_name": primitive,
        "uid": uid,
        "parameters": {
            param_name: value,
        },
        "parameter_order": [
            param_name,
        ],
        "parameter_kinds": {
            param_name: "positional",
        },
        "position": [
            0.0,
            0.0,
        ],
        "port_count": 2,
        "port_names": [
            "p1",
            "p2",
        ],
        "rotation_degrees": 0.0,
        "repeat_count": 1,
        "symbol_port_layout": [],
    }


def p_instance(uid: str, port_number: int, position: Optional[List[float]] = None) -> Dict[str, Any]:
    return {
        "type_name": "P",
        "uid": uid,
        "parameters": {
            "port": str(port_number),
        },
        "parameter_order": [
            "port",
        ],
        "parameter_kinds": {
            "port": "positional",
        },
        "position": position or [0.0, 0.0],
        "port_count": 2,
        "port_names": [
            "p1",
            "p2",
        ],
        "rotation_degrees": 0.0,
        "repeat_count": 1,
        "symbol_port_layout": [],
    }


def r_instance(uid: str, resistance: str, position: Optional[List[float]] = None) -> Dict[str, Any]:
    return {
        "type_name": "R",
        "uid": uid,
        "parameters": {
            "R": str(resistance),
        },
        "parameter_order": [
            "R",
        ],
        "parameter_kinds": {
            "R": "positional",
        },
        "position": position or [0.0, 0.0],
        "port_count": 2,
        "port_names": [
            "p1",
            "p2",
        ],
        "rotation_degrees": 0.0,
        "repeat_count": 1,
        "symbol_port_layout": [],
    }


def canonical_node(node: str, aliases: Dict[str, str]) -> str:
    seen = set()
    current = str(node)
    while current in aliases:
        if current in seen:
            raise ValueError(f"x-equivalent-circuit node alias cycle at {current!r}")
        seen.add(current)
        current = str(aliases[current])
    return current


def add_endpoint(
    node_endpoints: Dict[str, Tuple[str, str]],
    wires: List[Dict[str, Any]],
    labels: List[Dict[str, Any]],
    node: str,
    uid: str,
    port: str,
) -> None:
    if node == "0":
        labels.append({"name": "0", "instance_uid": uid, "port": port})
        return

    if node not in node_endpoints:
        node_endpoints[node] = (uid, port)
        return

    anchor_uid, anchor_port = node_endpoints[node]
    wires.append(
        {
            "source_instance_uid": anchor_uid,
            "source_port": anchor_port,
            "target_instance_uid": uid,
            "target_port": port,
            "name": "",
        }
    )


def build_equivalent_schematic(
    builtin_path: Path,
    builtin_data: Dict[str, Any],
    inst: Dict[str, Any],
    out_name: str,
) -> Dict[str, Any]:
    eq = builtin_data.get("x-equivalent-circuit")
    if not isinstance(eq, dict) or not eq:
        raise ValueError(
            f"Instance {inst.get('uid')} ({inst.get('type_name')}): built-in S block "
            f"{builtin_path.name} has no x-equivalent-circuit."
        )
    if eq.get("kind") != "lumped_passive_network":
        raise ValueError(
            f"{builtin_path.name}: unsupported x-equivalent-circuit kind {eq.get('kind')!r}"
        )

    values = merge_parameters(inst)
    aliases = {str(k): str(v) for k, v in (eq.get("node_aliases", {}) or {}).items()}

    instances: List[Dict[str, Any]] = []
    wires: List[Dict[str, Any]] = []
    labels: List[Dict[str, Any]] = []
    node_endpoints: Dict[str, Tuple[str, str]] = {}

    for idx, branch in enumerate(eq.get("branches", []) or [], start=1):
        branch_name = str(branch.get("name") or f"B{idx}")
        nodes = branch.get("between")
        if not isinstance(nodes, list) or len(nodes) != 2:
            raise ValueError(f"{builtin_path.name} branch {branch_name}: expected two nodes")

        node_a = canonical_node(str(nodes[0]), aliases)
        node_b = canonical_node(str(nodes[1]), aliases)

        if "impedance" in branch:
            expr = substitute_variables(str(branch["impedance"]), values)
            primitive, value = lower_impedance(expr, f"{builtin_path.name} branch {branch_name}")
        elif "admittance" in branch:
            expr = substitute_variables(str(branch["admittance"]), values)
            primitive, value = lower_admittance(expr, f"{builtin_path.name} branch {branch_name}")
        else:
            raise ValueError(
                f"{builtin_path.name} branch {branch_name}: expected impedance or admittance"
            )

        uid = f"{branch_name}_{primitive}{idx}"
        instances.append(primitive_instance(uid, primitive, value))
        add_endpoint(node_endpoints, wires, labels, node_a, uid, "p1")
        add_endpoint(node_endpoints, wires, labels, node_b, uid, "p2")

    pins = []
    for pin in eq.get("pins", []) or []:
        name = str(pin["name"])
        node = canonical_node(str(pin["node"]), aliases)
        if node == "0":
            raise ValueError(f"{builtin_path.name}: exported pin {name!r} resolves to ground")
        if node not in node_endpoints:
            raise ValueError(
                f"{builtin_path.name}: exported pin {name!r} uses node {node!r}, "
                "but no branch touches that node"
            )
        uid, port = node_endpoints[node]
        pins.append({"name": name, "instance_uid": uid, "port": port})

    return {
        "name": out_name,
        "type": "schematic",
        "hb_top_block": False,
        "port_count": int(builtin_data.get("port_count", len(pins))),
        "port_names": list(builtin_data.get("port_names", [pin["name"] for pin in pins])),
        "instances": instances,
        "wires": wires,
        "labels": labels,
        "pins": pins,
        "x_rewrite_source": {
            "builtin": builtin_path.name,
            "instance_uid": inst.get("uid"),
        },
    }


def memo_entry(class_name: str, hb: Iterable[str] = (), sp: Iterable[str] = ()) -> Dict[str, Any]:
    return {
        "class": class_name,
        "hb_elements": sorted(set(hb)),
        "sp_elements": sorted(set(sp)),
    }


def instance_param(inst: Dict[str, Any], names: Sequence[str]) -> Optional[str]:
    source = inst.get("parameters", {}) or {}
    for name in names:
        if name in source and source[name] not in (None, ""):
            return str(source[name])
    return None


def normalized_numeric_expr(expr: str) -> Optional[float]:
    compact = strip_outer_parens(str(expr)).replace(" ", "")
    safe = compact.replace("^", "**")
    if not re.fullmatch(r"[0-9eE+\-*/().]+", safe):
        return None
    try:
        value = eval(safe, {"__builtins__": {}}, {})
    except Exception:
        return None
    try:
        return float(value)
    except Exception:
        return None


def assert_50_ohm_resistor(inst: Dict[str, Any], context: str) -> None:
    value = instance_param(inst, ["R", "R1", "R_shunt"])
    if value is None:
        raise ValueError(f"{context}: resistor has no R/R1/R_shunt parameter")

    numeric = normalized_numeric_expr(value)
    if numeric is None or not math.isclose(numeric, 50.0, rel_tol=1e-9, abs_tol=1e-12):
        raise ValueError(
            f"{context}: expected removable port-parallel resistance to be 50 Ohm, "
            f"got {value!r}"
        )


def endpoint_key(uid: str, port: Any) -> Tuple[str, str]:
    return str(uid), str(port)


class UnionFind:
    def __init__(self) -> None:
        self.parent: Dict[Tuple[str, str], Tuple[str, str]] = {}

    def find(self, item: Tuple[str, str]) -> Tuple[str, str]:
        if item not in self.parent:
            self.parent[item] = item
            return item
        if self.parent[item] != item:
            self.parent[item] = self.find(self.parent[item])
        return self.parent[item]

    def union(self, a: Tuple[str, str], b: Tuple[str, str]) -> None:
        ra = self.find(a)
        rb = self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def build_net_index(data: Dict[str, Any]) -> Dict[Tuple[str, str], Set[Tuple[str, str]]]:
    uf = UnionFind()

    for inst in data.get("instances", []) or []:
        uid = str(inst.get("uid"))
        port_names = inst.get("port_names", []) or []
        port_count = int(inst.get("port_count", len(port_names) or 0) or 0)

        if port_names:
            for idx, _port_name in enumerate(port_names, start=1):
                uf.find(endpoint_key(uid, idx))
        else:
            for idx in range(1, port_count + 1):
                uf.find(endpoint_key(uid, idx))

    for wire in data.get("wires", []) or []:
        a = endpoint_key(wire["source_instance_uid"], wire["source_port"])
        b = endpoint_key(wire["target_instance_uid"], wire["target_port"])
        uf.union(a, b)

    nets: Dict[Tuple[str, str], Set[Tuple[str, str]]] = {}
    for endpoint in list(uf.parent.keys()):
        root = uf.find(endpoint)
        nets.setdefault(root, set()).add(endpoint)
    return nets


def endpoint_to_net_map(data: Dict[str, Any]) -> Dict[Tuple[str, str], Set[Tuple[str, str]]]:
    result: Dict[Tuple[str, str], Set[Tuple[str, str]]] = {}
    for endpoints in build_net_index(data).values():
        for endpoint in endpoints:
            result[endpoint] = endpoints
    return result


def pin_endpoint(pin: Dict[str, Any]) -> Tuple[str, str]:
    return endpoint_key(pin["instance_uid"], pin["port"])


def remove_hb_boundary_ports_and_50r(data: Dict[str, Any]) -> None:
    """
    Remove existing P instances and any R=50 instances on the same net.

    This is intentionally permissive about count: every R on a P net must be 50,
    but the number of such resistors is not used as an invariant.
    """
    instances = data.get("instances", []) or []
    uid_to_inst = {str(inst.get("uid")): inst for inst in instances}

    p_uids = {uid for uid, inst in uid_to_inst.items() if is_p_instance(inst)}
    if not p_uids:
        return

    ep_to_net = endpoint_to_net_map(data)

    removable_r_uids: Set[str] = set()
    for p_uid in p_uids:
        p_inst = uid_to_inst[p_uid]
        p_ports = p_inst.get("port_names", []) or ["p1"]

        # At this stage existing topology has already been named-port normalized,
        # so existing P endpoints are normally numeric integers. Check numeric first,
        # then fall back to names for safety.
        for p_index, p_port_name in enumerate(p_ports, start=1):
            candidate_keys = [endpoint_key(p_uid, p_index), endpoint_key(p_uid, p_port_name)]
            net: Optional[Set[Tuple[str, str]]] = None
            for key in candidate_keys:
                if key in ep_to_net:
                    net = ep_to_net[key]
                    break
            if net is None:
                net = {endpoint_key(p_uid, p_index)}

            for uid, _port in net:
                if uid == p_uid:
                    continue
                inst = uid_to_inst.get(uid)
                if inst and is_r_instance(inst):
                    assert_50_ohm_resistor(inst, f"Port net around {p_uid}")
                    removable_r_uids.add(uid)

    remove_uids = p_uids | removable_r_uids

    data["instances"] = [
        inst for inst in instances if str(inst.get("uid")) not in remove_uids
    ]
    data["wires"] = [
        wire
        for wire in data.get("wires", []) or []
        if str(wire.get("source_instance_uid")) not in remove_uids
        and str(wire.get("target_instance_uid")) not in remove_uids
    ]
    data["pins"] = [
        pin
        for pin in data.get("pins", []) or []
        if str(pin.get("instance_uid")) not in remove_uids
    ]
    data["labels"] = [
        label
        for label in data.get("labels", []) or []
        if str(label.get("instance_uid")) not in remove_uids
    ]


def unique_preserve_order(items: Iterable[str]) -> List[str]:
    seen: Set[str] = set()
    result: List[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def require_at_most_one_label(data: Dict[str, Any], key: str) -> List[str]:
    values = data.get(key, []) or []
    if not isinstance(values, list):
        raise ValueError(f"{key} must be a list of pin-label strings")
    if len(values) > 1:
        raise ValueError(
            f"{key} currently supports exactly one port label. "
            "Multiple X input/output, pump, and DC ports are not yet implemented."
        )
    for value in values:
        if not isinstance(value, str):
            raise ValueError(f"{key} must contain pin-label strings, got {value!r}")
    return list(values)


def require_label_list(data: Dict[str, Any], key: str) -> List[str]:
    values = data.get(key, []) or []
    if not isinstance(values, list):
        raise ValueError(f"{key} must be a list of pin-label strings")
    for value in values:
        if not isinstance(value, str):
            raise ValueError(f"{key} must contain pin-label strings, got {value!r}")
    return list(values)


def find_top_pin(data: Dict[str, Any], name: str) -> Dict[str, Any]:
    matches = [pin for pin in data.get("pins", []) or [] if str(pin.get("name")) == name]
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one top-level pin named {name!r}; found {len(matches)}")
    return matches[0]


def warn_if_existing_parallel_resistor(
    data: Dict[str, Any],
    target_uid: str,
    target_port: str,
    pin_name: str,
) -> None:
    uf = UnionFind()
    uid_to_inst = {
        str(inst.get("uid")): inst
        for inst in data.get("instances", []) or []
        if inst.get("uid")
    }

    for inst in data.get("instances", []) or []:
        uid = str(inst.get("uid"))
        port_names = inst.get("port_names", []) or []
        if not port_names:
            port_names = [f"p{port}" for port in range(1, int(inst.get("port_count", 0) or 0) + 1)]
        for port in port_names:
            uf.find(endpoint_key(uid, port))

    uf.find("0")

    for wire in data.get("wires", []) or []:
        uf.union(
            endpoint_key(wire["source_instance_uid"], wire["source_port"]),
            endpoint_key(wire["target_instance_uid"], wire["target_port"]),
        )

    for label in data.get("labels", []) or []:
        if str(label.get("name", "")) in {"0", "P_0", "GND", "gnd", "ground"}:
            uf.union(endpoint_key(label["instance_uid"], label["port"]), "0")

    target_root = uf.find(endpoint_key(target_uid, target_port))
    ground_root = uf.find("0")

    for inst in data.get("instances", []) or []:
        uid = str(inst.get("uid"))
        if not is_r_instance(inst):
            continue

        port_names = inst.get("port_names", []) or ["p1", "p2"]
        if len(port_names) < 2:
            continue

        roots = [
            uf.find(endpoint_key(uid, port_names[0])),
            uf.find(endpoint_key(uid, port_names[1])),
        ]
        if target_root in roots and ground_root in roots:
            value = instance_param(inst, ["R", "R1", "R_shunt"])
            print(
                f"  -> [WARNING] X port {pin_name!r} already has resistor "
                f"{uid} in parallel to ground (R={value!r}); adding z0 shunt anyway."
            )


def add_final_boundary_ports(data: Dict[str, Any]) -> None:
    x_input_ports = require_at_most_one_label(data, "x_input_ports")
    x_output_ports = require_at_most_one_label(
        data,
        "x_output_ports",
    )
    require_label_list(data, "x_pump_ports")
    require_label_list(data, "x_dc_ports")

    if not x_input_ports:
        raise ValueError("x_input_ports must contain exactly one input port label.")
    if not x_output_ports:
        raise ValueError("x_output_ports must contain exactly one output port label.")

    z0 = str(data.get("z0", 50.0))
    boundary_names = unique_preserve_order(x_input_ports + x_output_ports)

    existing_uids = {str(inst.get("uid")) for inst in data.get("instances", []) or []}

    field_by_label = {}
    field_by_label[x_input_ports[0]] = 1
    field_by_label[x_output_ports[0]] = 2

    for pin_name in boundary_names:
        pin = find_top_pin(data, str(pin_name))
        target_uid, target_port = pin_endpoint(pin)
        warn_if_existing_parallel_resistor(data, target_uid, target_port, str(pin_name))

        uid_base = f"XP_{pin_name}"
        uid = uid_base
        suffix = 1
        while uid in existing_uids:
            suffix += 1
            uid = f"{uid_base}_{suffix}"
        existing_uids.add(uid)
        r_uid_base = f"XR_{pin_name}"
        r_uid = r_uid_base
        suffix = 1
        while r_uid in existing_uids:
            suffix += 1
            r_uid = f"{r_uid_base}_{suffix}"
        existing_uids.add(r_uid)

        data.setdefault("instances", []).append(
            p_instance(
                uid,
                field_by_label[str(pin_name)],
                position=[-700.0, -100.0 + 80.0 * field_by_label[str(pin_name)]],
            )
        )
        data.setdefault("instances", []).append(
            r_instance(
                r_uid,
                z0,
                position=[-620.0, -100.0 + 80.0 * field_by_label[str(pin_name)]],
            )
        )
        data.setdefault("wires", []).append(
            {
                "source_instance_uid": uid,
                "source_port": "p1",
                "target_instance_uid": target_uid,
                "target_port": target_port,
                "name": "",
            }
        )
        data.setdefault("wires", []).append(
            {
                "source_instance_uid": r_uid,
                "source_port": "p1",
                "target_instance_uid": target_uid,
                "target_port": target_port,
                "name": "",
            }
        )
        data.setdefault("labels", []).append(
            {
                "name": "0",
                "instance_uid": uid,
                "port": "p2",
            }
        )
        data.setdefault("labels", []).append(
            {
                "name": "0",
                "instance_uid": r_uid,
                "port": "p2",
            }
        )

        pin["instance_uid"] = uid
        pin["port"] = "p1"


def convert_new_x_ports_to_numeric(data: Dict[str, Any]) -> None:
    """
    x_rewrite runs after named-port normalization, so newly added topology
    endpoints must use numeric ports, not names like 'p1'.

    This pass is deliberately tolerant: existing integer ports are left alone,
    and only string ports are resolved through the instance's port_names list.
    """
    uid_to_inst = {
        str(inst.get("uid")): inst
        for inst in data.get("instances", []) or []
        if inst.get("uid")
    }

    def port_number(uid: str, port: Any, context: str) -> int:
        if isinstance(port, int):
            return port

        if not isinstance(port, str):
            raise ValueError(
                f"{context}: expected int or named string port, got {port!r} "
                f"({type(port).__name__})"
            )

        inst = uid_to_inst.get(str(uid))
        if inst is None:
            raise ValueError(f"{context}: instance {uid!r} not found")

        port_names = inst.get("port_names", []) or []
        if port not in port_names:
            raise ValueError(
                f"{context}: port {port!r} not found on instance {uid!r}; "
                f"available ports: {port_names}"
            )

        return port_names.index(port) + 1

    name = str(data.get("name", "<schematic>"))

    for wire in data.get("wires", []) or []:
        src_uid = str(wire["source_instance_uid"])
        tgt_uid = str(wire["target_instance_uid"])

        wire["source_port"] = port_number(
            src_uid,
            wire["source_port"],
            f"{name} wire source {src_uid!r}",
        )
        wire["target_port"] = port_number(
            tgt_uid,
            wire["target_port"],
            f"{name} wire target {tgt_uid!r}",
        )

    for pin in data.get("pins", []) or []:
        uid = str(pin["instance_uid"])
        pin["port"] = port_number(
            uid,
            pin["port"],
            f"{name} pin {pin.get('name')!r}",
        )

    for label in data.get("labels", []) or []:
        uid = str(label["instance_uid"])
        label["port"] = port_number(
            uid,
            label["port"],
            f"{name} label {label.get('name')!r}",
        )


def normalize_top_level_x_ports(data: Dict[str, Any]) -> None:
    remove_hb_boundary_ports_and_50r(data)
    add_final_boundary_ports(data)


class XRewriter:
    def __init__(self, script_dir: Path, project_name: str) -> None:
        self.script_dir = script_dir
        self.project_name = project_name
        self.builtin_dir = script_dir / "built-in"
        self.project_output_dir = get_project_output_dir(script_dir, project_name)
        self.resolved_dir = get_resolved_dir(script_dir, project_name)
        self.memo_path = self.project_output_dir / "classification_memo.json"
        if not self.memo_path.exists():
            raise FileNotFoundError(f"Missing classification memo: {self.memo_path}")
        self.memo = load_json(self.memo_path)
        self.processed: set[Path] = set()
        self.generated: List[Path] = []

    def add_hb_primitive_memos(self) -> None:
        for primitive, (_, rel_path) in HB_PRIMITIVES.items():
            path = (self.script_dir / rel_path).resolve()
            self.memo[str(path)] = memo_entry("hbsolve_primitive", hb=[primitive])

        p_path = (self.script_dir / "built-in/hbsolve/P.json").resolve()
        self.memo[str(p_path)] = memo_entry("hbsolve_primitive", hb=["P"])

    def rewrite_target(self, target: str) -> None:
        original_path = resolve_source_target(self.script_dir, target)
        if not original_path.exists():
            raise FileNotFoundError(f"Could not find target file {original_path}")

        input_path = get_stage_input_path(original_path, self.resolved_dir)
        self.add_hb_primitive_memos()
        out_path = self.rewrite_file(input_path.resolve(), original_path.resolve())

        if out_path.name != original_path.name:
            raise ValueError(
                f"Internal error: target rewrite wrote {out_path.name}, "
                f"expected {original_path.name}"
            )

        data = load_json(out_path)
        normalize_top_level_x_ports(data)
        save_json(out_path, data)

        self.memo[str(original_path.resolve())] = memo_entry(
            "hbsolve_block",
            hb=[data.get("name", out_path.stem)],
        )
        self.memo[str(out_path.resolve())] = memo_entry(
            "hbsolve_block",
            hb=[data.get("name", out_path.stem)],
        )

        save_json(self.memo_path, self.memo)
        print(f"  -> Updated classification memo: {self.memo_path}")

    def rewrite_file(self, file_path: Path, original_key_path: Optional[Path] = None) -> Path:
        file_path = file_path.resolve()
        if file_path in self.processed:
            return self.resolved_dir / file_path.name
        self.processed.add(file_path)

        data = load_json(file_path)
        if not is_schematic(data):
            return file_path

        changed = False
        new_instances = []

        for inst in data.get("instances", []) or []:
            type_name = inst.get("type_name")
            if not type_name:
                new_instances.append(inst)
                continue

            inst_file = resolve_component(
                str(type_name),
                file_path.parent,
                self.builtin_dir,
                self.script_dir,
                self.resolved_dir,
            )
            if not inst_file:
                new_instances.append(inst)
                continue

            child_data = load_json(inst_file)

            if is_builtin_sparam(child_data):
                generated_name = f"{file_path.stem}__x_{inst['uid']}_{Path(str(type_name)).stem}"
                generated_path = self.resolved_dir / f"{generated_name}.json"
                equivalent = build_equivalent_schematic(
                    inst_file.resolve(),
                    child_data,
                    inst,
                    generated_name,
                )
                save_json(generated_path, equivalent)
                self.memo[str(generated_path.resolve())] = memo_entry(
                    "hbsolve_block",
                    hb=[generated_name],
                )
                self.generated.append(generated_path)

                rewritten = dict(inst)
                rewritten["type_name"] = generated_name
                rewritten["parameters"] = {}
                rewritten["parameter_order"] = []
                rewritten["parameter_kinds"] = {}
                new_instances.append(rewritten)
                changed = True
                continue

            if is_schematic(child_data):
                self.rewrite_file(inst_file.resolve())

            new_instances.append(inst)

        out_path = self.resolved_dir / file_path.name
        if changed or out_path.exists() or file_path.parent != self.resolved_dir:
            data["instances"] = new_instances
            save_json(out_path, data)

            memo_key = original_key_path.resolve() if original_key_path else file_path.resolve()
            self.memo[str(memo_key)] = memo_entry(
                "hbsolve_block",
                hb=[data.get("name", out_path.stem)],
            )
            self.memo[str(out_path.resolve())] = memo_entry(
                "hbsolve_block",
                hb=[data.get("name", out_path.stem)],
            )
            return out_path.resolve()

        return file_path


def run_rewrite(targets: Sequence[str]) -> None:
    print("==================================================")
    print(" Running X-Parameter S-to-HB Rewrite              ")
    print("==================================================\n")

    script_dir = Path(__file__).parent.resolve() if "__file__" in globals() else Path.cwd().resolve()
    for target in targets:
        target_path = Path(target)
        project_name = target_path.parent.name if target_path.parent.name else "default_project"
        print(f"Project: {project_name} | Target: {target_path.name}")
        rewriter = XRewriter(script_dir, project_name)
        rewriter.rewrite_target(target)
        if rewriter.generated:
            for path in rewriter.generated:
                print(f"  -> Generated HB equivalent: {path}")
        else:
            print("  -> No built-in S primitives required rewriting.")
        print("-" * 60)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rewrite supported built-in S blocks into HB schematic equivalents."
    )
    parser.add_argument("file", nargs="+", help="Target JSON file(s)")
    args = parser.parse_args()

    try:
        run_rewrite(args.file)
    except Exception as exc:
        print(f"[X-REWRITE ERROR] {exc}", file=sys.stderr)
        raise


if __name__ == "__main__":
    main()
