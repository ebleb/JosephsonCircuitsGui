import json
import hashlib
import subprocess
from pathlib import Path
from graphlib import TopologicalSorter
import re

from path_utils import resolve_source_target
from simulation_helper_multimode import append_multimode_block_if_enabled

JULIA_GLOBAL_NAMES = {
    "im", "pi", "π", "exp", "sqrt", "sin", "cos", "tan",
    "log", "log10", "abs", "real", "imag", "w",
}



"""
simulation.py

Simulation stage input:

    outputs/<project>/specialized/

This stage should run after:

    merged -> validated -> netlisted -> specialized

Why specialized/:
    At this point each instance type_name has already been rewritten to a
    parameter-specialized component name such as:

        ABCD_shuntY__p_a1b2c3d4e5

    Therefore component values are encoded in the referenced JSON identity.
    The simulator no longer needs to decide whether two instances with the same
    base type have different values. It just simulates/cache-loads the concrete
    specialized child JSON.

Caching:
    The specialized filename already encodes component values.
    Still, the output also depends on topology and simulation settings
    such as frequency range, ports, HB pump settings, and multimode flags.
    So the cache key is the full JSON content hash. This is simpler and safer
    than maintaining CACHE_RELEVANT_KEYS by hand.
"""


class UnionFind:
    def __init__(self):
        self.parent = {}

    def find(self, i):
        if self.parent.setdefault(i, i) == i:
            return i
        self.parent[i] = self.find(self.parent[i])
        return self.parent[i]

    def union(self, i, j):
        root_i = self.find(i)
        root_j = self.find(j)

        if root_i != root_j:
            if root_j == "0":
                self.parent[root_i] = root_j
            elif root_i == "0":
                self.parent[root_j] = root_i
            else:
                self.parent[root_j] = root_i


def load_json(path):
    with open(path, "r") as f:
        return json.load(f)


def stable_json_hash(obj):
    payload = json.dumps(obj, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def cache_key_for_json(json_path):
    """
    Simplified cache key.

    Because component values are already encoded in specialized type names and
    specialized child JSONs, we can hash the full JSON content instead of
    maintaining a fragile list of relevant keys.
    """
    return stable_json_hash(load_json(json_path))


def manifest_path(cache_dir):
    return cache_dir / "cache_manifest.json"


def load_cache_manifest(cache_dir):
    path = manifest_path(cache_dir)
    if not path.exists():
        return {}

    with open(path, "r") as f:
        return json.load(f)


def update_cache_manifest(cache_dir, cell_name, cache_key, csv_path):
    manifest = load_cache_manifest(cache_dir)

    manifest[cell_name] = {
        "cache_key": cache_key,
        "csv": str(Path(csv_path).resolve()),
    }

    with open(manifest_path(cache_dir), "w") as f:
        json.dump(manifest, f, indent=2)


def prune_cache_manifest(cache_dir, valid_cell_names):
    path = manifest_path(cache_dir)
    if not path.exists():
        return

    manifest = load_cache_manifest(cache_dir)
    pruned = {
        cell_name: entry
        for cell_name, entry in manifest.items()
        if cell_name in valid_cell_names
    }

    if pruned == manifest:
        return

    with open(path, "w") as f:
        json.dump(pruned, f, indent=2)


def lookup_cached_csv(cache_dir, cell_name):
    manifest = load_cache_manifest(cache_dir)
    entry = manifest.get(cell_name)

    if not entry:
        return None

    csv_path = Path(entry["csv"])
    return csv_path if csv_path.exists() else None


def failed_manifest_path(cache_dir):
    return cache_dir / "failed_standalone.json"


def load_failed_standalone(cache_dir):
    path = failed_manifest_path(cache_dir)
    if not path.exists():
        return set()

    with open(path, "r") as f:
        return set(json.load(f))


def mark_failed_standalone(cache_dir, cell_name):
    failed = load_failed_standalone(cache_dir)
    failed.add(cell_name)

    with open(failed_manifest_path(cache_dir), "w") as f:
        json.dump(sorted(failed), f, indent=2)


def is_failed_standalone(cache_dir, cell_name):
    return cell_name in load_failed_standalone(cache_dir)


def resolve_component_json(type_name, simulation_dir, builtin_dir):
    """
    Resolve a component after specialization.

    First look in outputs/<project>/specialized, because specialized type names
    like ABCD_shuntY__p_xxx are real JSON files there.

    Fall back to built-in only for leaf built-ins that were not specialized.
    """
    target_name = Path(type_name).stem + ".json"

    path = simulation_dir / target_name
    if path.exists():
        return path

    if builtin_dir.exists():
        for p in builtin_dir.rglob(target_name):
            if p.is_file():
                return p

    return None



def require_int_port(value, context):
    if not isinstance(value, int):
        raise ValueError(
            f"{context}: expected integer port after variable propagation/merge, "
            f"got {value!r} ({type(value).__name__})"
        )
    return value


def instance_lookup(data):
    return {
        inst["uid"]: inst
        for inst in data.get("instances", [])
        if inst.get("uid")
    }


def validate_port_on_instance(data, uid, port_value, context):
    insts = instance_lookup(data)

    if uid not in insts:
        raise ValueError(f"{context}: unknown instance uid {uid!r}")

    port = require_int_port(port_value, context)
    port_count = int(insts[uid].get("port_count", 0))

    if port < 1 or port > port_count:
        raise ValueError(
            f"{context}: port {port} is invalid for instance {uid!r}; "
            f"valid range is 1..{port_count}"
        )

    return port


def validate_numeric_topology(data, context_name):
    """
    Simulation consumes already-merged/specialized numeric topology.

    Hierarchical name-to-number resolution must already be done by earlier
    stages. At simulation time, reject any missing, non-integer, or out-of-range
    topology ports.
    """
    for wire in data.get("wires", []) or []:
        src_uid = wire.get("source_instance_uid")
        tgt_uid = wire.get("target_instance_uid")

        if not src_uid or not tgt_uid:
            raise ValueError(f"{context_name}: wire missing source/target uid: {wire}")

        validate_port_on_instance(
            data,
            src_uid,
            wire.get("source_port"),
            f"{context_name} wire source {src_uid!r}",
        )
        validate_port_on_instance(
            data,
            tgt_uid,
            wire.get("target_port"),
            f"{context_name} wire target {tgt_uid!r}",
        )

    for pin in data.get("pins", []) or []:
        uid = pin.get("instance_uid")
        name = pin.get("name")

        if not uid or not name:
            raise ValueError(f"{context_name}: pin missing name/instance_uid: {pin}")

        validate_port_on_instance(
            data,
            uid,
            pin.get("port"),
            f"{context_name} exported pin {name!r}",
        )

    for label in data.get("labels", []) or []:
        uid = label.get("instance_uid")
        name = label.get("name")

        if not uid:
            raise ValueError(f"{context_name}: label missing instance_uid: {label}")

        validate_port_on_instance(
            data,
            uid,
            label.get("port"),
            f"{context_name} label {name!r}",
        )



def get_output_dir(target):
    script_dir = Path(__file__).parent.resolve() if "__file__" in globals() else Path.cwd().resolve()
    target_path = Path(target)
    project_name = target_path.parent.name if target_path.parent.name else "default_project"
    return script_dir / "outputs" / project_name


def get_original_stem_for_memo(path_or_name):
    """
    Specialized files may be named:
        ring_module__p_abcd1234.json

    The classification memo knows:
        ring_module.json

    This helper returns the base name before "__p_".
    """
    stem = Path(path_or_name).stem
    if "__p_" in stem:
        return stem.split("__p_")[0]
    if "_inline_" in stem:
        return stem.split("_inline_")[0]
    return stem


def get_memo_class_for_path(json_path, memo):
    json_path = Path(json_path).resolve()

    direct = memo.get(str(json_path))
    if direct:
        return direct.get("class", "unknown")

    original_stem = get_original_stem_for_memo(json_path)

    for key, value in memo.items():
        key_path = Path(key)
        if key_path.stem == original_stem:
            return value.get("class", "unknown")

    return "unknown"


def classify_json_for_simulation(json_path, memo):
    """
    Classify using actual JSON content first, then memo fallback.

    This is needed because specialized files do not necessarily exist in the
    original classification memo.
    """
    data = load_json(json_path)

    if data.get("type") == "built-in":
        subtype = data.get("subtype")
        if subtype == "sSolve":
            return "sparam_primitive"
        if subtype == "hbsolve":
            return "hbsolve_primitive"
        # Many primitive JSONs are just built-ins without subtype consistency.
        return get_memo_class_for_path(json_path, memo)

    if (
        (data.get("simulation") or {}).get("hb", {}).get("top_block") in [True, "true", "True"]
        or data.get("hb_top_block") in [True, "true", "True"]
    ):
        return "hbsolve_block"

    if data.get("type") == "schematic":
        return get_memo_class_for_path(json_path, memo)

    return get_memo_class_for_path(json_path, memo)


def base_cell_name_for_port_order(json_path):
    stem = Path(json_path).stem

    if "__p_" in stem:
        stem = stem.split("__p_")[0]

    if "_inline_" in stem:
        stem = stem.split("_inline_")[0]

    return stem


def load_json_port_order(project_dir, cell_name):
    """
    The netlist stage writes *_json_port_order.json in outputs/<project>/netlisted.
    Specialized stage may copy it, but we search both specialized and netlisted.
    """
    search_dirs = [
        project_dir / "specialized",
        project_dir / "netlisted",
        project_dir,
    ]

    for directory in search_dirs:
        if not directory.exists():
            continue

        matches = list(directory.glob("*_json_port_order.json"))
        if not matches:
            continue

        with open(matches[0], "r") as f:
            port_order = json.load(f)

        if cell_name in port_order:
            return port_order.get(cell_name)

    return None


def expected_ports_julia(project_dir, json_path):
    cell_name = base_cell_name_for_port_order(json_path)
    ports = load_json_port_order(project_dir, cell_name)

    if not ports:
        return "nothing"

    items = []

    for p in ports:
        if not (isinstance(p, list) and len(p) == 2):
            raise ValueError(
                f"Unsupported port format for {cell_name}: {p}. "
                "Expected [instance_uid, integer_port]."
            )

        instance_uid, instance_port = p
        instance_port = require_int_port(
            instance_port,
            f"{cell_name} expected port order for {instance_uid!r}",
        )

        items.append(f'("{instance_uid}", {instance_port})')

    return "[" + ", ".join(items) + "]"



def _uid_group_prefix(uid):
    """
    Compact HB repeated blocks produced by the merger look like:

        S1_TCM1_C1
        S1_TC1_L1
        S1_TC3_C2

    where S1 is the repeated parent instance uid. This helper extracts S1.
    """
    uid = str(uid)
    if "_" not in uid:
        return uid
    return uid.split("_", 1)[0]


def _copy_uid_for_repeat(group_prefix, uid, rep_idx):
    uid = str(uid)
    prefix = f"{group_prefix}_"
    if uid.startswith(prefix):
        suffix = uid[len(prefix):]
        return f"{group_prefix}_{rep_idx}_{suffix}"
    return f"{uid}_{rep_idx}"


def expand_repeated_hb_groups_for_simulation(data):
    """
    Expand compact repeated HB primitive groups into a flat primitive circuit.

    Why this exists:
        The merger no longer expands repeat_count. That is good for keeping JSON
        compact, but hbsolve still needs an explicit primitive circuit.

    Semantics:
        repeat_count means cascade N copies of the same 2-port block.

    Expected compact JSON shape after merger:
        - all primitives belonging to the repeated parent carry the same
          repeat_count > 1
        - their uids share the repeated parent prefix, e.g. S1_...
        - exactly two boundary connections connect the repeated group to the
          outside circuit

    Expansion:
        S1_TCM1_C1, ..., S1_TC3_C2 with repeat_count=N becomes:
            S1_1_TCM1_C1, ..., S1_1_TC3_C2
            S1_2_TCM1_C1, ..., S1_2_TC3_C2
            ...
            S1_N_TCM1_C1, ..., S1_N_TC3_C2

        and adds cascade wires:
            group_output(copy i) -> group_input(copy i+1)
    """
    instances = data.get("instances", [])
    repeated = [
        inst for inst in instances
        if int(inst.get("repeat_count", 1) or 1) > 1
    ]

    if not repeated:
        return data

    out = dict(data)

    # Group repeated primitives by their repeated parent prefix.
    groups = {}
    for inst in repeated:
        prefix = _uid_group_prefix(inst["uid"])
        groups.setdefault(prefix, []).append(inst)

    repeated_uids = {
        inst["uid"]
        for group in groups.values()
        for inst in group
    }

    new_instances = []

    # Keep non-repeated instances as-is.
    for inst in instances:
        if inst.get("uid") not in repeated_uids:
            new_instances.append(dict(inst))

    group_info = {}

    for prefix, group_insts in groups.items():
        repeat_counts = sorted(set(int(inst.get("repeat_count", 1) or 1) for inst in group_insts))
        if len(repeat_counts) != 1:
            raise ValueError(
                f"Repeated HB group {prefix} has inconsistent repeat_count values: {repeat_counts}"
            )

        repeat_count = repeat_counts[0]

        # Only 2-port cascades are supported.
        # The repeated group can contain many primitives internally, but the
        # group itself must expose exactly two boundary ports.
        group_uids = {inst["uid"] for inst in group_insts}

        boundary = []
        for wire in data.get("wires", []):
            src_uid = wire.get("source_instance_uid")
            tgt_uid = wire.get("target_instance_uid")
            src_in = src_uid in group_uids
            tgt_in = tgt_uid in group_uids

            if src_in ^ tgt_in:
                if src_in:
                    boundary.append({
                        "group_uid": src_uid,
                        "group_port": require_int_port(wire.get("source_port"), f"wire source {wire.get('source_instance_uid')!r}"),
                        "outside_uid": tgt_uid,
                        "outside_port": require_int_port(wire.get("target_port"), f"wire target {wire.get('target_instance_uid')!r}"),
                        "group_is_source": True,
                        "wire": wire,
                    })
                else:
                    boundary.append({
                        "group_uid": tgt_uid,
                        "group_port": require_int_port(wire.get("target_port"), f"wire target {wire.get('target_instance_uid')!r}"),
                        "outside_uid": src_uid,
                        "outside_port": require_int_port(wire.get("source_port"), f"wire source {wire.get('source_instance_uid')!r}"),
                        "group_is_source": False,
                        "wire": wire,
                    })

        if len(boundary) != 2:
            raise ValueError(
                f"Repeated HB group {prefix} must have exactly two boundary connections, "
                f"found {len(boundary)}. Boundary={boundary}"
            )

        # Prefer orientation:
        #   outside -> group = cascade input
        #   group -> outside = cascade output
        input_candidates = [b for b in boundary if not b["group_is_source"]]
        output_candidates = [b for b in boundary if b["group_is_source"]]

        if len(input_candidates) == 1 and len(output_candidates) == 1:
            start = input_candidates[0]
            end = output_candidates[0]
        else:
            # Fallback: deterministic but explicit. This avoids silently failing
            # if wire direction is arbitrary, while still allowing simple cases.
            start, end = boundary[0], boundary[1]

        group_info[prefix] = {
            "repeat_count": repeat_count,
            "uids": group_uids,
            "start": start,
            "end": end,
        }

        for rep_idx in range(1, repeat_count + 1):
            for inst in group_insts:
                new_inst = dict(inst)
                new_inst["uid"] = _copy_uid_for_repeat(prefix, inst["uid"], rep_idx)
                new_inst["repeat_count"] = 1
                new_instances.append(new_inst)

    def map_group_endpoint(uid, port, rep_idx):
        prefix = _uid_group_prefix(uid)
        if prefix not in group_info:
            return uid, port
        return _copy_uid_for_repeat(prefix, uid, rep_idx), port

    def endpoint_key(uid, port):
        return (uid, int(port))

    new_wires = []

    for wire in data.get("wires", []):
        src_uid = wire.get("source_instance_uid")
        tgt_uid = wire.get("target_instance_uid")
        src_prefix = _uid_group_prefix(src_uid)
        tgt_prefix = _uid_group_prefix(tgt_uid)
        src_group = src_prefix if src_uid in group_info.get(src_prefix, {}).get("uids", set()) else None
        tgt_group = tgt_prefix if tgt_uid in group_info.get(tgt_prefix, {}).get("uids", set()) else None

        src_repeated = src_uid in repeated_uids
        tgt_repeated = tgt_uid in repeated_uids

        if src_repeated and tgt_repeated:
            if src_prefix != tgt_prefix:
                raise ValueError(
                    f"Wire connects two different repeated HB groups: {wire}"
                )

            info = group_info[src_prefix]
            for rep_idx in range(1, info["repeat_count"] + 1):
                new_wire = dict(wire)
                new_wire["source_instance_uid"], new_wire["source_port"] = map_group_endpoint(
                    src_uid, require_int_port(wire.get("source_port"), f"wire source {wire.get('source_instance_uid')!r}"), rep_idx
                )
                new_wire["target_instance_uid"], new_wire["target_port"] = map_group_endpoint(
                    tgt_uid, require_int_port(wire.get("target_port"), f"wire target {wire.get('target_instance_uid')!r}"), rep_idx
                )
                new_wires.append(new_wire)

        elif src_repeated ^ tgt_repeated:
            prefix = src_prefix if src_repeated else tgt_prefix
            info = group_info[prefix]
            start = info["start"]
            end = info["end"]
            repeat_count = info["repeat_count"]

            group_uid = src_uid if src_repeated else tgt_uid
            group_port = require_int_port(wire.get("source_port" if src_repeated else "target_port"), f"repeated boundary wire {wire}")
            key = endpoint_key(group_uid, group_port)

            start_key = endpoint_key(start["group_uid"], start["group_port"])
            end_key = endpoint_key(end["group_uid"], end["group_port"])

            if key == start_key:
                rep_idx = 1
            elif key == end_key:
                rep_idx = repeat_count
            else:
                raise ValueError(
                    f"Boundary wire touches repeated HB group {prefix} at neither "
                    f"start nor end endpoint: {wire}"
                )

            new_wire = dict(wire)
            if src_repeated:
                new_wire["source_instance_uid"], new_wire["source_port"] = map_group_endpoint(
                    src_uid, require_int_port(wire.get("source_port"), f"wire source {wire.get('source_instance_uid')!r}"), rep_idx
                )
            else:
                new_wire["target_instance_uid"], new_wire["target_port"] = map_group_endpoint(
                    tgt_uid, require_int_port(wire.get("target_port"), f"wire target {wire.get('target_instance_uid')!r}"), rep_idx
                )
            new_wires.append(new_wire)

        else:
            new_wires.append(dict(wire))

    # Add cascade wires from output endpoint of copy i to input endpoint of copy i+1.
    for prefix, info in group_info.items():
        start = info["start"]
        end = info["end"]
        repeat_count = info["repeat_count"]

        for rep_idx in range(1, repeat_count):
            end_uid = _copy_uid_for_repeat(prefix, end["group_uid"], rep_idx)
            start_uid = _copy_uid_for_repeat(prefix, start["group_uid"], rep_idx + 1)

            new_wires.append({
                "source_instance_uid": end_uid,
                "source_port": int(end["group_port"]),
                "target_instance_uid": start_uid,
                "target_port": int(start["group_port"]),
                "name": "",
            })

    new_labels = []
    for label in data.get("labels", []):
        uid = label.get("instance_uid")
        if uid in repeated_uids:
            prefix = _uid_group_prefix(uid)
            info = group_info[prefix]
            for rep_idx in range(1, info["repeat_count"] + 1):
                new_label = dict(label)
                new_label["instance_uid"] = _copy_uid_for_repeat(prefix, uid, rep_idx)
                new_labels.append(new_label)
        else:
            new_labels.append(dict(label))

    new_pins = []
    for pin in data.get("pins", []):
        uid = pin.get("instance_uid")
        port = require_int_port(pin.get("port"), f"top-level pin {pin.get('name')!r}")
        if uid in repeated_uids:
            prefix = _uid_group_prefix(uid)
            info = group_info[prefix]
            start_key = endpoint_key(info["start"]["group_uid"], info["start"]["group_port"])
            end_key = endpoint_key(info["end"]["group_uid"], info["end"]["group_port"])
            key = endpoint_key(uid, port)

            if key == start_key:
                rep_idx = 1
            elif key == end_key:
                rep_idx = info["repeat_count"]
            else:
                raise ValueError(
                    f"Top-level pin touches repeated HB group {prefix} at an internal "
                    f"endpoint, not the cascade boundary: {pin}"
                )

            new_pin = dict(pin)
            new_pin["instance_uid"] = _copy_uid_for_repeat(prefix, uid, rep_idx)
            new_pins.append(new_pin)
        else:
            new_pins.append(dict(pin))

    out["instances"] = new_instances
    out["wires"] = new_wires
    out["labels"] = new_labels
    out["pins"] = new_pins

    return out



def build_nodal_netlist(data):
    validate_numeric_topology(data, data.get("name", "<unknown>"))

    uf = UnionFind()

    # Initialize all primitive ports so floating primitives can be caught cleanly.
    for inst in data.get("instances", []):
        uid = inst.get("uid")
        port_count = int(inst.get("port_count", 0))
        if uid:
            for port in range(1, port_count + 1):
                uf.find(f"{uid}:{port}")

    for wire in data.get("wires", []):
        src_uid = wire["source_instance_uid"]
        tgt_uid = wire["target_instance_uid"]
        src_port = validate_port_on_instance(
            data,
            src_uid,
            wire.get("source_port"),
            f"wire source {src_uid!r}",
        )
        tgt_port = validate_port_on_instance(
            data,
            tgt_uid,
            wire.get("target_port"),
            f"wire target {tgt_uid!r}",
        )

        src = f"{src_uid}:{src_port}"
        tgt = f"{tgt_uid}:{tgt_port}"
        uf.union(src, tgt)

    for label in data.get("labels", []):
        name = str(label.get("name", ""))
        if name in ["0", "P_0", "GND", "gnd"]:
            uid = label["instance_uid"]
            port = validate_port_on_instance(
                data,
                uid,
                label.get("port"),
                f"ground label {name!r} on {uid!r}",
            )
            pin = f"{uid}:{port}"
            uf.union(pin, "0")

    root_to_int = {"0": 0}
    next_node_id = 1
    node_map = {}

    for key in list(uf.parent.keys()):
        root = uf.find(key)
        if root not in root_to_int:
            root_to_int[root] = next_node_id
            next_node_id += 1
        node_map[key] = root_to_int[root]

    return node_map, next_node_id


def get_primary_value(obj, default="1.0"):
    """
    Works for both instances and component JSONs that use parameter_order.
    """
    param_order = obj.get("parameter_order", [])
    if not param_order:
        return default

    primary_key = param_order[0]
    params = obj.get("parameters", {})
    if primary_key in params and params[primary_key] not in ["", None]:
        return params[primary_key]

    # Specialized component JSONs may store variables instead.
    for var in obj.get("variables", []):
        if var.get("name") == primary_key:
            return var.get("resolved", var.get("default", default))

    return default


def get_parameter_value(obj, name, default=None):
    resolved = obj.get("resolved_parameters", {}) or {}
    if name in resolved and resolved[name] not in ["", None]:
        return resolved[name]

    params = obj.get("parameters", {}) or {}
    if name in params and params[name] not in ["", None]:
        return params[name]

    for var in obj.get("variables", []) or []:
        if var.get("name") == name:
            value = var.get("resolved", var.get("default", default))
            if value not in ["", None]:
                return value

    return default


def get_parameter_value_from_instance_or_child(inst, child_data, name, default=None):
    value = get_parameter_value(inst, name, default=None)
    if value not in ["", None]:
        return value

    if child_data is not None:
        for var in child_data.get("variables", []) or []:
            if var.get("name") == name:
                value = var.get("resolved", var.get("default", default))
                if value not in ["", None]:
                    return value

        specialized = child_data.get("specialized_parameters", {}) or {}
        if name in specialized and specialized[name] not in ["", None]:
            return specialized[name]

    return default


def variable_assignments_from_child_and_instance(inst, child_data=None):
    """
    Julia assignments for matrix-code built-ins.

    After specialization, concrete parameter values live in the child JSON
    variables/specialized_parameters. Instance parameters may still exist for
    traceability, but the child JSON is the source of truth.
    """
    values = {}

    if child_data:
        for var in child_data.get("variables", []) or []:
            name = var.get("name")
            if not name:
                continue
            value = var.get("resolved", var.get("default"))
            if value not in ["", None]:
                values[name] = value

        for name, value in (child_data.get("specialized_parameters", {}) or {}).items():
            if value not in ["", None]:
                values.setdefault(name, value)

    for name, value in (inst.get("parameters", {}) or {}).items():
        if value not in ["", None]:
            values.setdefault(name, value)

    lines = []
    for name, value in values.items():
        if name == "w" and value in ["", None, "w"]:
            continue
        lines.append(f"        {name} = {value}")

    return "\n".join(lines)


def is_singular_solve_error(err):
    text = ""

    if getattr(err, "stdout", None):
        text += str(err.stdout)

    if getattr(err, "stderr", None):
        text += str(err.stderr)

    singular_markers = [
        "SingularException",
        "singular",
        "Singular",
        "LinearAlgebra.SingularException",
    ]

    return any(marker in text for marker in singular_markers)


def extract_hbsolve_ports(data):
    ports = []

    for inst in data.get("instances", []):
        if inst.get("type_name") != "P":
            continue

        val = get_primary_value(inst, default=None)
        if val is not None:
            ports.append(int(val))

    ports = sorted(set(ports))

    if not ports:
        raise ValueError("No HBSolve P ports found in flattened circuit.")

    if len(ports) == 1:
        return f"({ports[0]},)"

    return "(" + ", ".join(str(p) for p in ports) + ")"


def extract_hbsolve_port_numbers(data):
    ports = []

    for inst in data.get("instances", []):
        if inst.get("type_name") != "P":
            continue

        val = get_primary_value(inst, default=None)
        if val is not None:
            ports.append(int(val))

    ports = sorted(set(ports))

    if not ports:
        raise ValueError("No HBSolve P ports found in flattened circuit.")

    return ports


def julia_int_tuple(values):
    values = list(values)
    if len(values) == 1:
        return f"({values[0]},)"
    return "(" + ", ".join(str(v) for v in values) + ")"


def julia_port_tuple_array(ports):
    items = [f'("{uid}", {port})' for uid, port in ports]
    return "[" + ", ".join(items) + "]"


def extract_hbsolve_sol_ports(data, port_numbers):
    by_p_number = {}

    for info in (data.get("hb_exposed_pin_to_p_block", {}) or {}).values():
        try:
            p_number = int(info["p_port_number"])
            uid = str(info["p_instance_uid"])
            local_port = int(info["p_local_port"])
        except (KeyError, TypeError, ValueError):
            continue

        by_p_number.setdefault(p_number, (uid, local_port))

    if all(port in by_p_number for port in port_numbers):
        return [by_p_number[port] for port in port_numbers]

    fallback = []
    for port in port_numbers:
        for inst in data.get("instances", []):
            if inst.get("type_name") != "P":
                continue
            if int(get_primary_value(inst, default=-1)) == port:
                fallback.append((inst["uid"], 1))
                break

    if len(fallback) == len(port_numbers):
        return fallback

    raise ValueError("Could not map HBSolve P ports back to JSON endpoint ports.")


def extract_sim_variables_julia(data):
    lines = []
    function_names = cell_function_names(data)

    z0 = (data.get("simulation") or {}).get("z0") or data.get("z0")
    if z0 not in ["", None]:
        lines.append(f"Z0 = {z0}")
        lines.append(f"z0 = {z0}")
        lines.append(f"z_0 = {z0}")
        lines.append(f"Z_0 = {z0}")

    for source_key in ["simulation_variables", "variables"]:
        for var in data.get(source_key, []):
            name = var.get("name")
            value = var.get("resolved", var.get("default", "0.0"))

            if not name:
                continue

            if name in function_names or (name in JULIA_GLOBAL_NAMES and (name != "w" or value in ["", None, "w"])):
                continue

            lines.append(f"{name} = {value}")

    return "\n".join(lines)


def expression_function_names(expr):
    return set(re.findall(r"\b[A-Za-z_]\w*\b(?=\s*\()", str(expr)))


def cell_function_names(data):
    names = set()
    for key in ["variables", "simulation_variables"]:
        for var in data.get(key, []) or []:
            names.update(expression_function_names(var.get("resolved", var.get("default", var.get("value", "")))))
    for inst in data.get("instances", []) or []:
        for value in (inst.get("parameters", {}) or {}).values():
            names.update(expression_function_names(value))
        for value in (inst.get("resolved_parameters", {}) or {}).values():
            names.update(expression_function_names(value))
    for value in (data.get("specialized_parameters", {}) or {}).values():
        names.update(expression_function_names(value))
    return names


def frequency_settings(data):
    f_start = data.get("simulation_freq_start", 1.0)
    f_stop = data.get("simulation_freq_stop", 14.0)
    f_points = data.get("simulation_freq_points", 200)
    return f_start, f_stop, f_points



def generate_builtin_sparam_script(
    json_path,
    out_jl_path,
    out_csv_path,
):
    """
    Simulate a specialized built-in sSolve component directly.

    Crucial detail:
        Variables such as thetae = ne0*w/c*L depend on w.
        w is only defined inside the frequency loop, so ALL component variable
        assignments are emitted inside the loop.

    This matches variable propagation semantics: w remains symbolic until the
    simulation loop sets w = ws[k].
    """
    data = load_json(json_path)

    if data.get("type") != "built-in" or data.get("subtype") != "sSolve":
        raise ValueError(f"{json_path.name} is not a built-in sSolve component.")

    matrix_code = data.get("matrix_code")
    if not matrix_code:
        raise ValueError(f"Built-in sSolve component {json_path.name} has no matrix_code.")

    matrix_type = str(data.get("matrix_type", "S")).upper()

    if matrix_type == "ABCD":
        matrix_expr = f"JosephsonCircuits.AtoS(ComplexF64.({matrix_code}))"
    elif matrix_type == "S":
        matrix_expr = matrix_code
    else:
        raise ValueError(
            f"Built-in sSolve component {json_path.name} has unsupported matrix_type={matrix_type!r}"
        )

    f_start, f_stop, f_points = frequency_settings(data)

    # Only emit z0 aliases globally. Do not emit variables here, because some
    # may depend on w.
    z0_lines = []
    _z0 = (data.get("simulation") or {}).get("z0") or data.get("z0")
    if _z0 not in ["", None]:
        z0_lines.extend([
            f"Z0 = {_z0}",
            f"z0 = {_z0}",
            f"z_0 = {_z0}",
            f"Z_0 = {_z0}",
        ])
    z0_str = "\n".join(z0_lines)

    # Component variables go inside the loop. De-duplicate by name.
    values = {}

    for var in data.get("variables", []) or []:
        name = var.get("name")
        if not name:
            continue
        value = var.get("resolved", var.get("default"))
        if value in ["", None]:
            continue
        if name == "w" and str(value).strip() == "w":
            continue
        values[name] = value

    for name, value in (data.get("specialized_parameters", {}) or {}).items():
        if value in ["", None]:
            continue
        if name == "w" and str(value).strip() == "w":
            continue
        values.setdefault(name, value)

    loop_assignments = "\n".join(
        f"    {name} = {value}"
        for name, value in values.items()
    )

    jl_code = f"""using DelimitedFiles
using LinearAlgebra
using JosephsonCircuits

{z0_str}

ws = 2 * pi * range({f_start}, {f_stop}, length={f_points}) * 1e9

function save_s_matrix(filepath, ws, S)
    num_freqs = length(ws)
    num_ports = size(S, 1)

    out_data = zeros(Float64, num_freqs, 1 + 2 * (num_ports^2))
    out_data[:, 1] = ws ./ (2*pi*1e9)

    col = 2
    for out_p in 1:num_ports
        for in_p in 1:num_ports
            out_data[:, col] = real.(S[out_p, in_p, :])
            out_data[:, col + 1] = imag.(S[out_p, in_p, :])
            col += 2
        end
    end

    writedlm(filepath, out_data, ',')
end

println("Running built-in sSolve on {json_path.name}...")

S_first = nothing

for k in eachindex(ws)
    w = ws[k]
{loop_assignments}

    S_k = {matrix_expr}

    if S_first === nothing
        global S_first = zeros(ComplexF64, size(S_k, 1), size(S_k, 2), length(ws))
    end

    S_first[:, :, k] .= S_k
end

save_s_matrix("{out_csv_path.as_posix()}", ws, S_first)
println("Saved built-in sSolve cache to {out_csv_path.name}")
"""

    with open(out_jl_path, "w") as f:
        f.write(jl_code)


def generate_direct_s_script(json_path, cache_dir, out_jl_path, out_csv_path):
    """Simulate a direct-S-matrix cell (ABCD loop, no solveS) by wrapping its
    generated_source in a standalone Julia function and evaluating it."""
    data = load_json(json_path)
    source = (data.get("generated_source") or "").strip()
    if not source:
        raise ValueError(f"{json_path.name} has no generated_source for direct_s simulation.")

    f_start, f_stop, f_points = frequency_settings(data)

    # Collect concrete variable values
    values = {}
    for var in data.get("variables", []) or []:
        name = var.get("name")
        value = var.get("resolved", var.get("default", var.get("value")))
        if name and value not in ("", None) and str(value).strip():
            values[name] = value
    for name, value in (data.get("specialized_parameters", {}) or {}).items():
        if value not in ("", None) and str(value).strip():
            values.setdefault(name, value)

    var_assignments = "\n".join(f"{n} = {v}" for n, v in values.items())

    # Indent every line of the function body
    indented = "\n".join("    " + line if line.strip() else "" for line in source.splitlines())

    jl_code = f"""using DelimitedFiles
using LinearAlgebra
using JosephsonCircuits

{var_assignments}

ws = 2 * pi * range({f_start}, {f_stop}, length={f_points}) * 1e9

function save_s_matrix(filepath, ws, S)
    num_freqs = length(ws)
    num_ports = size(S, 1)
    out_data = zeros(Float64, num_freqs, 1 + 2 * (num_ports^2))
    out_data[:, 1] = ws ./ (2*pi*1e9)
    col = 2
    for out_p in 1:num_ports
        for in_p in 1:num_ports
            out_data[:, col]     = real.(S[out_p, in_p, :])
            out_data[:, col + 1] = imag.(S[out_p, in_p, :])
            col += 2
        end
    end
    writedlm(filepath, out_data, ',')
end

function _direct_s_fn(w)
{indented}
end

println("Running direct S-matrix on {json_path.name}...")
S = _direct_s_fn(ws)

save_s_matrix("{out_csv_path.as_posix()}", ws, S)
println("Saved direct S-matrix cache to {out_csv_path.name}")
"""
    with open(out_jl_path, "w") as f:
        f.write(jl_code)


def hb_uid_group_prefix(uid):
    """
    Compact HB repeated primitives produced by the merger usually look like:
        S1_TCM1_C1
        S1_TC1_L1

    The repeated parent prefix is S1.
    """
    uid = str(uid)
    return uid.split("_", 1)[0] if "_" in uid else uid


def collect_hb_repeat_groups(data, node_map):
    """
    Identify repeated HB primitive groups and derive compact cascade metadata.

    This does NOT expand the circuit. It only computes enough metadata for the
    Julia generator to build repeated circuit entries with for loops.
    """
    instances = data.get("instances", [])
    repeated_instances = [
        inst for inst in instances
        if int(inst.get("repeat_count", 1) or 1) > 1
    ]

    groups = {}
    for inst in repeated_instances:
        uid = inst.get("uid")
        if not uid:
            continue

        if inst.get("type_name") == "P":
            raise ValueError(
                f"Invalid repeat_count on P block {uid}. Repeated groups may not contain P blocks."
            )

        prefix = hb_uid_group_prefix(uid)
        groups.setdefault(prefix, []).append(inst)

    group_info = {}

    for prefix, group_insts in groups.items():
        repeat_counts = sorted(
            set(int(inst.get("repeat_count", 1) or 1) for inst in group_insts)
        )
        if len(repeat_counts) != 1:
            raise ValueError(
                f"Repeated HB group {prefix} has inconsistent repeat_count values: {repeat_counts}"
            )

        repeat_count = repeat_counts[0]
        group_uids = {inst["uid"] for inst in group_insts}

        # Find external boundary connections. Multiple primitive endpoints can
        # legitimately sit on the same boundary net, for example parallel L/C
        # endpoints connected to the same outside node. Treat those as one
        # electrical boundary, not as multiple cascade boundaries.
        boundary = []
        for wire in data.get("wires", []):
            src_uid = wire.get("source_instance_uid")
            tgt_uid = wire.get("target_instance_uid")
            src_in = src_uid in group_uids
            tgt_in = tgt_uid in group_uids

            if src_in ^ tgt_in:
                if src_in:
                    group_port = require_int_port(
                        wire.get("source_port"),
                        f"wire source {wire.get('source_instance_uid')!r}",
                    )
                    boundary.append({
                        "group_uid": src_uid,
                        "group_port": group_port,
                        "outside_uid": tgt_uid,
                        "outside_port": require_int_port(
                            wire.get("target_port"),
                            f"wire target {wire.get('target_instance_uid')!r}",
                        ),
                        "group_is_source": True,
                        "boundary_node": node_map[f"{src_uid}:{group_port}"],
                    })
                else:
                    group_port = require_int_port(
                        wire.get("target_port"),
                        f"wire target {wire.get('target_instance_uid')!r}",
                    )
                    boundary.append({
                        "group_uid": tgt_uid,
                        "group_port": group_port,
                        "outside_uid": src_uid,
                        "outside_port": require_int_port(
                            wire.get("source_port"),
                            f"wire source {wire.get('source_instance_uid')!r}",
                        ),
                        "group_is_source": False,
                        "boundary_node": node_map[f"{tgt_uid}:{group_port}"],
                    })

        boundary_by_node = {}
        boundary_nodes_in_order = []
        for entry in boundary:
            node = entry["boundary_node"]
            if node not in boundary_by_node:
                boundary_by_node[node] = []
                boundary_nodes_in_order.append(node)
            boundary_by_node[node].append(entry)

        if len(boundary_by_node) != 2:
            raise ValueError(
                f"Repeated HB group {prefix} must have exactly two boundary electrical nodes, "
                f"found {len(boundary_by_node)}. Boundary={boundary}"
            )

        # Preferred orientation:
        #   outside -> group = cascade input
        #   group -> outside = cascade output
        input_nodes = [
            node for node, entries in boundary_by_node.items()
            if any(not entry["group_is_source"] for entry in entries)
            and not any(entry["group_is_source"] for entry in entries)
        ]
        output_nodes = [
            node for node, entries in boundary_by_node.items()
            if any(entry["group_is_source"] for entry in entries)
            and not any(not entry["group_is_source"] for entry in entries)
        ]

        if len(input_nodes) == 1 and len(output_nodes) == 1:
            start_node = input_nodes[0]
            end_node = output_nodes[0]
        else:
            # Deterministic fallback if wire direction is arbitrary. Preserve
            # the old wire-order behavior, but after collapsing duplicate
            # electrical boundary entries.
            start_node, end_node = boundary_nodes_in_order[0], boundary_nodes_in_order[1]

        group_nodes = set()
        for inst in group_insts:
            uid = inst["uid"]
            for port in range(1, int(inst.get("port_count", 2)) + 1):
                key = f"{uid}:{port}"
                if key in node_map:
                    group_nodes.add(node_map[key])

        # 0 is global ground and never gets replicated. The cascade start/end
        # nodes are handled specially. Everything else is local to each copy.
        internal_nodes = sorted(n for n in group_nodes if n not in {0, start_node, end_node})

        group_info[prefix] = {
            "prefix": prefix,
            "repeat_count": repeat_count,
            "uids": group_uids,
            "instances": group_insts,
            "start_node": start_node,
            "end_node": end_node,
            "internal_nodes": internal_nodes,
        }

    return group_info


def sanitize_julia_identifier(name):
    safe = re.sub(r"[^A-Za-z0-9_]", "_", str(name))
    if not safe or safe[0].isdigit():
        safe = "_" + safe
    return safe


def generate_hb_repeat_node_helpers(group_info, first_free_node):
    """
    Emit Julia helper functions for repeated HB groups.

    The compact nodal solution gives one logical copy of a repeated block.
    Julia then maps each local node into the repeated cascade:

        local input node:
            copy 1 -> original input node
            copy r>1 -> cascade link node r-1

        local output node:
            copy N -> original output node
            copy r<N -> cascade link node r

        internal nodes:
            unique per copy

        ground node 0:
            always 0
    """
    helper_lines = []
    next_free = first_free_node

    for prefix, info in group_info.items():
        safe = sanitize_julia_identifier(prefix)
        repeat_count = info["repeat_count"]
        internal_nodes = info["internal_nodes"]

        link_base = next_free
        link_count = max(repeat_count - 1, 0)
        next_free += link_count

        internal_base = next_free
        internal_stride = len(internal_nodes)
        internal_count = repeat_count * internal_stride
        next_free += internal_count

        internal_map_lines = []
        for idx, local_node in enumerate(internal_nodes, start=1):
            internal_map_lines.append(
                f"    elseif local_node == {local_node}\n"
                f"        return {internal_base} + (rep_idx - 1) * {internal_stride} + {idx - 1}"
            )

        internal_map = "\n".join(internal_map_lines)

        helper_lines.append(f"""
function {safe}_link_node(rep_idx)
    return {link_base} + rep_idx - 1
end

function {safe}_node(local_node, rep_idx)
    if local_node == 0
        return 0
    elseif local_node == {info['start_node']}
        return rep_idx == 1 ? {info['start_node']} : {safe}_link_node(rep_idx - 1)
    elseif local_node == {info['end_node']}
        return rep_idx == {repeat_count} ? {info['end_node']} : {safe}_link_node(rep_idx)
{internal_map}
    else
        error("Unknown local node $(local_node) in repeated HB group {prefix}")
    end
end
""")

    return "\n".join(helper_lines), next_free


def hb_node_expr_for_inst_port(uid, port, node_map, group_info):
    key = f"{uid}:{port}"
    local_node = node_map[key]

    prefix = hb_uid_group_prefix(uid)
    if prefix in group_info and uid in group_info[prefix]["uids"]:
        safe = sanitize_julia_identifier(prefix)
        return f"{safe}_node({local_node}, rep_idx)"

    return str(local_node)


def resolve_hb_branch_name(data, raw_name):
    if raw_name in ["", None]:
        return None

    raw = str(raw_name).strip()
    instances = data.get("instances", []) or []

    def generated_name(inst):
        return f"{inst.get('type_name')}_{inst.get('uid')}"

    # Exact UID match in the flattened circuit.
    for inst in instances:
        uid = inst.get("uid")
        if uid == raw:
            return generated_name(inst)

    # Already-expanded generated branch name.
    for inst in instances:
        if generated_name(inst) == raw:
            return raw

    # Match local schematic names against flattened UIDs such as FL1_L2.
    suffix = "_" + raw
    matches = [inst for inst in instances if str(inst.get("uid", "")).endswith(suffix)]
    if len(matches) == 1:
        return generated_name(matches[0])

    if len(matches) > 1:
        raise ValueError(
            f"Ambiguous HB branch reference {raw!r}; matches {[inst.get('uid') for inst in matches]}"
        )

    return None


def find_builtin_dir(script_dir):
    direct = script_dir / "built-in"
    if direct.is_dir():
        return direct
    alt = script_dir.parent / "built-in"
    if alt.is_dir():
        return alt
    return direct


def hb_primitive_metadata(inst, simulation_dir, builtin_dir):
    t_name = str(inst.get("type_name", ""))
    direct_primitives = {"Lj", "NL", "L", "C", "K", "I", "R", "P", "Cj"}

    if t_name in direct_primitives:
        return {
            "primitive_name": t_name,
            "port_count": int(inst.get("port_count", 0)),
            "child_data": None,
        }

    child_path = resolve_component_json(t_name, simulation_dir, builtin_dir)
    if child_path is None:
        return None

    child_data = load_json(child_path)
    if child_data.get("type") != "built-in" or child_data.get("subtype") != "hbsolve":
        return None

    primitive_name = (
        child_data.get("specialized_from")
        or child_data.get("name")
        or Path(t_name).stem
    )

    return {
        "primitive_name": str(primitive_name),
        "port_count": int(child_data.get("port_count", inst.get("port_count", 0))),
        "child_data": child_data,
    }


def generate_hb_circuit_push_lines(data, node_map, group_info, simulation_dir, builtin_dir):
    """
    Generate Julia code that pushes primitive entries into `circuit`.

    Repeated HB groups are emitted as Julia `for rep_idx in 1:N` loops.
    Non-repeated primitives are emitted once.
    """
    repeated_uids = {
        uid
        for info in group_info.values()
        for uid in info["uids"]
    }

    lines = []

    # Non-repeated primitives.
    for inst in data.get("instances", []):
        uid = inst.get("uid", "UnknownUID")
        info = hb_primitive_metadata(inst, simulation_dir, builtin_dir)

        if info is None:
            continue
        if uid in repeated_uids:
            continue

        t_name = info["primitive_name"]
        child_data = info["child_data"]

        if t_name == "K":
            inductor_a = get_parameter_value_from_instance_or_child(inst, child_data, "inductor_a")
            inductor_b = get_parameter_value_from_instance_or_child(inst, child_data, "inductor_b")
            coupling = get_parameter_value_from_instance_or_child(inst, child_data, "K", default="1.0")
            branch_a = resolve_hb_branch_name(data, inductor_a)
            branch_b = resolve_hb_branch_name(data, inductor_b)

            if branch_a is None or branch_b is None:
                raise ValueError(
                    f"HB primitive {uid!r} of type 'K' must resolve both "
                    f"inductor names inductor_a={inductor_a!r}, inductor_b={inductor_b!r}."
                )

            lines.append(
                f'push!(circuit, ("K", {julia_string(branch_a)}, {julia_string(branch_b)}, {coupling}))'
            )
            continue

        port_count = info["port_count"]
        if port_count != 2:
            raise ValueError(
                f"HB primitive {uid!r} of type {t_name!r} must have exactly 2 ports, "
                f"got port_count={port_count}"
            )

        if child_data is not None:
            variables = child_data.get("variables", []) or []
            primary_key = variables[0].get("name") if variables else None
            val = get_parameter_value_from_instance_or_child(inst, child_data, primary_key, default="1.0")
        else:
            val = get_primary_value(inst, default="1.0")
        n1 = hb_node_expr_for_inst_port(uid, 1, node_map, group_info)
        n2 = hb_node_expr_for_inst_port(uid, 2, node_map, group_info)

        lines.append(
            f'push!(circuit, ("{t_name}_{uid}", {n1}, {n2}, {val}))'
        )

    # Repeated primitive groups.
    for prefix, info in group_info.items():
        repeat_count = info["repeat_count"]
        group_lines = []

        for inst in info["instances"]:
            uid = inst.get("uid", "UnknownUID")
            meta = hb_primitive_metadata(inst, simulation_dir, builtin_dir)

            if meta is None:
                continue

            t_name = meta["primitive_name"]

            if t_name == "K":
                raise ValueError(
                    f"Repeated HB primitive {uid!r} of type 'K' is not supported."
                )

            port_count = meta["port_count"]
            if port_count != 2:
                raise ValueError(
                    f"Repeated HB primitive {uid!r} of type {t_name!r} must have exactly 2 ports, "
                    f"got port_count={port_count}"
                )

            child_data = meta["child_data"]
            if child_data is not None:
                variables = child_data.get("variables", []) or []
                primary_key = variables[0].get("name") if variables else None
                val = get_parameter_value_from_instance_or_child(inst, child_data, primary_key, default="1.0")
            else:
                val = get_primary_value(inst, default="1.0")
            n1 = hb_node_expr_for_inst_port(uid, 1, node_map, group_info)
            n2 = hb_node_expr_for_inst_port(uid, 2, node_map, group_info)

            group_lines.append(
                f'    push!(circuit, ("{t_name}_{uid}_$(rep_idx)", {n1}, {n2}, {val}))'
            )

        lines.append(
            f"for rep_idx in 1:{repeat_count}\n"
            + "\n".join(group_lines)
            + "\nend"
        )

    return "\n".join(lines)

HB_NODEFLUX_CACHE_VERSION = "hb-cache-v2-port-order"


def nodeflux_csv_path(out_csv_path):
    return Path(f"{out_csv_path.with_suffix('')}_nodeflux.csv")


def generate_hbsolve_script(json_path, cache_dir, out_jl_path, out_csv_path, project_dir):
    data = load_json(json_path)
    validate_numeric_topology(data, json_path.name)
    builtin_dir = find_builtin_dir(Path(__file__).parent.resolve())
    simulation_dir = json_path.parent

    node_map, _ = build_nodal_netlist(data)

    # Keep the JSON compact, but generate repeated HB primitive groups as Julia
    # loops. This avoids massive copy-pasted circuit output while still giving
    # hbsolve a flat circuit vector at runtime.
    group_info = collect_hb_repeat_groups(data, node_map)
    first_free_node = max(node_map.values(), default=0) + 1
    repeat_helpers_jl, _ = generate_hb_repeat_node_helpers(group_info, first_free_node)
    circuit_push_jl = generate_hb_circuit_push_lines(
        data,
        node_map,
        group_info,
        simulation_dir,
        builtin_dir,
    )

    _sim_hb = (data.get("simulation") or {}).get("hb") or {}
    pump_freqs = _sim_hb.get("pump_frequencies", data.get("hb_pump_frequencies", [7.12])) or [7.12]
    pump_ports = _sim_hb.get("pump_ports", data.get("hb_pump_ports", [1])) or [1]
    pump_currents = _sim_hb.get("pump_currents", data.get("hb_pump_currents", [1.85e-6])) or [1.85e-6]
    dc_ports = _sim_hb.get("dc_ports", data.get("hb_dc_ports", [])) or []
    dc_currents = _sim_hb.get("dc_currents", data.get("hb_dc_currents", [])) or []

    n_pumps = max(1, len(pump_freqs))

    def _pad(lst, n, default):
        lst = list(lst) if lst else [default]
        while len(lst) < n:
            lst.append(lst[-1])
        return lst[:n]

    pump_ports = _pad(pump_ports, n_pumps, 1)
    pump_currents = _pad(pump_currents, n_pumps, "1.85e-6")
    mod_harmonics = _pad(_sim_hb.get("modulation_harmonics", data.get("hb_modulation_harmonics", [10])) or [10], n_pumps, 10)
    pump_harmonics_vals = _pad(_sim_hb.get("pump_harmonics", data.get("hb_pump_harmonics", [20])) or [20], n_pumps, 20)

    def _pump_mode(n, i):
        return "(" + ",".join("1" if j == i else "0" for j in range(n)) + ",)"

    def _dc_mode(n):
        return "(" + ",".join(["0"] * n) + ",)"

    wp_parts = ", ".join(f"2 * pi * {f} * 1e9" for f in pump_freqs)
    curr_lines = "\n".join(f"Ip_{i+1} = {c}" for i, c in enumerate(pump_currents))
    dc_curr_lines = "\n".join(f"Idc_{i+1} = {c}" for i, c in enumerate(dc_currents))

    source_entries = [
        f"(mode={_pump_mode(n_pumps, i)}, port={p}, current=Ip_{i+1})"
        for i, p in enumerate(pump_ports)
    ]
    for i, (dp, _) in enumerate(zip(dc_ports, dc_currents)):
        source_entries.append(f"(mode={_dc_mode(n_pumps)}, port={dp}, current=Idc_{i+1})")

    sources_line = "sources = [" + ", ".join(source_entries) + "]"
    nmod_tuple = "(" + ", ".join(str(int(h)) for h in mod_harmonics) + ",)"
    npump_tuple = "(" + ", ".join(str(int(h)) for h in pump_harmonics_vals) + ",)"
    signal_mode_jl = "(" + ",".join(["0"] * n_pumps) + ",)"
    dc_kwarg = "\n    dc=true," if dc_ports else ""

    f_start, f_stop, f_points = frequency_settings(data)
    port_numbers = extract_hbsolve_port_numbers(data)
    ports_jl = julia_int_tuple(port_numbers)
    sol_ports_jl = julia_port_tuple_array(extract_hbsolve_sol_ports(data, port_numbers))
    expected_ports_str = expected_ports_julia(project_dir, json_path)
    sim_vars_str = extract_sim_variables_julia(data)

    nodeflux_csv = nodeflux_csv_path(out_csv_path)

    jl_code = f"""using DelimitedFiles
using LinearAlgebra
using JosephsonCircuits

{sim_vars_str}

ws = 2 * pi * range({f_start}, {f_stop}, length={f_points}) * 1e9
wp = ({wp_parts},)
{curr_lines}
{dc_curr_lines}

{repeat_helpers_jl}

circuit = Any[]

{circuit_push_jl}

{sources_line}

println("Running hbsolve on {json_path.name}...")
rpm = hbsolve(
    ws,
    wp,
    sources,
    {nmod_tuple},
    {npump_tuple},
    circuit,
    Dict();
    threewavemixing={str(_sim_hb.get("threewave_mixing", data.get("hb_threewave_mixing", True))).lower()},
    fourwavemixing={str(_sim_hb.get("fourwave_mixing", data.get("hb_fourwave_mixing", True))).lower()},{dc_kwarg}
)

function save_hbsolve_result()
    num_freqs = length(ws)
    ports = {ports_jl}
    sol_ports = {sol_ports_jl}
    expected_ports = {expected_ports_str}
    num_ports = length(ports)

    S_matrix = zeros(ComplexF64, num_ports, num_ports, num_freqs)

    for out_idx in 1:num_ports
        for in_idx in 1:num_ports
            out_p = ports[out_idx]
            in_p = ports[in_idx]
            S_matrix[out_idx, in_idx, :] .= rpm.linearized.S({signal_mode_jl}, out_p, {signal_mode_jl}, in_p, :)
        end
    end

    S_matrix = apply_port_order(S_matrix, sol_ports, expected_ports)
    num_ports = size(S_matrix, 1)

    out_data = zeros(Float64, num_freqs, 1 + 2 * (num_ports^2))
    out_data[:, 1] = ws ./ (2*pi*1e9)

    col = 2
    for out_idx in 1:num_ports
        for in_idx in 1:num_ports
            out_data[:, col] = real.(S_matrix[out_idx, in_idx, :])
            out_data[:, col + 1] = imag.(S_matrix[out_idx, in_idx, :])
            col += 2
        end
    end

    writedlm("{out_csv_path.as_posix()}", out_data, ',')
end

function apply_port_order(S_k, sol_ports, expected_ports)
    if expected_ports === nothing
        return S_k
    end

    perm = Int[]
    missing = Tuple{{String, Int}}[]

    for ep in expected_ports
        idx = findfirst(p -> p == ep, sol_ports)
        if idx === nothing
            push!(missing, ep)
            continue
        end
        push!(perm, idx)
    end

    if !isempty(missing)
        println("[WARN] JSON exported port(s) not present as HB ports: ", missing)
        println("       HB ports: ", sol_ports)
    end

    if isempty(perm)
        error("None of the JSON expected ports were found in HB output ports $(sol_ports)")
    end

    return S_k[perm, perm, :]
end

function save_nodeflux_csv(filepath)
    modes = rpm.nonlinear.modes
    freqs = JosephsonCircuits.calcmodefreqs(rpm.nonlinear.w, rpm.nonlinear.modes) ./ (2*pi*1e9)
    values = vec(rpm.nonlinear.nodeflux)
    Nmodes = length(modes)
    Nmodes == 0 && error("Cannot save nodeflux: nonlinear modes are empty.")
    length(values) % Nmodes == 0 || error("Cannot save nodeflux: nodeflux length is not divisible by mode count.")
    Nnodes = length(values) ÷ Nmodes
    nodeflux_matrix = reshape(values, Nmodes, Nnodes)'
    sort_idx = sortperm(freqs)
    freq_plot = freqs[sort_idx]
    nodeflux_plot = nodeflux_matrix[:, sort_idx]
    modes_plot = modes[sort_idx]

    open(filepath, "w") do io
        println(io, "mode,frequency_GHz,node,real,imag")
        for node_idx in 1:Nnodes
            for mode_idx in 1:Nmodes
                value = nodeflux_plot[node_idx, mode_idx]
                println(io, string(first(modes_plot[mode_idx]), ",", freq_plot[mode_idx], ",", node_idx, ",", real(value), ",", imag(value)))
            end
        end
    end
end

save_hbsolve_result()
save_nodeflux_csv("{nodeflux_csv.as_posix()}")
println("Saved HBSolve cache to {out_csv_path.name}")
println("Saved nodeflux cache to {nodeflux_csv.name}")
"""

    jl_code = append_multimode_block_if_enabled(
        data=data,
        jl_code=jl_code,
        out_csv_path=out_csv_path,
        ports_jl=ports_jl,
    )

    with open(out_jl_path, "w") as f:
        f.write(jl_code)



def make_julia_network_expr_for_instance(inst, child_data, cache_dir, builtin_dir, simulation_dir):
    """
    Return a Julia expression that evaluates to the S-matrix for one concrete
    instance at frequency index k.

    This is used for both normal instances and repeated instances.
    """
    uid = inst["uid"]
    target_name = Path(inst["type_name"]).stem

    if child_data is not None and child_data.get("type") == "built-in" and child_data.get("subtype") == "sSolve":
        matrix_code = child_data.get("matrix_code")
        if not matrix_code:
            raise ValueError(f"Built-in sSolve component {target_name} has no matrix_code.")

        matrix_type = str(child_data.get("matrix_type", "S")).upper()
        param_assignments = variable_assignments_from_child_and_instance(inst, child_data)

        if matrix_type == "ABCD":
            matrix_expr = f"JosephsonCircuits.AtoS(ComplexF64.({matrix_code}))"
        elif matrix_type == "S":
            matrix_expr = matrix_code
        else:
            raise ValueError(
                f"Built-in sSolve component {target_name} has unsupported matrix_type={matrix_type!r}"
            )

        return f"""let
{param_assignments}
        ComplexF64.({matrix_expr})
    end"""

    if child_data is not None and child_data.get("type") == "matrix":
        matrix_values = str(child_data.get("matrix_values", "") or "").strip()
        matrix_definitions = str(child_data.get("matrix_definitions", "") or "").strip()
        matrix_type = str(child_data.get("matrix_type", "S")).upper()
        param_assignments = variable_assignments_from_child_and_instance(inst, child_data)

        def indent_block(text, spaces=8):
            pad = " " * spaces
            return "\n".join(pad + line for line in text.splitlines() if line.strip())

        defs_block = indent_block(matrix_definitions) if matrix_definitions else ""
        matrix_expr = f"JosephsonCircuits.AtoS(ComplexF64.({matrix_values}))" if matrix_type == "ABCD" else matrix_values
        inner = ""
        if param_assignments:
            inner += param_assignments + "\n"
        if defs_block:
            inner += defs_block + "\n"
        return f"""let
{inner}        ComplexF64.({matrix_expr})
    end"""

    if is_failed_standalone(cache_dir, target_name):
        raise RuntimeError(
            f"Child {target_name} previously failed standalone simulation; "
            f"cannot simulate parent instance {uid}."
        )

    csv_path = lookup_cached_csv(cache_dir, target_name)
    if csv_path is None:
        raise RuntimeError(
            f"Missing child simulation for {target_name} while building network "
            f"for instance {uid}. Inlining recovery has been removed."
        )

    return f'load_s_matrix_at("{csv_path.as_posix()}", k)'


def repeated_instance_network_entries_julia(inst, child_data, cache_dir, builtin_dir, simulation_dir):
    """
    Generate clean Julia network entries.

    repeat_count == 1:
        push!(networks, ("UID", S_expr))

    repeat_count > 1:
        for rep_idx in 1:N
            push!(networks, ("UID_$(rep_idx)", S_expr))
        end
    """
    uid = inst["uid"]
    repeat_count = int(inst.get("repeat_count", 1) or 1)

    s_expr = make_julia_network_expr_for_instance(
        inst,
        child_data,
        cache_dir,
        builtin_dir,
        simulation_dir,
    )

    if repeat_count <= 1:
        return f"""    push!(networks, ("{uid}", {s_expr}))"""

    return f"""    for rep_idx in 1:{repeat_count}
        push!(networks, ("{uid}_$(rep_idx)", {s_expr}))
    end"""


def endpoint_expr_for_instance_port(inst, port):
    """
    Map a schematic endpoint through explicit repeat_connections.

    For repeat_count > 1:
        repeat_connections out ports belong to the last repeated copy.
        repeat_connections in ports belong to the first repeated copy.
    """
    uid = inst["uid"]
    repeat_count = int(inst.get("repeat_count", 1) or 1)
    port = require_int_port(port, f"endpoint {uid}")

    if repeat_count <= 1:
        return f'("{uid}", {port})'

    out_ports, in_ports = repeat_boundary_ports(inst)

    if port in in_ports and port in out_ports:
        raise ValueError(
            f"Repeated instance {uid} uses port {port} as both a repeat input "
            "and repeat output, so the external boundary copy is ambiguous."
        )

    if port in in_ports:
        return f'("{uid}_1", {port})'

    if port in out_ports:
        return f'("{uid}_{repeat_count}", {port})'

    raise ValueError(
        f"Port {port} is not part of repeat_connections for repeated instance {uid}. "
        "External endpoints on repeated instances must use a declared repeat "
        "input or output port."
    )


def port_name_to_number(inst, name):
    port_names = inst.get("port_names", []) or []
    if name in port_names:
        return port_names.index(name) + 1

    if isinstance(name, str) and name.startswith("P_"):
        suffix = name[2:]
        if suffix.isdigit():
            port = int(suffix)
            port_count = int(inst.get("port_count", 0) or 0)
            if 1 <= port <= port_count:
                return port

    return None


def substitute_repeat_j(ref, j_value):
    ref = str(ref)
    if "j" not in ref:
        return ref

    match = re.fullmatch(r"(.*)j(?:([+-])(\d+))?", ref)
    if not match:
        raise ValueError(f"Unsupported repeat port expression {ref!r}")

    prefix, sign, amount = match.groups()
    offset = int(amount or 0)
    if sign == "-":
        offset *= -1

    return f"{prefix}{j_value + offset}"


def expand_repeat_connection_ref(inst, ref):
    if not isinstance(ref, str) or not ref:
        raise ValueError(
            f"Instance {inst.get('uid')}: repeat connection ports must be "
            f"non-empty strings, got {ref!r}"
        )

    exact_port = port_name_to_number(inst, ref)
    if exact_port is not None:
        return {None: exact_port}

    if "j" not in ref:
        raise ValueError(
            f"Instance {inst.get('uid')}: repeat connection port {ref!r} "
            f"does not match port_names={inst.get('port_names', [])}"
        )

    out = {}
    port_count = int(inst.get("port_count", 0) or 0)
    for j_value in range(1, port_count + 1):
        expanded = substitute_repeat_j(ref, j_value)
        port = port_name_to_number(inst, expanded)
        if port is not None:
            out[j_value] = port

    if not out:
        raise ValueError(
            f"Instance {inst.get('uid')}: repeat connection expression {ref!r} "
            f"did not resolve to any valid port in port_names={inst.get('port_names', [])}"
        )

    return out


def repeat_connection_pairs(inst):
    connections = inst.get("repeat_connections", []) or []
    if not isinstance(connections, list) or not connections:
        raise ValueError(
            f"Instance {inst.get('uid')}: repeated instances must define a "
            "non-empty repeat_connections list."
        )

    pairs = []
    for item in connections:
        if not isinstance(item, dict) or "out" not in item or "in" not in item:
            raise ValueError(
                f"Instance {inst.get('uid')}: each repeat_connections entry must "
                f"contain 'out' and 'in', got {item!r}"
            )

        out_map = expand_repeat_connection_ref(inst, item["out"])
        in_map = expand_repeat_connection_ref(inst, item["in"])

        if None in out_map and None in in_map:
            pairs.append((out_map[None], in_map[None]))
            continue

        if None in out_map or None in in_map:
            raise ValueError(
                f"Instance {inst.get('uid')}: repeat connection {item!r} mixes "
                "a j-dependent endpoint with a fixed endpoint."
            )

        for j_value in sorted(set(out_map) & set(in_map)):
            pairs.append((out_map[j_value], in_map[j_value]))

    unique_pairs = []
    seen = set()
    for pair in pairs:
        if pair in seen:
            continue
        seen.add(pair)
        unique_pairs.append(pair)

    if not unique_pairs:
        raise ValueError(
            f"Instance {inst.get('uid')}: repeat_connections did not produce "
            "any valid port pairs."
        )

    return unique_pairs


def repeat_boundary_ports(inst):
    pairs = repeat_connection_pairs(inst)
    out_ports = {out_port for out_port, _in_port in pairs}
    in_ports = {in_port for _out_port, in_port in pairs}
    return out_ports, in_ports


def repeated_instance_connections_julia(inst):
    """
    Internal cascade connections for repeat_count > 1:

        UID_i out_port -> UID_(i+1) in_port

    The out/in pairs come only from repeat_connections.
    """
    uid = inst["uid"]
    repeat_count = int(inst.get("repeat_count", 1) or 1)

    if repeat_count <= 1:
        return []

    pairs = repeat_connection_pairs(inst)

    lines = []
    for rep_idx in range(1, repeat_count):
        for out_port, in_port in pairs:
            lines.append(
                f'    push!(connections, [("{uid}_{rep_idx}", {out_port}), '
                f'("{uid}_{rep_idx + 1}", {in_port})])'
            )

    return lines



def generate_solves_script(
    json_path,
    cache_dir,
    out_jl_path,
    builtin_dir,
    out_csv_path,
    project_dir,
    simulation_dir,
):
    data = load_json(json_path)
    validate_numeric_topology(data, json_path.name)

    f_start, f_stop, f_points = frequency_settings(data)
    sim_vars_str = extract_sim_variables_julia(data)
    expected_ports_str = expected_ports_julia(project_dir, json_path)

    instances_by_uid = {
        inst["uid"]: inst
        for inst in data.get("instances", [])
        if "uid" in inst
    }

    network_entry_lines = []

    for inst in data.get("instances", []):
        if "uid" not in inst:
            continue

        target_name = Path(inst["type_name"]).stem
        child_json = resolve_component_json(target_name, simulation_dir, builtin_dir)
        child_data = load_json(child_json) if child_json and child_json.exists() else None

        network_entry_lines.append(
            repeated_instance_network_entries_julia(
                inst,
                child_data,
                cache_dir,
                builtin_dir,
                simulation_dir,
            )
        )

    connection_lines = []

    # Original schematic wires, mapped through repeated endpoints.
    for wire in data.get("wires", []):
        src_uid = wire["source_instance_uid"]
        tgt_uid = wire["target_instance_uid"]

        src_port = validate_port_on_instance(
            data,
            src_uid,
            wire.get("source_port"),
            f"{json_path.name} wire source {src_uid!r}",
        )
        tgt_port = validate_port_on_instance(
            data,
            tgt_uid,
            wire.get("target_port"),
            f"{json_path.name} wire target {tgt_uid!r}",
        )

        src_inst = instances_by_uid.get(src_uid, {})
        tgt_inst = instances_by_uid.get(tgt_uid, {})

        src_ep = endpoint_expr_for_instance_port(src_inst, src_port)
        tgt_ep = endpoint_expr_for_instance_port(tgt_inst, tgt_port)

        connection_lines.append(f"    push!(connections, [{src_ep}, {tgt_ep}])")

    # Internal cascade wires for repeated components.
    for inst in data.get("instances", []):
        connection_lines.extend(repeated_instance_connections_julia(inst))

    networks_builder_str = "\n".join(network_entry_lines)
    connections_builder_str = "\n".join(connection_lines)

    jl_code = f"""using DelimitedFiles
using LinearAlgebra
using JosephsonCircuits

{sim_vars_str}

ws = 2 * pi * range({f_start}, {f_stop}, length={f_points}) * 1e9

function load_s_matrix(filepath)
    in_data = readdlm(filepath, ',', Float64)
    num_freqs = size(in_data, 1)
    num_ports = Int(sqrt((size(in_data, 2) - 1) / 2))

    S = zeros(ComplexF64, num_ports, num_ports, num_freqs)

    col = 2
    for out_p in 1:num_ports
        for in_p in 1:num_ports
            S[out_p, in_p, :] = in_data[:, col] .+ im .* in_data[:, col + 1]
            col += 2
        end
    end

    return S
end

function load_s_matrix_at(filepath, k)
    S = load_s_matrix(filepath)
    return ComplexF64.(S[:, :, k])
end

function save_s_matrix(filepath, ws, S)
    num_freqs = length(ws)
    num_ports = size(S, 1)

    out_data = zeros(Float64, num_freqs, 1 + 2 * (num_ports^2))
    out_data[:, 1] = ws ./ (2*pi*1e9)

    col = 2
    for out_p in 1:num_ports
        for in_p in 1:num_ports
            out_data[:, col] = real.(S[out_p, in_p, :])
            out_data[:, col + 1] = imag.(S[out_p, in_p, :])
            col += 2
        end
    end

    writedlm(filepath, out_data, ',')
end

function extract_s_matrix(sol)
    if hasproperty(sol, :S)
        return sol.S
    elseif sol isa AbstractArray
        return sol
    else
        error("Cannot extract S-matrix from solveS result. Expected sol.S or array.")
    end
end

function solve_networks(networks, connections)
    if isempty(connections) && length(networks) == 1
        name, S = networks[1]
        ports = [(name, p) for p in 1:size(S, 1)]
        return reshape(S, size(S, 1), size(S, 2), 1), ports
    end

    sol = solveS(networks, connections)
    return extract_s_matrix(sol), sol.ports
end

function apply_port_order(S_k, sol_ports)
    expected_ports = {expected_ports_str}

    if expected_ports === nothing
        return S_k
    end

    perm = Int[]
    missing = Tuple{{String, Int}}[]

    for ep in expected_ports
        idx = findfirst(p -> p == ep, sol_ports)
        if idx === nothing
            push!(missing, ep)
            continue
        end
        push!(perm, idx)
    end

    if !isempty(missing)
        println("[WARN] JSON exported port(s) not present as external solveS ports: ", missing)
        println("       solveS external ports: ", sol_ports)
    end

    if isempty(perm)
        error("None of the JSON expected ports were found in solveS output ports $(sol_ports)")
    end

    return S_k[perm, perm, :]
end

function build_networks(w, k)
    networks = Tuple{{String, Matrix{{ComplexF64}}}}[]

{networks_builder_str}

    return networks
end

function build_connections()
    connections = Vector{{Vector{{Tuple{{String, Int}}}}}}()

{connections_builder_str}

    return connections
end

connections = build_connections()

println("Running solveS on {json_path.name}...")

S_first = nothing

for k in eachindex(ws)
    w = ws[k]
    networks = build_networks(w, k)
    S_k, sol_ports = solve_networks(networks, connections)
    S_k = apply_port_order(S_k, sol_ports)
    S_k = ComplexF64.(S_k)

    if S_first === nothing
        global S_first = zeros(ComplexF64, size(S_k, 1), size(S_k, 2), length(ws))
    end

    S_first[:, :, k] .= S_k[:, :, 1]
end

save_s_matrix("{out_csv_path.as_posix()}", ws, S_first)
println("Saved solveS cache to {out_csv_path.name}")
"""

    with open(out_jl_path, "w") as f:
        f.write(jl_code)

def build_dependency_graph(simulation_dir):
    """
    Dependency graph over actual JSON files in specialized/.

    This includes generated specialized files, which are not present in the
    original classification memo.
    """
    graph = {}

    for target_json in simulation_dir.glob("*.json"):
        # Skip metadata JSONs produced by netlist stage.
        if target_json.name.endswith("_json_port_order.json"):
            continue

        data = load_json(target_json)
        deps = set()

        for inst in data.get("instances", []):
            t_name = inst.get("type_name")
            if not t_name:
                continue

            dep_path = simulation_dir / (Path(t_name).stem + ".json")
            if dep_path.exists():
                deps.add(str(dep_path))

        graph[str(target_json)] = deps

    return graph


def dependency_levels(graph):
    """
    Return dependency-ready levels from a graph shaped as node -> dependencies.

    Each level can run in parallel because all dependencies are either in earlier
    levels or absent from the graph.
    """
    sorter = TopologicalSorter(graph)
    sorter.prepare()

    levels = []
    while sorter.is_active():
        ready = list(sorter.get_ready())
        if ready:
            levels.append(ready)
            sorter.done(*ready)

    return levels


def julia_string(value):
    return json.dumps(str(value))


def cache_key_with_dependencies(target_file, self_cache_key, graph, cache_dir):
    manifest = load_cache_manifest(cache_dir)
    dependency_keys = []

    for dep_str in sorted(graph.get(str(target_file), [])):
        dep_path = Path(dep_str)
        dep_cell_name = dep_path.stem
        dep_entry = manifest.get(dep_cell_name, {})
        dependency_keys.append(
            {
                "cell": dep_cell_name,
                "cache_key": dep_entry.get("cache_key"),
            }
        )

    return stable_json_hash(
        {
            "self": self_cache_key,
            "dependencies": dependency_keys,
        }
    )


def generate_batch_julia_script(batch_script, jobs):
    script_paths = [job["jl_script"].as_posix() for job in jobs]
    scripts_jl = ",\n    ".join(julia_string(path) for path in script_paths)

    jl_code = f"""using Base.Threads
using DelimitedFiles
using LinearAlgebra
using JosephsonCircuits

const SIMULATION_SCRIPTS = String[
    {scripts_jl}
]

function include_isolated(path::String, idx::Int)
    module_name = Symbol("SimulationBatch_", idx)
    mod = Module(module_name)
    Core.eval(mod, :(using DelimitedFiles))
    Core.eval(mod, :(using LinearAlgebra))
    Core.eval(mod, :(using JosephsonCircuits))
    println("[BATCH] starting $(basename(path))")
    Base.include(mod, path)
    println("[BATCH] finished $(basename(path))")
    return nothing
end

tasks = Task[]

for (idx, path) in enumerate(SIMULATION_SCRIPTS)
    push!(tasks, Threads.@spawn include_isolated(path, idx))
end

for task in tasks
    fetch(task)
end
"""

    with open(batch_script, "w") as f:
        f.write(jl_code)


def run_julia_script(job, cache_dir):
    node_class = job["node_class"]
    jl_script = job["jl_script"]
    cell_name = job["cell_name"]

    if node_class in ["hbsolve_block", "hbsolve_primitive"]:
        subprocess.run(["julia", str(jl_script)], check=True)
        return

    try:
        subprocess.run(
            ["julia", str(jl_script)],
            check=True,
            capture_output=True,
            text=True,
        )

    except subprocess.CalledProcessError as err:
        if is_singular_solve_error(err):
            mark_failed_standalone(cache_dir, cell_name)

            raise RuntimeError(
                f"{job['target_file'].name} failed standalone solveS with a singular "
                "matrix error. Inlining recovery has been removed, so this "
                "cell cannot be used as a child simulation."
            ) from err

        print(err.stdout or "")
        print(err.stderr or "")
        raise


def run_julia_batch(jobs, cache_dir, batch_script):
    if len(jobs) == 1:
        run_julia_script(jobs[0], cache_dir)
        return

    generate_batch_julia_script(batch_script, jobs)
    print(f"[BATCH] Running {len(jobs)} independent simulations in one Julia session...")

    try:
        subprocess.run(
            ["julia", "--threads=auto", str(batch_script)],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as err:
        print(err.stdout or "")
        print(err.stderr or "")
        print("[BATCH] Batch failed; rerunning this level one-by-one for precise errors.")
        for job in jobs:
            run_julia_script(job, cache_dir)
        return


def prepare_simulation_job(
    target_file,
    node_class,
    cache_key,
    cell_name,
    cache_dir,
    builtin_dir,
    project_dir,
    simulation_dir,
):
    csv_cache = cache_dir / f"{cell_name}_{cache_key}.csv"
    jl_script = cache_dir / f"run_{cell_name}_{cache_key}.jl"

    if node_class in ["hbsolve_block", "hbsolve_primitive"]:
        generate_hbsolve_script(target_file, cache_dir, jl_script, csv_cache, project_dir)

    elif node_class in ["sparam_block", "sparam_primitive"]:
        target_data = load_json(target_file)

        if target_data.get("generated_from") == "julia_direct_s":
            generate_direct_s_script(target_file, cache_dir, jl_script, csv_cache)
        elif target_data.get("type") == "built-in" and target_data.get("subtype") == "sSolve":
            generate_builtin_sparam_script(target_file, jl_script, csv_cache)
        else:
            if target_data.get("type") != "built-in":
                validate_numeric_topology(target_data, target_file.name)
            generate_solves_script(
                target_file,
                cache_dir,
                jl_script,
                builtin_dir,
                csv_cache,
                project_dir,
                simulation_dir,
            )

    elif node_class == "mixture":
        raise ValueError(f"{target_file.name} is a mixture; simulation is not allowed yet.")

    else:
        print(f"[SKIP] Unknown class for {target_file.name}: {node_class}")
        return None

    return {
        "target_file": target_file,
        "node_class": node_class,
        "cache_key": cache_key,
        "cell_name": cell_name,
        "csv_cache": csv_cache,
        "jl_script": jl_script,
    }


def orchestrate_simulation(target, script_dir):
    target_path = Path(target)
    project_name = target_path.parent.name if target_path.parent.name else "default_project"

    project_dir = script_dir / "outputs" / project_name
    simulation_dir = project_dir / "specialized"
    cache_dir = project_dir / "cache"
    builtin_dir = script_dir / "built-in"

    cache_dir.mkdir(parents=True, exist_ok=True)

    memo_path = project_dir / "classification_memo.json"
    if not memo_path.exists():
        raise RuntimeError("Classification memo not found. Run classifier first.")

    if not simulation_dir.exists():
        raise RuntimeError(f"Specialized directory not found: {simulation_dir}. Run specialization stage first.")

    with open(memo_path, "r") as f:
        memo = json.load(f)

    graph = build_dependency_graph(simulation_dir)

    if not graph:
        raise RuntimeError(f"No simulation JSON files found in {simulation_dir}")

    prune_cache_manifest(
        cache_dir,
        {
            Path(file_str).stem
            for file_str in graph
        },
    )

    levels = dependency_levels(graph)

    print("==================================================")
    print(" Running Simulation Orchestrator                  ")
    print("==================================================")
    print(f" Simulation input: {simulation_dir}")
    print("==================================================\n")

    for level_idx, level in enumerate(levels, start=1):
        jobs = []

        for file_str in level:
            target_file = Path(file_str)
            node_class = classify_json_for_simulation(target_file, memo)

            # Built-in HB primitives are circuit elements, not standalone HB
            # testbenches. They do not carry P blocks themselves; the enclosing
            # flattened HB schematic provides those ports.
            if node_class == "hbsolve_primitive":
                print(f"[SKIP] {target_file.name} is an HB primitive; it is simulated as part of its parent HB block.")
                continue

            raw_cache_key = cache_key_for_json(target_file)
            if node_class in ["hbsolve_block", "hbsolve_primitive"]:
                self_cache_key = stable_json_hash(
                    {
                        "json": raw_cache_key,
                        "hb_nodeflux_cache_version": HB_NODEFLUX_CACHE_VERSION,
                    }
                )
            else:
                self_cache_key = raw_cache_key
            cache_key = cache_key_with_dependencies(
                target_file,
                self_cache_key,
                graph,
                cache_dir,
            )
            cell_name = target_file.stem

            cached_csv = lookup_cached_csv(cache_dir, cell_name)
            if cached_csv is not None:
                manifest = load_cache_manifest(cache_dir)
                entry = manifest.get(cell_name, {})
                if entry.get("cache_key") == cache_key:
                    print(f"[CACHE HIT] {target_file.name} -> {cached_csv.name}")
                    continue

            print(f"[SIMULATE] Queued {target_file.name} ({node_class})...")
            job = prepare_simulation_job(
                target_file,
                node_class,
                cache_key,
                cell_name,
                cache_dir,
                builtin_dir,
                project_dir,
                simulation_dir,
            )
            if job is not None:
                jobs.append(job)

        if not jobs:
            continue

        batch_script = cache_dir / f"run_batch_level_{level_idx:03d}.jl"
        run_julia_batch(jobs, cache_dir, batch_script)

        for job in jobs:
            update_cache_manifest(
                cache_dir,
                job["cell_name"],
                job["cache_key"],
                job["csv_cache"],
            )

    print("\nSimulation pipeline complete.")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Run simulation for a single target JSON file."
    )
    parser.add_argument(
        "target",
        help="Target JSON file, e.g. example_full/first_half.json"
    )

    args = parser.parse_args()

    current_dir = Path(__file__).parent.resolve() if "__file__" in globals() else Path.cwd().resolve()

    orchestrate_simulation(args.target, current_dir)
