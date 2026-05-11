import json
import collections
import shutil
from pathlib import Path

from path_utils import resolve_source_target


# The merger normalises all ground connections to label "0" before this stage.
# Only "0" is recognised here; any other ground-like name surviving to validation
# indicates a pipeline bug and should NOT be silently treated as ground.
GROUND_LABELS = {"0"}


def get_project_output_dir(script_dir, project_name):
    out_dir = script_dir / "outputs" / project_name
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def get_merge_output_dir(script_dir, project_name):
    return script_dir / "outputs" / project_name / "merged"


def get_resolved_ports_output_dir(script_dir, project_name):
    return script_dir / "outputs" / project_name / "resolved_ports"


def get_validated_output_dir(script_dir, project_name):
    validated_dir = script_dir / "outputs" / project_name / "validated"
    validated_dir.mkdir(parents=True, exist_ok=True)
    return validated_dir


def copy_json_tree_for_validation(port_resolved_output_dir, validated_output_dir):
    """
    Start every validation run from a clean copy of the port-resolved output.

    The validator may add fields such as:
        hb_input_field
        hb_input_pin_name
        hb_output_field
        hb_output_pin_name
        hb_exposed_pin_to_p_block

    Those annotations go into outputs/<project>/validated, not back into
    outputs/<project>/merged.
    """
    if not port_resolved_output_dir.exists():
        raise FileNotFoundError(
            f"Missing port-resolved output folder: {port_resolved_output_dir}. "
            "Run port_resolution.py before validator.py."
        )

    validated_output_dir.mkdir(parents=True, exist_ok=True)

    for old_json in validated_output_dir.glob("*.json"):
        old_json.unlink()

    for src in port_resolved_output_dir.glob("*.json"):
        dst = validated_output_dir / src.name
        shutil.copy2(src, dst)


def resolve_from_resolved_ports(type_name, resolved_ports_dir):
    """
    Resolve a child component strictly from outputs/<project>/resolved_ports.

    The validator runs after port_resolution, so every non-built-in child that
    still needs validation must already exist in resolved_ports with numeric
    ports. Qualified type names such as example_add_drop/add_drop are flattened
    by merger/port_resolution to add_drop.json, so lookup is filename-based.
    """
    target_name = Path(str(type_name).replace("\\", "/")).stem + ".json"
    candidate = Path(resolved_ports_dir) / target_name
    return candidate.resolve() if candidate.is_file() else None




def find_builtin_dir(script_dir):
    """Locate logic/built-in robustly.

    The pipeline is sometimes launched from logic/ and sometimes from the app
    root one folder above logic/. Validation inputs still come only from
    outputs/<project>/resolved_ports, but primitive type definitions are library
    metadata and must be resolved from the built-in folder.
    """
    script_dir = Path(script_dir).resolve()
    candidates = [
        script_dir / "built-in",
        script_dir / "logic" / "built-in",
        script_dir.parent / "logic" / "built-in",
        Path.cwd().resolve() / "built-in",
        Path.cwd().resolve() / "logic" / "built-in",
    ]

    seen = set()
    unique_candidates = []
    for candidate in candidates:
        candidate = candidate.resolve()
        if candidate in seen:
            continue
        seen.add(candidate)
        unique_candidates.append(candidate)
        if candidate.exists() and candidate.is_dir():
            return candidate

    raise FileNotFoundError(
        "Could not locate built-in folder. Checked:\n"
        + "\n".join(f"  - {path}" for path in unique_candidates)
    )


def resolve_builtin_type(type_name, builtin_dir):
    """Resolve a primitive/library type from logic/built-in only."""
    if not builtin_dir.exists() or not builtin_dir.is_dir():
        return None

    raw = Path(str(type_name).replace("\\", "/"))
    if raw.suffix != ".json":
        raw = raw.with_suffix(".json")

    direct = builtin_dir / raw
    if direct.is_file():
        return direct.resolve()

    target_name = raw.name
    for path in builtin_dir.rglob(target_name):
        if path.is_file():
            return path.resolve()

    return None


def is_pin_block(inst):
    return inst.get("type_name", "").lower() == "p"


def is_ground_block(inst):
    t = inst.get("type_name", "").lower()
    return t in ["gnd", "ground"]


def get_primary_value(inst, default=None):
    param_order = inst.get("parameter_order", [])
    if not param_order:
        return default

    primary_key = param_order[0]
    params = inst.get("parameters", {})

    if primary_key in params and params[primary_key] not in ["", None]:
        return params[primary_key]

    return default


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


def get_memo_class(file_path, memo):
    file_path = Path(file_path).resolve()

    direct = memo.get(str(file_path))
    if direct:
        return direct.get("class", "unknown")

    matches = [v for k, v in memo.items() if Path(k).name == file_path.name]
    if matches:
        return matches[0].get("class", "unknown")

    return "unknown"


def require_int_port(value, context):
    if not isinstance(value, int):
        raise ValueError(
            f"{context}: expected integer port after port_resolution, "
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
    Validate that merged/validated topology uses only strict numeric ports.

    This stage runs after port_resolution. It does not perform hierarchy or
    named-port resolution; it only asserts that both HB and S-param simulation
    JSONs are numerically clean.
    """
    for wire in data.get("wires", []):
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

    for pin in data.get("pins", []):
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

    for label in data.get("labels", []):
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


def verify_flattened_primitives(out_file_path, builtin_dir, memo):
    """
    Verify that a flattened HB circuit contains only HB primitives.

    The flattened HB circuit itself is read from outputs/<project>/resolved_ports.
    Primitive/library definitions are resolved from logic/built-in. They are not
    expected to be copied into resolved_ports.
    """
    with open(out_file_path, "r") as f:
        data = json.load(f)

    all_valid = True
    invalid_components = []

    for inst in data.get("instances", []):
        type_name = inst.get("type_name")
        if not type_name:
            continue

        if is_pin_block(inst):
            continue

        inst_file = resolve_builtin_type(type_name, builtin_dir)
        if not inst_file:
            invalid_components.append(f"{type_name} (missing from built-in folder: {builtin_dir})")
            all_valid = False
            continue

        node_class = get_memo_class(inst_file, memo)

        if node_class != "hbsolve_primitive":
            all_valid = False
            invalid_components.append(f"{type_name} (Class: {node_class})")

    if not all_valid:
        print("      -> [ERROR] Found non-HB primitive elements in flattened HBSolve circuit:")
        for invalid in sorted(set(invalid_components)):
            print(f"         - {invalid}")

    return all_valid

def build_connectivity_graph(data):
    """
    Old instance-level graph used only for warning compatibility.
    The exact P-port extraction below uses port-level union-find instead.
    """
    adj = collections.defaultdict(list)

    for wire in data.get("wires", []):
        u1 = wire.get("source_instance_uid")
        u2 = wire.get("target_instance_uid")
        if u1 and u2:
            adj[u1].append(u2)
            adj[u2].append(u1)

    label_map = collections.defaultdict(list)
    for lbl in data.get("labels", []):
        name = lbl.get("name")
        uid = lbl.get("instance_uid")
        if name and uid:
            label_map[name].append(uid)

    for _, uids in label_map.items():
        for i in range(len(uids)):
            for j in range(i + 1, len(uids)):
                adj[uids[i]].append(uids[j])
                adj[uids[j]].append(uids[i])

    return adj


def has_path_to_p_block(start_uid, instances_dict, adj_graph):
    queue = [start_uid]
    visited = {start_uid}

    while queue:
        curr_uid = queue.pop(0)
        curr_inst = instances_dict.get(curr_uid)

        if curr_inst and is_pin_block(curr_inst):
            return True

        for neighbor in adj_graph.get(curr_uid, []):
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append(neighbor)

    return False


def build_port_union_find(data):
    """
    Build exact electrical nets over numeric instance ports, e.g. "IT1_P1:1".
    """
    uf = UnionFind()

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

        uf.union(f"{src_uid}:{src_port}", f"{tgt_uid}:{tgt_port}")

    label_groups = collections.defaultdict(list)

    for label in data.get("labels", []):
        uid = label["instance_uid"]
        name = str(label.get("name", ""))

        port = validate_port_on_instance(
            data,
            uid,
            label.get("port"),
            f"label {name!r} on {uid!r}",
        )

        key = f"{uid}:{port}"
        uf.find(key)

        if name in GROUND_LABELS:
            uf.union(key, "0")
        elif name:
            label_groups[name].append(key)

    for _, keys in label_groups.items():
        if len(keys) < 2:
            continue

        first = keys[0]
        for other in keys[1:]:
            uf.union(first, other)

    return uf


def extract_p_block_number(inst):
    value = get_primary_value(inst, default=None)
    if value is None:
        return None

    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(
            f"P block {inst.get('uid')} has non-integer primary value {value!r}"
        )


def derive_exposed_pin_to_p_block_map(data):
    uf = build_port_union_find(data)
    instances = data.get("instances", [])
    root_to_p_blocks = collections.defaultdict(list)

    for inst in instances:
        if not is_pin_block(inst):
            continue

        uid = inst.get("uid")
        if not uid:
            continue

        p_number = extract_p_block_number(inst)
        if p_number is None:
            continue

        port_count = int(inst.get("port_count", 0))
        for local_port in range(1, port_count + 1):
            root = uf.find(f"{uid}:{local_port}")
            root_to_p_blocks[root].append(
                {
                    "p_instance_uid": uid,
                    "p_local_port": local_port,
                    "p_port_number": p_number,
                }
            )

    exposed_map = {}

    for pin in data.get("pins", []):
        pin_name = pin.get("name")
        uid = pin.get("instance_uid")

        if not pin_name or not uid:
            continue

        port = validate_port_on_instance(
            data,
            uid,
            pin.get("port"),
            f"top-level pin {pin_name!r} on {uid!r}",
        )

        root = uf.find(f"{uid}:{port}")
        matches = root_to_p_blocks.get(root, [])

        if not matches:
            continue

        exposed_map[pin_name] = {
            "instance_uid": uid,
            "port": port,
            "p_instance_uid": matches[0]["p_instance_uid"],
            "p_local_port": matches[0]["p_local_port"],
            "p_port_number": matches[0]["p_port_number"],
            "all_p_matches": matches,
        }

    return exposed_map


def select_p_number_for_named_external_pins(pin_to_p, pin_names):
    """
    Given a list such as simulation_input_ports or simulation_output_ports,
    return the first listed external pin that resolves to a JosephsonCircuits P number.

    Returns:
        (selected_pin_name, selected_p_number)

    If no listed pin resolves, returns (None, None).
    """
    for pin_name in pin_names or []:
        if pin_name in pin_to_p:
            return pin_name, int(pin_to_p[pin_name]["p_port_number"])

    return None, None


def update_hb_io_fields(data):
    """
    Adds/updates:
        hb_exposed_pin_to_p_block

    And, if possible:
        hb_input_field
        hb_input_pin_name
        hb_output_field
        hb_output_pin_name

    hb_input_field is selected from simulation_input_ports.
    hb_output_field is selected from simulation_output_ports.

    If no input pin resolves, input falls back to first exposed P-connected pin.
    If no output pin resolves, output falls back to a different exposed P-connected
    pin from input when possible, otherwise the first exposed P-connected pin.
    """
    pin_to_p = derive_exposed_pin_to_p_block_map(data)
    data["hb_exposed_pin_to_p_block"] = pin_to_p

    if str(data.get("x-params", data.get("x_params", ""))).lower() == "true":
        input_pin_names = data.get("x_input_ports", []) or []
        output_pin_names = data.get("x_output_ports", []) or []
    else:
        input_pin_names = data.get("simulation_input_ports", []) or []
        output_pin_names = data.get("simulation_output_ports", []) or []

    input_pin, input_p_number = select_p_number_for_named_external_pins(
        pin_to_p,
        input_pin_names,
    )

    output_pin, output_p_number = select_p_number_for_named_external_pins(
        pin_to_p,
        output_pin_names,
    )

    if input_p_number is None:
        if not input_pin_names:
            raise ValueError(
                "HB circuit has no simulation_input_ports (or x_input_ports) configured. "
                "Set at least one input port in the simulation settings."
            )
        raise ValueError(
            f"HB circuit simulation_input_ports {input_pin_names!r} could not be resolved "
            f"to any P block. Available P-connected pins: {list(pin_to_p.keys()) or 'none'}."
        )

    if output_p_number is None:
        if not output_pin_names:
            raise ValueError(
                "HB circuit has no simulation_output_ports (or x_output_ports) configured. "
                "Set at least one output port in the simulation settings."
            )
        raise ValueError(
            f"HB circuit simulation_output_ports {output_pin_names!r} could not be resolved "
            f"to any P block. Available P-connected pins: {list(pin_to_p.keys()) or 'none'}."
        )

    data["hb_input_field"] = input_p_number
    data["hb_input_pin_name"] = input_pin
    data["hb_output_field"] = output_p_number
    data["hb_output_pin_name"] = output_pin

    return {
        "pin_to_p": pin_to_p,
        "input_pin": input_pin,
        "input_p_number": input_p_number,
        "output_pin": output_pin,
        "output_p_number": output_p_number,
    }


def update_hb_input_field(data):
    result = update_hb_io_fields(data)
    return result["pin_to_p"], result["input_pin"], result["input_p_number"]


def validate_hbsolve_circuit(input_file_path, validated_file_path, builtin_dir, memo):
    """
    Validate an HB block from input_file_path and write the annotated result to
    validated_file_path. Never mutates merger output in-place.
    """
    print(f"    [Action] VALIDATING HB-SOLVE BLOCK: '{input_file_path.name}'")

    primitives_valid = verify_flattened_primitives(
        input_file_path, builtin_dir, memo
    )

    with open(input_file_path, "r") as f:
        data = json.load(f)

    is_valid = primitives_valid

    try:
        validate_numeric_topology(data, input_file_path.name)
    except Exception as err:
        print(f"      -> [ERROR] HB topology validation failed: {err}")
        is_valid = False

    instances = data.get("instances", [])
    labels = data.get("labels", [])

    instances_dict = {}
    has_p_block = False
    has_gnd_block = False

    for inst in instances:
        uid = inst.get("uid")
        if uid:
            instances_dict[uid] = inst

        if is_pin_block(inst):
            has_p_block = True

        if is_ground_block(inst):
            has_gnd_block = True

    has_zero_node = any(str(lbl.get("name", "")) == "0" for lbl in labels)

    if has_gnd_block:
        print(
            "      -> [ERROR] HBcheck Failed: GND block still exists. "
            "It should have been converted to label '0'."
        )
        is_valid = False

    if not has_zero_node:
        print("      -> [ERROR] HBcheck Failed: Missing label '0' ground reference.")
        is_valid = False

    if not has_p_block:
        print("      -> [ERROR] HBcheck Failed: Missing 'P' pin block.")
        is_valid = False

    adj_graph = build_connectivity_graph(data)
    exposed_pins = data.get("pins", [])
    unconnected_pins = 0

    for pin in exposed_pins:
        start_uid = pin.get("instance_uid")
        if start_uid and not has_path_to_p_block(start_uid, instances_dict, adj_graph):
            unconnected_pins += 1

    if unconnected_pins > 0:
        print(
            f"      -> [WARNING] HBcheck: {unconnected_pins}/{len(exposed_pins)} "
            "exposed top-level pins are not physically connected to a 'P' block."
        )

    try:
        io_result = update_hb_io_fields(data)
        pin_to_p = io_result["pin_to_p"]
        input_pin = io_result["input_pin"]
        input_p_number = io_result["input_p_number"]
        output_pin = io_result["output_pin"]
        output_p_number = io_result["output_p_number"]

        if pin_to_p:
            print("      -> HB exposed pin to P-block mapping:")
            for pin_name, info in pin_to_p.items():
                print(
                    f"         - {pin_name}: P block {info['p_instance_uid']} "
                    f"=> JosephsonCircuits port {info['p_port_number']}"
                )

        if input_p_number is not None:
            print(
                f"      -> Selected hb_input_field = {input_p_number} "
                f"from external pin {input_pin!r}"
            )
        else:
            print(
                "      -> [WARNING] Could not derive hb_input_field: no exposed input pin "
                "resolved to a P block."
            )

        if output_p_number is not None:
            print(
                f"      -> Selected hb_output_field = {output_p_number} "
                f"from external pin {output_pin!r}"
            )
        else:
            print(
                "      -> [WARNING] Could not derive hb_output_field: no exposed output pin "
                "resolved to a P block."
            )

    except Exception as err:
        print(f"      -> [ERROR] Could not derive HB input/output fields: {err}")
        is_valid = False

    validated_file_path.parent.mkdir(parents=True, exist_ok=True)
    with open(validated_file_path, "w") as f:
        json.dump(data, f, indent=2)

    print(f"      -> Wrote validated JSON to: {validated_file_path}")

    if is_valid:
        print("      -> [SUCCESS] HBcheck passed all hard requirements.")

    return is_valid


def validate_sparam_circuit(input_file_path, validated_file_path):
    """
    Validate an S-parameter block's numeric topology and copy it forward.
    """
    print(f"    [Action] VALIDATING S-PARAM BLOCK: '{input_file_path.name}'")

    with open(input_file_path, "r") as f:
        data = json.load(f)

    try:
        validate_numeric_topology(data, input_file_path.name)
        if data.get("type") == "schematic":
            port_count = int(data.get("port_count", 0) or 0)
            pins = data.get("pins", []) or []
            if port_count and len(pins) < port_count:
                raise ValueError(
                    f"{input_file_path.name}: schematic declares port_count={port_count} "
                    f"but only exports {len(pins)} pin(s)"
                )
    except Exception as err:
        print(f"      -> [ERROR] S-param topology validation failed: {err}")
        validated_file_path.parent.mkdir(parents=True, exist_ok=True)
        with open(validated_file_path, "w") as f:
            json.dump(data, f, indent=2)
        return False

    validated_file_path.parent.mkdir(parents=True, exist_ok=True)
    with open(validated_file_path, "w") as f:
        json.dump(data, f, indent=2)

    print(f"      -> Wrote validated JSON to: {validated_file_path}")
    print("      -> [SUCCESS] S-param topology validation passed.")
    return True


def validate_hb_inclusions_inside_sparam(
    input_file_path,
    memo,
    builtin_dir,
    resolved_ports_output_dir,
    validated_output_dir,
    processed=None,
):
    """
    Recursively validate children of an S-parameter block using only files from
    outputs/<project>/resolved_ports as inputs.
    """
    if processed is None:
        processed = set()

    input_file_path = Path(input_file_path).resolve()

    if input_file_path in processed:
        return True

    processed.add(input_file_path)

    with open(input_file_path, "r") as f:
        data = json.load(f)

    ok = True

    for inst in data.get("instances", []):
        type_name = inst.get("type_name")
        if not type_name:
            continue

        child_input = resolve_from_resolved_ports(type_name, resolved_ports_output_dir)
        if not child_input:
            print(
                f"      -> [ERROR] Missing resolved_ports file for component "
                f"{type_name!r} referenced in {input_file_path.name}"
            )
            ok = False
            continue

        child_class = get_memo_class(child_input, memo)
        child_validated = validated_output_dir / child_input.name

        if child_class == "hbsolve_block":
            ok = validate_hbsolve_circuit(
                child_input,
                child_validated,
                builtin_dir,
                memo,
            ) and ok

        elif child_class == "sparam_block":
            ok = validate_sparam_circuit(
                child_input,
                child_validated,
            ) and ok

            ok = validate_hb_inclusions_inside_sparam(
                child_input,
                memo,
                builtin_dir,
                resolved_ports_output_dir,
                validated_output_dir,
                processed,
            ) and ok

        elif child_class == "mixture":
            print(f"      -> [ERROR] Mixture block {child_input.name} is not allowed.")
            ok = False

        elif child_class in {"sparam_primitive", "hbsolve_primitive"}:
            if not child_validated.exists():
                shutil.copy2(child_input, child_validated)

        else:
            print(
                f"      -> [ERROR] Unknown classification for {child_input.name}: "
                f"{child_class}"
            )
            ok = False

    return ok


def execute_validation_tree(
    input_file_path,
    memo,
    builtin_dir,
    resolved_ports_output_dir,
    validated_output_dir,
    processed=None,
):
    """
    Validate one tree starting from a file in resolved_ports.
    """
    if processed is None:
        processed = set()

    input_file_path = Path(input_file_path).resolve()

    if input_file_path in processed:
        return True

    processed.add(input_file_path)

    if not input_file_path.exists():
        print(f"    [ERROR] Missing resolved_ports input file: {input_file_path}")
        return False

    node_class = get_memo_class(input_file_path, memo)
    validated_file_path = validated_output_dir / input_file_path.name

    if node_class == "hbsolve_block":
        return validate_hbsolve_circuit(
            input_file_path,
            validated_file_path,
            builtin_dir,
            memo,
        )

    if node_class == "sparam_block":
        sparam_ok = validate_sparam_circuit(
            input_file_path,
            validated_file_path,
        )

        hb_ok = validate_hb_inclusions_inside_sparam(
            input_file_path,
            memo,
            builtin_dir,
            resolved_ports_output_dir,
            validated_output_dir,
        )

        if sparam_ok and hb_ok:
            print("      -> [SUCCESS] S-param block passed topology and HB inclusion validation.")
        else:
            print("      -> [ERROR] S-param block failed validation.")

        return sparam_ok and hb_ok

    if node_class in ["sparam_primitive", "hbsolve_primitive"]:
        shutil.copy2(input_file_path, validated_file_path)
        return True

    if node_class == "mixture":
        print(f"    [ERROR] '{input_file_path.name}' is a mixture. Validation not allowed.")
        return False

    print(f"    [ERROR] Unknown classification for {input_file_path.name}: {node_class}")
    return False


def run_validator(target_files):
    print("==================================================")
    print(" Running Validation Engine                        ")
    print("==================================================\n")

    script_dir = Path(__file__).parent.resolve() if "__file__" in globals() else Path.cwd().resolve()
    builtin_dir = find_builtin_dir(script_dir)

    all_ok = True

    for target in target_files:
        target_path = Path(target)
        project_name = target_path.parent.name if target_path.parent.name else "default_project"

        project_output_dir = get_project_output_dir(script_dir, project_name)
        resolved_ports_output_dir = get_resolved_ports_output_dir(script_dir, project_name)
        validated_output_dir = get_validated_output_dir(script_dir, project_name)

        memo_path = project_output_dir / "classification_memo.json"
        if not memo_path.exists():
            print(f"Error: Memo for {project_name} not found. Please run classifier first.")
            all_ok = False
            continue

        if not resolved_ports_output_dir.exists():
            print(f"Error: Port-resolved output folder not found: {resolved_ports_output_dir}")
            print("       Run port_resolution.py before the validator.")
            all_ok = False
            continue

        with open(memo_path, "r") as f:
            memo = json.load(f)

        try:
            copy_json_tree_for_validation(resolved_ports_output_dir, validated_output_dir)
        except Exception as err:
            print(f"Error: Could not initialize validated output folder: {err}")
            all_ok = False
            continue

        original_file_path = resolve_source_target(script_dir, target)
        node_class = get_memo_class(original_file_path, memo)

        print(f"Project: {project_name} | Validating Target: {target_path.name}")
        print(f"    Port input:       {resolved_ports_output_dir}")
        print(f"    Validated output: {validated_output_dir}")

        if node_class == "mixture":
            print(f"    [ERROR] Target '{target_path.name}' is a mixture. Validation not allowed.")
            ok = False
        else:
            resolved_ports_input_path = resolved_ports_output_dir / original_file_path.name
            ok = execute_validation_tree(
                resolved_ports_input_path,
                memo,
                builtin_dir,
                resolved_ports_output_dir,
                validated_output_dir,
            )

        all_ok = all_ok and ok
        print("-" * 60)

    if all_ok:
        print("Validation complete: all checked targets passed.")
    else:
        print("Validation complete: one or more targets failed.")

    return all_ok


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="Run validator for a single JSON file."
    )
    parser.add_argument(
        "file",
        help="Path to the JSON file to validate, e.g. example_twpa/twpa.json"
    )

    args = parser.parse_args()
    sys.exit(0 if run_validator([args.file]) else 1)
