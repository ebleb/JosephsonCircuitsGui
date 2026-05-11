#!/usr/bin/env python3
"""
Strict topology port resolution stage.

Before this stage, every wire/pin/label endpoint must use a named string port.
After this stage, every endpoint uses a 1-based integer port. No earlier stage
is allowed to leak integer ports, and no later stage is allowed to see strings.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from path_utils import resolve_source_target


def load_json(path: Path) -> Dict[str, Any]:
    with open(path, "r") as f:
        return json.load(f)


def save_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def project_name_for_target(target: str) -> str:
    target_dir = Path(target).parent
    return target_dir.name if str(target_dir) != "." else "default_project"


def resolve_component(
    cell_name: str,
    current_dir: Path,
    builtin_dir: Path,
    script_dir: Path,
    stage_dir: Optional[Path] = None,
    resolved_dir: Optional[Path] = None,
) -> Optional[Path]:
    if "/" in cell_name or "\\" in cell_name:
        target_path = script_dir / cell_name
        if target_path.suffix != ".json":
            target_path = target_path.with_suffix(".json")
        target_name = target_path.name
    else:
        target_name = cell_name if cell_name.endswith(".json") else f"{cell_name}.json"
        target_path = current_dir / target_name

    for base_dir in (stage_dir, resolved_dir):
        if base_dir is not None:
            candidate = base_dir / Path(target_name).name
            if candidate.is_file():
                return candidate

    if target_path.is_file():
        return target_path

    local_path = current_dir / target_name
    if local_path.is_file():
        return local_path

    if builtin_dir.exists():
        for path in builtin_dir.rglob(Path(target_name).name):
            if path.is_file():
                return path

    return None


def require_named_port(value: Any, context: str) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        raise ValueError(
            f"{context}: port resolution input must be a named string; "
            f"got pre-resolved integer {value!r}."
        )
    if not isinstance(value, str):
        raise ValueError(
            f"{context}: port resolution input must be a named string; "
            f"got {value!r} ({type(value).__name__})."
        )
    if not value:
        raise ValueError(f"{context}: port name may not be empty.")
    return value


def require_resolved_port(value: Any, context: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(
            f"{context}: port resolution output must be an integer; "
            f"got {value!r} ({type(value).__name__})."
        )
    if value < 1:
        raise ValueError(f"{context}: resolved port must be >= 1; got {value!r}.")
    return value


def instance_lookup(data: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {
        inst["uid"]: inst
        for inst in data.get("instances", []) or []
        if inst.get("uid")
    }


def primitive_port_map(inst: Dict[str, Any], child_data: Dict[str, Any], context: str) -> Dict[str, int]:
    port_names = child_data.get("port_names") or inst.get("port_names") or []
    if not isinstance(port_names, list) or not all(isinstance(p, str) and p for p in port_names):
        raise ValueError(
            f"{context}: instance {inst.get('uid')!r} must define port_names "
            f"as a non-empty list of strings. Got {port_names!r}."
        )
    return {name: index for index, name in enumerate(port_names, start=1)}


def schematic_pin_port_map(inst: Dict[str, Any], child_data: Dict[str, Any], context: str) -> Dict[str, int]:
    child_type = str(child_data.get("type", "")).lower()
    if child_type == "built-in":
        return primitive_port_map(inst, child_data, context)

    if child_type != "schematic":
        raise ValueError(
            f"{context}: component {inst.get('type_name')!r} must resolve to a "
            f"built-in or schematic JSON before port resolution. Got type={child_data.get('type')!r}."
        )

    pins = child_data.get("pins", []) or []
    if not pins:
        raise ValueError(
            f"{context}: child schematic {inst.get('type_name')!r} has no exported "
            "pins[]. Schematic instance ports must be resolved by child pins[].name; "
            "port_names fallback is only valid for built-in primitives."
        )

    mapping: Dict[str, int] = {}
    for external_port, pin in enumerate(pins, start=1):
        name = pin.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError(f"{context}: child exported pin has invalid name: {pin}")
        if name in mapping:
            raise ValueError(f"{context}: duplicate exported pin name {name!r}")
        mapping[name] = external_port
    return mapping


def build_interface_maps(
    data: Dict[str, Any],
    current_dir: Path,
    builtin_dir: Path,
    script_dir: Path,
    stage_dir: Optional[Path],
    resolved_dir: Optional[Path],
    context_name: str,
) -> Dict[str, Dict[str, int]]:
    maps: Dict[str, Dict[str, int]] = {}
    for inst in data.get("instances", []) or []:
        uid = inst.get("uid")
        type_name = inst.get("type_name")
        if not uid or not type_name:
            continue

        child_path = resolve_component(
            str(type_name),
            current_dir,
            builtin_dir,
            script_dir,
            stage_dir=stage_dir,
            resolved_dir=resolved_dir,
        )
        if child_path is None:
            raise ValueError(
                f"{context_name} instance {uid!r}: component {type_name!r} could not be resolved."
            )
        child_data = load_json(child_path)
        maps[str(uid)] = schematic_pin_port_map(
            inst,
            child_data,
            f"{context_name} instance {uid!r}",
        )
    return maps


def resolve_endpoint(
    maps: Dict[str, Dict[str, int]],
    uid: Any,
    port_value: Any,
    context: str,
) -> int:
    name = require_named_port(port_value, context)
    port_map = maps.get(str(uid))
    if port_map is None:
        raise ValueError(f"{context}: missing interface map for instance {uid!r}")
    if name not in port_map:
        raise ValueError(
            f"{context}: port {name!r} not found on instance {uid!r}; "
            f"available ports: {list(port_map)}"
        )
    return port_map[name]


def resolve_ports_in_data(
    data: Dict[str, Any],
    current_dir: Path,
    builtin_dir: Path,
    script_dir: Path,
    stage_dir: Optional[Path],
    resolved_dir: Optional[Path],
    context_name: str,
) -> Dict[str, Any]:
    insts = instance_lookup(data)
    maps = build_interface_maps(
        data,
        current_dir,
        builtin_dir,
        script_dir,
        stage_dir,
        resolved_dir,
        context_name,
    )

    for wire in data.get("wires", []) or []:
        src_uid = wire.get("source_instance_uid")
        tgt_uid = wire.get("target_instance_uid")
        if src_uid not in insts:
            raise ValueError(f"{context_name}: wire source instance {src_uid!r} not found: {wire}")
        if tgt_uid not in insts:
            raise ValueError(f"{context_name}: wire target instance {tgt_uid!r} not found: {wire}")
        wire["source_port"] = resolve_endpoint(
            maps,
            src_uid,
            wire.get("source_port"),
            f"{context_name} wire source {src_uid!r}",
        )
        wire["target_port"] = resolve_endpoint(
            maps,
            tgt_uid,
            wire.get("target_port"),
            f"{context_name} wire target {tgt_uid!r}",
        )

    for pin in data.get("pins", []) or []:
        uid = pin.get("instance_uid")
        if uid not in insts:
            raise ValueError(f"{context_name}: pin instance {uid!r} not found: {pin}")
        pin["port"] = resolve_endpoint(
            maps,
            uid,
            pin.get("port"),
            f"{context_name} pin {pin.get('name')!r}",
        )

    for label in data.get("labels", []) or []:
        uid = label.get("instance_uid")
        if uid not in insts:
            raise ValueError(f"{context_name}: label instance {uid!r} not found: {label}")
        label["port"] = resolve_endpoint(
            maps,
            uid,
            label.get("port"),
            f"{context_name} label {label.get('name')!r}",
        )

    validate_resolved_ports(data, context_name)
    return data


def validate_resolved_ports(data: Dict[str, Any], context_name: str) -> None:
    for wire in data.get("wires", []) or []:
        require_resolved_port(
            wire.get("source_port"),
            f"{context_name} wire source {wire.get('source_instance_uid')!r}",
        )
        require_resolved_port(
            wire.get("target_port"),
            f"{context_name} wire target {wire.get('target_instance_uid')!r}",
        )
    for pin in data.get("pins", []) or []:
        require_resolved_port(
            pin.get("port"),
            f"{context_name} pin {pin.get('name')!r}",
        )
    for label in data.get("labels", []) or []:
        require_resolved_port(
            label.get("port"),
            f"{context_name} label {label.get('name')!r}",
        )


def copy_merged_to_resolved(merge_dir: Path, resolved_ports_dir: Path) -> None:
    if not merge_dir.exists():
        raise FileNotFoundError(f"Missing merged folder: {merge_dir}")
    resolved_ports_dir.mkdir(parents=True, exist_ok=True)
    for old_json in resolved_ports_dir.glob("*.json"):
        old_json.unlink()
    for src in merge_dir.glob("*.json"):
        shutil.copy2(src, resolved_ports_dir / src.name)


def run_port_resolution(target_files: list[str]) -> None:
    script_dir = Path(__file__).parent.resolve()
    builtin_dir = script_dir / "built-in"

    for target in target_files:
        target_path = resolve_source_target(script_dir, target)
        project_name = project_name_for_target(target)
        project_dir = script_dir / "outputs" / project_name
        merge_dir = project_dir / "merged"
        resolved_ports_dir = project_dir / "resolved_ports"
        source_data_dir = script_dir.parent / "data"

        print(f"Resolving topology ports for {target}")
        copy_merged_to_resolved(merge_dir, resolved_ports_dir)

        stage_target = resolved_ports_dir / target_path.name
        if not stage_target.exists():
            raise FileNotFoundError(f"Merged target JSON not found: {stage_target}")

        for path in sorted(resolved_ports_dir.glob("*.json")):
            data = load_json(path)
            resolved = resolve_ports_in_data(
                data,
                path.parent,
                builtin_dir,
                script_dir,
                stage_dir=resolved_ports_dir,
                resolved_dir=source_data_dir,
                context_name=path.name,
            )
            save_json(path, resolved)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Strictly resolve named topology ports to integers.")
    parser.add_argument("files", nargs="+", help="Target JSON file(s)")
    args = parser.parse_args(argv)
    run_port_resolution(args.files)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"[PORT RESOLUTION ERROR] {exc}", file=sys.stderr)
        raise SystemExit(1)
