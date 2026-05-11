import json
import shutil
from pathlib import Path

from path_utils import resolve_source_target


HB_FREQ_KEYS = [
    "simulation_freq_start",
    "simulation_freq_stop",
    "simulation_freq_points",
    "simulation_sweep_type",
]

HB_SETTING_KEYS = [
    "z0",
    "simulation_input_ports",
    "simulation_output_ports",
    "simulation_figure_title",
    "hb_dc_ports",
    "hb_dc_currents",
    "hb_pump_frequencies",
    "hb_pump_ports",
    "hb_pump_currents",
    "hb_modulation_harmonics",
    "hb_pump_harmonics",
    "hb_threewave_mixing",
    "hb_fourwave_mixing",
    "hb_mode_analysis_enabled",
    "simulation_variables",
]

SPARAM_SETTING_KEYS = [
    "z0",
    "simulation_input_ports",
    "simulation_output_ports",
    "simulation_freq_start",
    "simulation_freq_stop",
    "simulation_freq_points",
    "simulation_sweep_type",
    "simulation_figure_title",
    "simulation_variables",
]


def get_project_output_dir(script_dir, project_name):
    out_dir = script_dir / "outputs" / project_name
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def get_resolved_dir(script_dir, project_name):
    return script_dir / "outputs" / project_name / "resolved_variables"


def get_merge_output_dir(script_dir, project_name):
    merge_dir = script_dir / "outputs" / project_name / "merged"
    merge_dir.mkdir(parents=True, exist_ok=True)
    return merge_dir


def get_stage_input_path(original_file_path, resolved_dir):
    """
    Every merge must start from the variable-propagation output.

    The merger must not fall back to source project JSON for project cells,
    because those files may still contain unresolved parameters and unresolved
    hierarchical references. Run variable_propagation.py first so the target and
    all required child cells are copied into outputs/<project>/resolved_variables.
    """
    resolved_path = resolved_dir / original_file_path.name
    if resolved_path.exists():
        return resolved_path.resolve()

    raise FileNotFoundError(
        f"Missing resolved input for merger: {resolved_path}. "
        "Run variable_propagation.py before running merger.py."
    )


def get_memo_class(file_path, memo):
    file_path = Path(file_path).resolve()

    direct = memo.get(str(file_path))
    if direct:
        return direct.get("class", "unknown")

    matches = [v for k, v in memo.items() if Path(k).name == file_path.name]
    if matches:
        return matches[0].get("class", "unknown")

    return "unknown"


def resolve_builtin_component(cell_name, builtin_dir):
    """Resolve only built-in primitive/block definitions from logic/built-in."""
    if not builtin_dir.exists() or not builtin_dir.is_dir():
        return None

    raw = Path(str(cell_name).replace("\\", "/"))
    target_name = raw.name if raw.name.endswith(".json") else f"{raw.name}.json"

    direct = builtin_dir / raw
    if direct.suffix != ".json":
        direct = direct.with_suffix(".json")
    if direct.is_file():
        return direct.resolve()

    for path in builtin_dir.rglob(target_name):
        if path.is_file():
            return path.resolve()

    return None


def _is_within(path, parent):
    try:
        Path(path).resolve().relative_to(Path(parent).resolve())
        return True
    except ValueError:
        return False


def resolve_component(cell_name, current_dir, builtin_dir, script_dir, resolved_dir=None):
    """
    Resolve project components from outputs/<project>/resolved_variables only.

    variable_propagation.py copies every non-built-in dependency into the
    resolved_variables tree. The merger therefore resolves project cells only
    from that tree and never falls back to source data/project folders.

    Resolution order:
      1. current_dir/<cell>.json, but only when current_dir is inside
         resolved_variables. This handles local children such as ring_module
         from resolved_variables/example_add_drop/add_drop.json.
      2. resolved_variables/<qualified/path>.json for references such as
         example_add_drop/add_drop.
      3. resolved_variables/<filename>.json for legacy flat outputs.
      4. logic/built-in only.
    """
    raw = Path(str(cell_name).replace("\\", "/"))
    if raw.suffix != ".json":
        raw = raw.with_suffix(".json")

    if resolved_dir is not None:
        resolved_base = Path(resolved_dir).resolve()
        current_base = Path(current_dir).resolve()

        # Local sibling lookup, but only within the resolved_variables tree.
        # This is required for child refs like "ring_module" inside
        # resolved_variables/example_add_drop/add_drop.json.
        if _is_within(current_base, resolved_base):
            local_path = current_base / raw.name
            if local_path.is_file():
                return local_path.resolve()

            # Also allow relative subpaths from the current resolved folder.
            local_qualified_path = current_base / raw
            if local_qualified_path.is_file():
                return local_qualified_path.resolve()

        # Qualified lookup from the root of resolved_variables.
        resolved_path = resolved_base / raw
        if resolved_path.is_file():
            return resolved_path.resolve()

        # Legacy fallback for older resolved_variables directories that were flat.
        flat_path = resolved_base / raw.name
        if flat_path.is_file():
            return flat_path.resolve()

    builtin_path = resolve_builtin_component(cell_name, builtin_dir)
    if builtin_path is not None:
        return builtin_path

    return None

def extract_settings(data, keys):
    return {key: data[key] for key in keys if key in data}


def require_int_port(value, context):
    if not isinstance(value, int):
        raise ValueError(
            f"{context}: expected numeric integer port after variable propagation, "
            f"got {value!r} ({type(value).__name__})"
        )
    return value


def require_named_port(value, context):
    if not isinstance(value, str) or not value:
        raise ValueError(
            f"{context}: expected named string port before port_resolution, "
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


class UnionFind:
    def __init__(self):
        self.parent = {}

    def find(self, item):
        if self.parent.setdefault(item, item) == item:
            return item

        self.parent[item] = self.find(self.parent[item])
        return self.parent[item]

    def union(self, a, b):
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


def endpoint_key(uid, port):
    return f"{uid}:{port}"


def build_flat_port_union_find(data):
    """
    Build electrical nets for an already-flattened named-port circuit.

    This is used by the merger only after hierarchy has been flattened. The
    merger does not infer hierarchy here; it only canonicalizes exposed HB pins
    to a P-block endpoint on the same electrical net when such an endpoint
    exists.
    """
    uf = UnionFind()

    for inst in data.get("instances", []):
        uid = inst.get("uid")
        if not uid:
            continue

        port_names = inst.get("port_names", []) or []
        if not port_names:
            port_count = int(inst.get("port_count", 0))
            port_names = [f"p{port}" for port in range(1, port_count + 1)]
        for port in port_names:
            uf.find(endpoint_key(uid, port))

    label_groups = {}

    for wire in data.get("wires", []):
        src_uid = wire.get("source_instance_uid")
        tgt_uid = wire.get("target_instance_uid")

        src_port = require_named_port(
            wire.get("source_port"),
            f"{data.get('name', '<flat>')} wire source {src_uid!r}",
        )
        tgt_port = require_named_port(
            wire.get("target_port"),
            f"{data.get('name', '<flat>')} wire target {tgt_uid!r}",
        )

        uf.union(endpoint_key(src_uid, src_port), endpoint_key(tgt_uid, tgt_port))



    for label in data.get("labels", []):
        uid = label.get("instance_uid")
        name = str(label.get("name", ""))

        port = require_named_port(
            label.get("port"),
            f"{data.get('name', '<flat>')} label {name!r} on {uid!r}",
        )

        key = endpoint_key(uid, port)

        if name in {"0", "P_0", "GND", "gnd", "ground"}:
            uf.union(key, "0")
        elif name:
            label_groups.setdefault(name, []).append(key)

    for _, keys in label_groups.items():
        if len(keys) < 2:
            continue

        first = keys[0]
        for other in keys[1:]:
            uf.union(first, other)

    return uf


def canonicalize_hb_exposed_pins_to_p_endpoints(data):
    """
    For flattened HB circuits, rewrite each exposed top-level pin to the P-block
    endpoint on the same electrical net.

    Why:
        A hierarchical exported pin can legitimately resolve to any primitive
        endpoint on the exported net. For HB simulation/validation, however, the
        exposed simulation pin must be associated with the JosephsonCircuits P
        block on that same net. This keeps hierarchy resolution in the merger,
        while downstream stages continue to consume numeric flattened topology.

    Strict behavior:
        If a requested simulation input/output pin does not share a net with
        exactly one P-block port, fail here instead of allowing the validator to
        fall back to the wrong P block.
    """
    uf = build_flat_port_union_find(data)

    root_to_p_endpoints = {}

    for inst in data.get("instances", []):
        if inst.get("type_name") != "P":
            continue

        uid = inst.get("uid")
        if not uid:
            continue

        port_names = inst.get("port_names", []) or []
        if not port_names:
            port_count = int(inst.get("port_count", 0))
            port_names = [f"p{port}" for port in range(1, port_count + 1)]
        for local_port in port_names:
            key = endpoint_key(uid, local_port)
            root = uf.find(key)
            root_to_p_endpoints.setdefault(root, []).append((uid, local_port))

    requested_pin_names = set(data.get("simulation_input_ports", []) or [])
    requested_pin_names.update(data.get("simulation_output_ports", []) or [])

    for pin in data.get("pins", []):
        pin_name = pin.get("name")
        uid = pin.get("instance_uid")

        if not pin_name or not uid:
            raise ValueError(f"{data.get('name', '<flat>')}: invalid exposed pin {pin}")

        port = require_named_port(
            pin.get("port"),
            f"{data.get('name', '<flat>')} exposed pin {pin_name!r}",
        )

        root = uf.find(endpoint_key(uid, port))
        matches = root_to_p_endpoints.get(root, [])

        unique_matches = sorted(set(matches))

        if (uid, port) in unique_matches:
            continue

        if len(unique_matches) == 1:
            p_uid, p_port = unique_matches[0]
            pin["instance_uid"] = p_uid
            pin["port"] = p_port
            continue

        if pin_name in requested_pin_names:
            if not unique_matches:
                raise ValueError(
                    f"{data.get('name', '<flat>')}: requested simulation pin "
                    f"{pin_name!r} resolves to {uid}:{port}, but that net does "
                    "not contain any P block endpoint."
                )

            raise ValueError(
                f"{data.get('name', '<flat>')}: requested simulation pin "
                f"{pin_name!r} resolves to {uid}:{port}, but that net contains "
                f"multiple P block endpoints: {unique_matches}"
            )

    return data


def primary_parameter_value(inst, default=None):
    order = inst.get("parameter_order", []) or []
    if not order:
        return default

    key = order[0]
    params = inst.get("parameters", {}) or {}

    if key in params and params[key] not in ("", None):
        return params[key]
    return default


def p_block_field_number(inst):
    value = primary_parameter_value(inst)
    if value is None:
        raise ValueError(f"P block {inst.get('uid')!r} has no field/port number")

    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"P block {inst.get('uid')!r} has non-integer field/port number {value!r}"
        ) from exc


def annotate_x_fields_from_exposed_pins(data):
    """
    Regular HB merge is also used for X simulations when the top-level JSON is
    already a single hb_top_block. In that branch x_merge_simulation.py does not
    run, so add the same label -> P-field annotations here.
    """
    if str(data.get("x-params", "")).lower() != "true":
        return data

    instances_by_uid = {
        inst.get("uid"): inst
        for inst in data.get("instances", [])
        if inst.get("uid")
    }

    pin_to_field = {}
    for pin in data.get("pins", []) or []:
        name = pin.get("name")
        uid = pin.get("instance_uid")
        inst = instances_by_uid.get(uid)

        if not name or not inst or inst.get("type_name") != "P":
            continue

        pin_to_field[str(name)] = p_block_field_number(inst)

    def labels_to_fields(source_key, target_key, required=False):
        labels = data.get(source_key, []) or []
        if required and not labels:
            raise ValueError(f"Missing required X setting {source_key}")
        if not isinstance(labels, list):
            raise ValueError(f"{source_key} must be a list of pin-label strings")

        fields = []
        for label in labels:
            if not isinstance(label, str):
                raise ValueError(f"{source_key} must contain pin-label strings, got {label!r}")
            if label not in pin_to_field:
                raise ValueError(
                    f"{source_key} label {label!r} does not correspond to an exposed P block. "
                    f"Available exposed P labels: {sorted(pin_to_field)}"
                )
            fields.append(pin_to_field[label])

        data[target_key] = fields

    labels_to_fields("x_pump_ports", "x_pump_fields", required=True)
    labels_to_fields("x_dc_ports", "x_dc_fields", required=False)
    labels_to_fields("x_input_ports", "x_input_fields", required=True)
    labels_to_fields("x_output_ports", "x_out_fields", required=True)

    if data.get("x_input_fields"):
        data["hb_input_field"] = int(data["x_input_fields"][0])
        data["hb_input_pin_name"] = data.get("x_input_ports", [None])[0]

    if data.get("x_out_fields"):
        data["hb_output_field"] = int(data["x_out_fields"][0])
        data["hb_output_pin_name"] = data.get("x_output_ports", [None])[0]

    return data


def build_child_pin_map_by_name(sub_data, child_name):
    child_pins_by_name = {}

    for pin in sub_data.get("pins", []):
        pin_name = pin.get("name")
        if not isinstance(pin_name, str) or not pin_name:
            raise ValueError(f"{child_name}: child exported pin has invalid name: {pin}")

        if pin_name in child_pins_by_name:
            raise ValueError(f"{child_name}: duplicate exported pin name {pin_name!r}")

        require_named_port(pin.get("port"), f"{child_name} exported pin {pin_name!r}")
        child_pins_by_name[pin_name] = pin

    return child_pins_by_name


def build_aliases_by_child_pin_index(
    *,
    parent_data,
    inst,
    current_uid,
    sub_data,
    sub_prefix,
    inst_file,
):
    """
    Resolve a parent's named port through the child pins[].

    Important:
        parent named port P_out
        -> child pin named P_out
        -> child pin's named internal endpoint port
    """
    parent_context = parent_data.get("name", "<unknown>")
    type_name = inst.get("type_name")

    port_count = int(inst.get("port_count", 0))
    child_pins = sub_data.get("pins", []) or []

    if len(child_pins) != port_count:
        raise ValueError(
            f"{parent_context}: instance {current_uid!r} of type {type_name!r} "
            f"has port_count={port_count}, but child {inst_file.name} exports "
            f"{len(child_pins)} pins."
        )

    aliases = {}

    for external_port, sub_pin in enumerate(child_pins, start=1):
        pin_name = sub_pin.get("name")
        if not isinstance(pin_name, str) or not pin_name:
            raise ValueError(
                f"{inst_file.name}: exported pin #{external_port} has invalid "
                f"name in {sub_pin}"
            )

        aliases[(current_uid, pin_name)] = (
            f"{sub_prefix}{sub_pin['instance_uid']}",
            require_named_port(
                sub_pin["port"],
                f"{inst_file.name} exported pin {pin_name!r}",
            ),
        )

    return aliases


def is_hbsolve_tree(file_path, builtin_dir, script_dir, memo, resolved_dir=None, active_stack=None, cache=None):
    if active_stack is None:
        active_stack = set()
    if cache is None:
        cache = {}

    file_path = Path(file_path).resolve()

    if file_path in cache:
        return cache[file_path]

    if file_path in active_stack:
        raise ValueError(f"Recursive dependency detected at {file_path}")

    active_stack.add(file_path)

    node_class = get_memo_class(file_path, memo)

    if node_class == "hbsolve_primitive":
        cache[file_path] = True
        active_stack.remove(file_path)
        return True

    if node_class != "hbsolve_block":
        cache[file_path] = False
        active_stack.remove(file_path)
        return False

    with open(file_path, "r") as f:
        data = json.load(f)

    for inst in data.get("instances", []):
        type_name = inst.get("type_name")
        if not type_name:
            continue

        inst_file = resolve_component(
            type_name,
            file_path.parent,
            builtin_dir,
            script_dir,
            resolved_dir,
        )

        if not inst_file:
            raise FileNotFoundError(
                f"Could not resolve component {type_name!r} in {file_path.name}"
            )

        child_class = get_memo_class(inst_file, memo)

        if child_class not in ["hbsolve_block", "hbsolve_primitive"]:
            cache[file_path] = False
            active_stack.remove(file_path)
            return False

        if child_class == "hbsolve_block":
            if not is_hbsolve_tree(
                inst_file,
                builtin_dir,
                script_dir,
                memo,
                resolved_dir,
                active_stack,
                cache,
            ):
                cache[file_path] = False
                active_stack.remove(file_path)
                return False

    cache[file_path] = True
    active_stack.remove(file_path)
    return True


def is_p_block_instance(inst):
    return inst.get("type_name") == "P"


def subtree_contains_p_block(
    file_path,
    builtin_dir,
    script_dir,
    memo,
    resolved_dir=None,
    seen=None,
):
    if seen is None:
        seen = set()

    file_path = Path(file_path).resolve()

    if file_path in seen:
        return False

    seen.add(file_path)

    with open(file_path, "r") as f:
        data = json.load(f)

    for inst in data.get("instances", []):
        if is_p_block_instance(inst):
            return True

        type_name = inst.get("type_name")
        if not type_name:
            continue

        inst_file = resolve_component(
            type_name,
            file_path.parent,
            builtin_dir,
            script_dir,
            resolved_dir,
        )

        if not inst_file:
            continue

        child_class = get_memo_class(inst_file, memo)

        if child_class in ["sparam_block", "hbsolve_block"]:
            if subtree_contains_p_block(
                inst_file,
                builtin_dir,
                script_dir,
                memo,
                resolved_dir,
                seen,
            ):
                return True

    return False


def validate_repeat_usage(
    data,
    current_dir,
    builtin_dir,
    script_dir,
    memo,
    resolved_dir=None,
    context_name="<unknown>",
):
    for inst in data.get("instances", []):
        repeat_count = int(inst.get("repeat_count", 1) or 1)

        if repeat_count <= 1:
            continue

        uid = inst.get("uid", "<unknown>")
        type_name = inst.get("type_name")
        port_count = int(inst.get("port_count", 0) or 0)

        if port_count <= 0:
            raise ValueError(
                f"Invalid repeat_count={repeat_count} on {context_name}:{uid}. "
                "Repeated components require a positive port_count."
            )

        if not inst.get("repeat_connections"):
            raise ValueError(
                f"Invalid repeat_count={repeat_count} on {context_name}:{uid}. "
                "Repeated components must define repeat_connections explicitly."
            )

        expand_repeat_connection_pairs(inst)

        inst_file = resolve_component(
            type_name,
            current_dir,
            builtin_dir,
            script_dir,
            resolved_dir,
        )

        if inst_file and subtree_contains_p_block(
            inst_file,
            builtin_dir,
            script_dir,
            memo,
            resolved_dir,
        ):
            raise ValueError(
                f"Invalid repeat_count={repeat_count} on {context_name}:{uid} "
                f"({type_name}). Repeated components may not contain P blocks."
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

    import re

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
            f"Instance {inst.get('uid')}: repeat connection ports must be non-empty strings, "
            f"got {ref!r}"
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


def expand_repeat_connection_pairs(inst):
    """
    Expand explicit repeat_connections into numeric (out_port, in_port) pairs.

    Examples:
        {"out": "P_j", "in": "P_j+5"} expands over every j where both names
        resolve against the instance port_names/port_count.

        {"out": "P_1", "in": "P_2"} expands to exactly one pair.
    """
    connections = inst.get("repeat_connections", []) or []
    if not isinstance(connections, list):
        raise ValueError(
            f"Instance {inst.get('uid')}: repeat_connections must be a list, "
            f"got {type(connections).__name__}"
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
                f"Instance {inst.get('uid')}: repeat connection {item!r} mixes a "
                "j-dependent endpoint with a fixed endpoint. Use either two fixed "
                "ports or two j-dependent expressions."
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
            f"Instance {inst.get('uid')}: repeat_connections did not produce any "
            "valid port pairs."
        )

    return unique_pairs


def flatten_circuit_data(
    data,
    current_dir,
    builtin_dir,
    script_dir,
    memo,
    uid_prefix="",
    resolved_dir=None,
):
    flat_instances = []
    flat_wires = []
    flat_pins = []
    flat_labels = []
    aliases = {}

    is_top_level = str(data.get("hb_top_block", "")).lower() == "true"

    validate_repeat_usage(
        data,
        current_dir,
        builtin_dir,
        script_dir,
        memo,
        resolved_dir,
        context_name=data.get("name", "<unknown>"),
    )

    for wire in data.get("wires", []):
        new_wire = dict(wire)

        require_named_port(new_wire.get("source_port"), f"{data.get('name', '<unknown>')} wire source")
        require_named_port(new_wire.get("target_port"), f"{data.get('name', '<unknown>')} wire target")

        if "source_instance_uid" in new_wire:
            new_wire["source_instance_uid"] = f"{uid_prefix}{new_wire['source_instance_uid']}"
        if "target_instance_uid" in new_wire:
            new_wire["target_instance_uid"] = f"{uid_prefix}{new_wire['target_instance_uid']}"

        flat_wires.append(new_wire)

    for label in data.get("labels", []):
        new_label = dict(label)

        # Labels may have a direct "port" field (old format) or derive port from
        # net_source_port (new format where labels anchor to nets). Use whichever
        # is present.
        label_port = new_label.get("port") or new_label.get("net_source_port")
        require_named_port(label_port, f"{data.get('name', '<unknown>')} label")
        if not new_label.get("port"):
            new_label["port"] = label_port

        if "instance_uid" in new_label:
            new_label["instance_uid"] = f"{uid_prefix}{new_label['instance_uid']}"
        if "net_source_uid" in new_label:
            new_label["net_source_uid"] = f"{uid_prefix}{new_label['net_source_uid']}"

        if new_label.get("name") in ["P_0", "0", "GND", "gnd"]:
            new_label["name"] = "0"

        flat_labels.append(new_label)

    for pin in data.get("pins", []):
        new_pin = dict(pin)

        require_named_port(
            new_pin.get("port"),
            f"{data.get('name', '<unknown>')} pin {new_pin.get('name')!r}",
        )

        if "instance_uid" in new_pin:
            new_pin["instance_uid"] = f"{uid_prefix}{new_pin['instance_uid']}"

        if is_top_level:
            flat_pins.append(new_pin)
        else:
            pin_name = str(new_pin.get("name", ""))

            if pin_name in ["P_0", "0", "GND", "gnd"]:
                new_pin["name"] = "0"
            else:
                new_pin["name"] = f"{uid_prefix}{pin_name}"

            flat_labels.append(new_pin)

    for idx, inst in enumerate(data.get("instances", [])):
        type_name = inst.get("type_name")
        if not type_name:
            continue

        inst_file = resolve_component(
            type_name,
            current_dir,
            builtin_dir,
            script_dir,
            resolved_dir,
        )

        node_class = get_memo_class(inst_file, memo) if inst_file else "unknown"

        repeat_count = int(inst.get("repeat_count", 1) or 1)
        base_uid = inst.get("uid", f"I{idx}")
        current_uid = f"{uid_prefix}{base_uid}"

        if node_class in ["hbsolve_primitive", "sparam_primitive"] or not inst_file:
            new_inst = dict(inst)
            new_inst["uid"] = current_uid
            new_inst["repeat_count"] = repeat_count
            flat_instances.append(new_inst)

        elif node_class in ["hbsolve_block", "sparam_block"]:
            with open(inst_file, "r") as f:
                sub_data = json.load(f)

            sub_prefix = f"{current_uid}_"

            aliases.update(
                build_aliases_by_child_pin_index(
                    parent_data=data,
                    inst=inst,
                    current_uid=current_uid,
                    sub_data=sub_data,
                    sub_prefix=sub_prefix,
                    inst_file=inst_file,
                )
            )

            sub_insts, sub_wires, sub_pins, sub_labels, sub_aliases = flatten_circuit_data(
                sub_data,
                inst_file.parent,
                builtin_dir,
                script_dir,
                memo,
                sub_prefix,
                resolved_dir,
            )

            if repeat_count > 1:
                for sub_inst in sub_insts:
                    sub_inst = dict(sub_inst)
                    existing_repeat = int(sub_inst.get("repeat_count", 1) or 1)
                    sub_inst["repeat_count"] = existing_repeat * repeat_count
                    flat_instances.append(sub_inst)
            else:
                flat_instances.extend(sub_insts)

            flat_wires.extend(sub_wires)
            flat_pins.extend(sub_pins)
            flat_labels.extend(sub_labels)
            aliases.update(sub_aliases)

        else:
            new_inst = dict(inst)
            new_inst["uid"] = current_uid
            new_inst["repeat_count"] = repeat_count
            flat_instances.append(new_inst)

    def resolve_alias(uid, port):
        require_named_port(port, f"alias endpoint {uid}")
        seen = set()

        while (uid, port) in aliases:
            if (uid, port) in seen:
                raise ValueError(f"Alias cycle detected at {uid}:{port}")

            seen.add((uid, port))
            uid, port = aliases[(uid, port)]
            require_named_port(port, f"alias target {uid}")

        return uid, port

    for wire in flat_wires:
        wire["source_instance_uid"], wire["source_port"] = resolve_alias(
            wire["source_instance_uid"],
            wire["source_port"],
        )
        wire["target_instance_uid"], wire["target_port"] = resolve_alias(
            wire["target_instance_uid"],
            wire["target_port"],
        )

    for pin in flat_pins:
        pin["instance_uid"], pin["port"] = resolve_alias(
            pin["instance_uid"],
            pin["port"],
        )

    for label in flat_labels:
        # Labels in new format (net-anchored) use net_source_uid; old format uses instance_uid.
        # Use whichever is present as the reference instance for resolve_alias.
        if "instance_uid" not in label and "net_source_uid" in label:
            label["instance_uid"] = label["net_source_uid"]
        if "instance_uid" in label:
            label["instance_uid"], label["port"] = resolve_alias(
                label["instance_uid"],
                label["port"],
            )

    return flat_instances, flat_wires, flat_pins, flat_labels, aliases


def collapse_and_save_hbsolve(
    file_path,
    builtin_dir,
    script_dir,
    memo,
    output_dir,
    resolved_dir=None,
    inherited_settings=None,
):
    with open(file_path, "r") as f:
        data = json.load(f)

    if str(data.get("hb_top_block", "")).lower() != "true":
        raise ValueError(
            f"Cannot collapse {file_path.name}: expected this top HB block "
            f'to have "hb_top_block": "true".'
        )

    print(f"    [Action] MERGE: Collapsing '{file_path.name}' into a flattened logical circuit...")

    hb_settings = {key: data[key] for key in HB_SETTING_KEYS if key in data}

    flat_insts, flat_wires, flat_pins, flat_labels, _ = flatten_circuit_data(
        data,
        file_path.parent,
        builtin_dir,
        script_dir,
        memo,
        resolved_dir=resolved_dir,
    )

    ground_uids = set()
    filtered_insts = []

    for inst in flat_insts:
        t_name = inst.get("type_name", "").lower()
        if "ground" in t_name or "gnd" in t_name:
            ground_uids.add(inst["uid"])
        else:
            filtered_insts.append(inst)

    filtered_wires = []

    for wire in flat_wires:
        src_uid = wire.get("source_instance_uid")
        tgt_uid = wire.get("target_instance_uid")

        is_src_gnd = src_uid in ground_uids
        is_tgt_gnd = tgt_uid in ground_uids

        if is_src_gnd and is_tgt_gnd:
            continue
        elif is_src_gnd:
            flat_labels.append({
                "name": "0",
                "instance_uid": tgt_uid,
                "port": wire.get("target_port"),
            })
        elif is_tgt_gnd:
            flat_labels.append({
                "name": "0",
                "instance_uid": src_uid,
                "port": wire.get("source_port"),
            })
        else:
            filtered_wires.append(wire)

    data["instances"] = filtered_insts
    data["wires"] = filtered_wires
    data["pins"] = flat_pins
    data["labels"] = flat_labels
    data["hb_top_block"] = "true"
    data.update(hb_settings)

    freq_source = inherited_settings if inherited_settings else data
    for key in HB_FREQ_KEYS:
        if key in freq_source:
            data[key] = freq_source[key]
        else:
            data.pop(key, None)

    data = canonicalize_hb_exposed_pins_to_p_endpoints(data)
    data = annotate_x_fields_from_exposed_pins(data)

    out_file = output_dir / file_path.name
    with open(out_file, "w") as f:
        json.dump(data, f, indent=2)

    print(
        f"      -> Saved flattened circuit to: {out_file} "
        f"(Instances: {len(filtered_insts)}, Wires: {len(filtered_wires)})"
    )


def save_unchanged_block(file_path, output_dir):
    out_file = output_dir / file_path.name
    if file_path.resolve() != out_file.resolve():
        shutil.copy2(file_path, out_file)
    print(f"    [Action] UNCHANGED: Copied '{file_path.name}' to {out_file}")


def save_block_with_sim_settings(file_path, output_dir, sim_settings=None):
    with open(file_path, "r") as f:
        data = json.load(f)

    if sim_settings:
        data.update(sim_settings)

    data["simulation_figure_title"] = f"subplot: {file_path.stem}"

    out_file = output_dir / file_path.name
    with open(out_file, "w") as f:
        json.dump(data, f, indent=2)

    print(f"    [Action] COPY: Saved '{file_path.name}' to {out_file}")


def execute_merge(
    file_path,
    builtin_dir,
    script_dir,
    memo,
    output_dir,
    processed=None,
    inherited_sparam_settings=None,
    resolved_dir=None,
):
    if processed is None:
        processed = set()

    file_path = Path(file_path).resolve()

    if file_path in processed:
        return

    processed.add(file_path)

    node_class = get_memo_class(file_path, memo)

    if node_class == "mixture":
        raise ValueError(f"'{file_path.name}' is a mixture. Merge is not allowed.")

    if node_class == "hbsolve_block":
        if not is_hbsolve_tree(file_path, builtin_dir, script_dir, memo, resolved_dir):
            raise ValueError(
                f"'{file_path.name}' is classified as hbsolve_block, "
                "but its subtree is not purely hbsolve."
            )

        collapse_and_save_hbsolve(
            file_path,
            builtin_dir,
            script_dir,
            memo,
            output_dir,
            resolved_dir,
            inherited_settings=inherited_sparam_settings,
        )
        return

    if node_class == "sparam_block":
        with open(file_path, "r") as f:
            data = json.load(f)

        validate_repeat_usage(
            data,
            file_path.parent,
            builtin_dir,
            script_dir,
            memo,
            resolved_dir,
            context_name=file_path.name,
        )

        local_settings = extract_settings(data, SPARAM_SETTING_KEYS)

        if inherited_sparam_settings is None:
            inherited_sparam_settings = local_settings

        save_block_with_sim_settings(
            file_path,
            output_dir,
            sim_settings=inherited_sparam_settings,
        )

        for inst in data.get("instances", []):
            type_name = inst.get("type_name")
            if not type_name:
                continue

            inst_file = resolve_component(
                type_name,
                file_path.parent,
                builtin_dir,
                script_dir,
                resolved_dir,
            )

            if not inst_file:
                raise FileNotFoundError(
                    f"Could not resolve component {type_name!r} in {file_path.name}"
                )

            execute_merge(
                inst_file,
                builtin_dir,
                script_dir,
                memo,
                output_dir,
                processed,
                inherited_sparam_settings,
                resolved_dir,
            )

        return

    if node_class == "sparam_primitive":
        save_block_with_sim_settings(
            file_path,
            output_dir,
            sim_settings=inherited_sparam_settings,
        )
        return

    if node_class == "hbsolve_primitive":
        save_unchanged_block(file_path, output_dir)
        return

    raise ValueError(f"Unknown class for {file_path.name}: {node_class}")


def run_merger(target_files):
    print("==================================================")
    print(" Running Merger Engine (Top-Down)                 ")
    print("==================================================\n")

    script_dir = Path(__file__).parent.resolve() if "__file__" in globals() else Path.cwd().resolve()
    builtin_dir = script_dir / "built-in"

    for target in target_files:
        target_path = Path(target)
        project_name = target_path.parent.name if target_path.parent.name else "default_project"

        project_output_dir = get_project_output_dir(script_dir, project_name)
        resolved_dir = get_resolved_dir(script_dir, project_name)
        merge_output_dir = get_merge_output_dir(script_dir, project_name)

        memo_path = project_output_dir / "classification_memo.json"
        if not memo_path.exists():
            raise RuntimeError(f"Classification memo for {project_name} not found. Run classifier first.")

        with open(memo_path, "r") as f:
            memo = json.load(f)

        original_file_path = resolve_source_target(script_dir, target)
        if not original_file_path.exists():
            raise RuntimeError(f"Could not find target file: {original_file_path}")

        file_path = get_stage_input_path(original_file_path, resolved_dir)

        node_class = get_memo_class(original_file_path, memo)

        print(f"Project: {project_name} | Target: {target_path.name} (Class: {node_class})")
        print(f"    Input source: {file_path}")
        print(f"    Merge output: {merge_output_dir}")

        if node_class != "mixture":
            execute_merge(
                file_path,
                builtin_dir,
                script_dir,
                memo,
                merge_output_dir,
                resolved_dir=resolved_dir,
            )
        else:
            raise ValueError(f"'{target_path.name}' is a mixture. Merge is not allowed.")

        print("-" * 60)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Run merger for a single JSON file."
    )
    parser.add_argument(
        "file",
        help="Path to the JSON file to merge, e.g. example_twpa/twpa.json"
    )

    args = parser.parse_args()
    run_merger([args.file])
