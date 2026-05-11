import json
import re
from pathlib import Path
from copy import deepcopy


SYMBOL_RE = re.compile(r"\b[A-Za-z_]\w*\b")

Z0_ALIASES = {"Z0", "z_0", "z0", "Z_0"}

KNOWN_GLOBALS = {
    "im", "pi", "π", "exp", "sqrt", "sin", "cos", "tan",
    "log", "log10", "abs", "real", "imag",
    "w",
} | Z0_ALIASES


def matrix_code_uses_z0(cell_data):
    code = str(cell_data.get("matrix_code", ""))
    return any(alias in code for alias in Z0_ALIASES)


def _with_json_suffix(path):
    path = Path(path)
    return path if path.suffix == ".json" else path.with_suffix(".json")


def data_dir_for_script(script_dir):
    """Return the app-level data folder next to logic/."""
    return Path(script_dir).resolve().parent / "data"


def resolve_component(cell_name, current_dir, builtin_dir, script_dir):
    """Resolve a component reference used by variable propagation.

    Resolution order intentionally supports GUI-generated qualified references such
    as ``example_twpa/twpa`` while the pipeline is running from ``logic/``:
      1. relative to the current JSON's folder,
      2. relative to logic/,
      3. relative to the sibling app-level data/ folder,
      4. built-ins by exact filename search.

    This lets a project in ``data/<current_project>`` reference another project
    in ``data/example_twpa`` without requiring manual import/copy before variable
    propagation.
    """
    current_dir = Path(current_dir).resolve()
    builtin_dir = Path(builtin_dir).resolve()
    script_dir = Path(script_dir).resolve()
    data_dir = data_dir_for_script(script_dir)

    raw_name = str(cell_name or "").strip()
    if not raw_name:
        return None

    normalized = raw_name.replace("\\", "/")

    if "/" in normalized:
        rel = _with_json_suffix(Path(normalized))
        candidates = [
            current_dir / rel,
            script_dir / rel,
            data_dir / rel,
            script_dir.parent / rel,
        ]
        for target_path in candidates:
            target_path = target_path.resolve()
            if target_path.is_file():
                return target_path
        return None

    target_name = raw_name if raw_name.endswith(".json") else f"{raw_name}.json"

    candidates = [
        current_dir / target_name,
        script_dir / target_name,
        data_dir / current_dir.name / target_name,
    ]
    for local_path in candidates:
        local_path = local_path.resolve()
        if local_path.is_file():
            return local_path

    if builtin_dir.exists():
        for path in builtin_dir.rglob(target_name):
            if path.is_file():
                return path.resolve()

    if data_dir.exists():
        for path in data_dir.rglob(target_name):
            if path.is_file():
                return path.resolve()

    return None


def component_output_relative_path(component_path, component_ref, script_dir, builtin_dir):
    """Return where a resolved dependency should be copied in resolved_variables/.

    Qualified project references keep their project-relative path, e.g.
    ``example_twpa/twpa`` -> ``example_twpa/twpa.json``. That way later stages
    can resolve the already-propagated copy from the pipeline output instead of
    going back to data/.
    """
    component_path = Path(component_path).resolve()
    script_dir = Path(script_dir).resolve()
    builtin_dir = Path(builtin_dir).resolve()
    data_dir = data_dir_for_script(script_dir)
    ref = str(component_ref or "").replace("\\", "/").strip()

    if ref and "/" in ref:
        return _with_json_suffix(Path(ref))

    for root in (data_dir, script_dir):
        try:
            return component_path.relative_to(root)
        except ValueError:
            pass

    try:
        return component_path.relative_to(builtin_dir)
    except ValueError:
        pass

    return Path(component_path.name)


def child_output_relative_path(parent_cell_path, parent_output_rel_path, component_path, component_ref, script_dir, builtin_dir):
    """Return the resolved_variables path for a child dependency.

    Unqualified references are local to the parent cell's project/folder, so the
    propagated child must be written beside the propagated parent. Qualified
    references such as ``example_add_drop/add_drop`` intentionally keep their
    project-relative path.
    """
    component_path = Path(component_path).resolve()
    parent_cell_path = Path(parent_cell_path).resolve()
    ref = str(component_ref or "").replace("\\", "/").strip()

    if ref and "/" not in ref and component_path.parent == parent_cell_path.parent:
        parent_rel = Path(parent_output_rel_path) if parent_output_rel_path is not None else Path(parent_cell_path.name)
        return parent_rel.parent / component_path.name

    return component_output_relative_path(component_path, component_ref, script_dir, builtin_dir)


def load_json(path):
    with open(path, "r") as f:
        return json.load(f)


def require_named_port_ref(port_ref, *, context):
    if not isinstance(port_ref, str):
        raise ValueError(
            f"{context}: topology ports must be named strings before variable propagation. "
            f"Got {port_ref!r} ({type(port_ref).__name__})."
        )

    if not port_ref:
        raise ValueError(f"{context}: port name may not be empty.")

    return port_ref


def require_int_port(port_ref, *, context):
    if not isinstance(port_ref, int):
        raise ValueError(
            f"{context}: resolved topology port must be an integer. "
            f"Got {port_ref!r} ({type(port_ref).__name__})."
        )

    if port_ref < 1:
        raise ValueError(f"{context}: resolved topology port must be >= 1. Got {port_ref!r}.")

    return port_ref


def instance_lookup(data):
    return {
        inst["uid"]: inst
        for inst in data.get("instances", []) or []
        if inst.get("uid")
    }


def instance_port_names(inst, *, context):
    port_names = inst.get("port_names", []) or []

    if not isinstance(port_names, list) or not all(isinstance(p, str) and p for p in port_names):
        raise ValueError(
            f"{context}: instance {inst.get('uid')!r} must define port_names "
            f"as a non-empty list of strings. Got {port_names!r}."
        )

    return port_names


def primitive_port_map(inst, child_data, *, context):
    """
    Interface map for primitive / built-in cells.

    Primitive cells do not export a pins[] interface. Their interface is their
    declared port_names, mapped directly to 1-based numeric ports.
    """
    port_names = child_data.get("port_names") or inst.get("port_names") or []
    if not isinstance(port_names, list) or not all(isinstance(p, str) and p for p in port_names):
        raise ValueError(
            f"{context}: primitive instance {inst.get('uid')!r} must define "
            f"port_names as a non-empty list of strings. Got {port_names!r}."
        )

    return {name: index for index, name in enumerate(port_names, start=1)}


def exported_pin_external_port(name, external_port):
    match = re.fullmatch(r"P([1-9][0-9]*)", str(name))
    if match:
        return int(match.group(1))
    return external_port


def exported_pin_numbering_uses_p_names(pins):
    numbers = []
    for pin in pins:
        name = pin.get("name")
        if not isinstance(name, str):
            return False
        match = re.fullmatch(r"P([1-9][0-9]*)", name)
        if not match:
            return False
        numbers.append(int(match.group(1)))

    return bool(numbers) and len(numbers) == len(set(numbers))


def exported_pin_port_map(inst, child_data, *, context):
    """
    Interface map for hierarchical schematic cells.

    Important distinction:
      - child pins[].port is the internal endpoint port after normalization.
      - for conventional P-numbered interfaces (P1, P2, ...), the external
        numeric port is the number in the pin name.
      - other schematic interfaces use pins[] position, counted from 1.

    Example:
        child pins:
            [
              {"name": "P_0", "instance_uid": "P1", "port": 1},
              {"name": "P_2", "instance_uid": "L1", "port": 2},
              {"name": "P_pin", "instance_uid": "P1", "port": 2}
            ]

        external interface map:
            P_0   -> 1
            P_2   -> 2
            P_pin -> 3

    Therefore a parent reference to instance.P_pin becomes numeric port 3,
    and the merger later resolves instance:3 back through the child pins[2]
    entry to the internal endpoint P1:2.
    """
    pins = child_data.get("pins", []) or []

    if not pins:
        if child_data.get("type") == "built-in":
            return primitive_port_map(inst, child_data, context=context)

        raise ValueError(
            f"{context}: hierarchical instance {inst.get('uid')!r} of type "
            f"{inst.get('type_name')!r} has no exported pins[]."
        )

    mapping = {}
    use_p_name_numbers = exported_pin_numbering_uses_p_names(pins)
    for external_port, pin in enumerate(pins, start=1):
        name = pin.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError(f"{context}: child exported pin has invalid name: {pin}")

        if name in mapping:
            raise ValueError(f"{context}: duplicate exported pin name {name!r}")

        # Validate the child pin's internal endpoint port, but do not use it as
        # the external port number.
        require_int_port(
            pin.get("port"),
            context=f"{context}: child exported pin {name!r} internal endpoint",
        )

        mapping[name] = (
            exported_pin_external_port(name, external_port)
            if use_p_name_numbers
            else external_port
        )

    return mapping


def normalize_named_ports_to_numbers(data, instance_interface_maps, *, context_name):
    """
    Convert named topology ports to numeric ports once, at resolved-variable time.

    For every instance endpoint, the named port is resolved through that
    instance's actual interface:
      - hierarchical schematic: child pins[].name -> exported pin index
      - built-in / primitive: child or instance port_names index

    This makes the numeric port used in a parent cell match the numeric port
    exposed by the child cell.
    """
    insts = instance_lookup(data)

    for wire in data.get("wires", []) or []:
        src_uid = wire.get("source_instance_uid")
        tgt_uid = wire.get("target_instance_uid")

        if src_uid not in insts:
            raise ValueError(f"{context_name}: wire source instance {src_uid!r} not found: {wire}")
        if tgt_uid not in insts:
            raise ValueError(f"{context_name}: wire target instance {tgt_uid!r} not found: {wire}")

        src_name = require_named_port_ref(
            wire.get("source_port"),
            context=f"{context_name} wire source {src_uid!r}",
        )
        tgt_name = require_named_port_ref(
            wire.get("target_port"),
            context=f"{context_name} wire target {tgt_uid!r}",
        )

        src_map = instance_interface_maps.get(src_uid)
        tgt_map = instance_interface_maps.get(tgt_uid)

        if src_map is None:
            raise ValueError(f"{context_name}: missing interface map for source instance {src_uid!r}")
        if tgt_map is None:
            raise ValueError(f"{context_name}: missing interface map for target instance {tgt_uid!r}")

        if src_name not in src_map:
            raise ValueError(
                f"{context_name}: port {src_name!r} is not exported by instance "
                f"{src_uid!r}. Available ports: {list(src_map)}"
            )
        if tgt_name not in tgt_map:
            raise ValueError(
                f"{context_name}: port {tgt_name!r} is not exported by instance "
                f"{tgt_uid!r}. Available ports: {list(tgt_map)}"
            )

        wire["source_port"] = src_map[src_name]
        wire["target_port"] = tgt_map[tgt_name]

    for pin in data.get("pins", []) or []:
        uid = pin.get("instance_uid")
        if uid not in insts:
            raise ValueError(f"{context_name}: pin instance {uid!r} not found: {pin}")

        port_name = require_named_port_ref(
            pin.get("port"),
            context=f"{context_name} exported pin {pin.get('name')!r}",
        )

        port_map = instance_interface_maps.get(uid)
        if port_map is None:
            raise ValueError(f"{context_name}: missing interface map for pin instance {uid!r}")

        if port_name not in port_map:
            raise ValueError(
                f"{context_name}: port {port_name!r} is not exported by instance "
                f"{uid!r}. Available ports: {list(port_map)}"
            )

        pin["port"] = port_map[port_name]

    for label in data.get("labels", []) or []:
        uid = label.get("instance_uid")
        if uid not in insts:
            raise ValueError(f"{context_name}: label instance {uid!r} not found: {label}")

        port_name = require_named_port_ref(
            label.get("port"),
            context=f"{context_name} label {label.get('name')!r}",
        )

        port_map = instance_interface_maps.get(uid)
        if port_map is None:
            raise ValueError(f"{context_name}: missing interface map for label instance {uid!r}")

        if port_name not in port_map:
            raise ValueError(
                f"{context_name}: port {port_name!r} is not exported by instance "
                f"{uid!r}. Available ports: {list(port_map)}"
            )

        label["port"] = port_map[port_name]

    return data


def inject_z0_aliases(env, z0_value):
    if z0_value in ["", None]:
        return env

    env = dict(env)
    for alias in Z0_ALIASES:
        env[alias] = str(z0_value)

    return env


def extract_top_z0(data):
    if "z0" in data and data["z0"] not in ["", None]:
        return str(data["z0"])
    return None


SIMULATION_GRID_KEYS = [
    "simulation_freq_start",
    "simulation_freq_stop",
    "simulation_freq_points",
]


def truthy(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "on"}
    return bool(value)


def extract_simulation_grid(data):
    grid = {}
    for key in SIMULATION_GRID_KEYS:
        value = data.get(key)
        if value in ["", None]:
            return None
        grid[key] = value
    return grid


def overwrite_hb_grid_from_parent(data, parent_grid):
    if not parent_grid or not truthy(data.get("hb_top_block", False)):
        return data
    for key in SIMULATION_GRID_KEYS:
        data[key] = parent_grid[key]
    return data


def uid_param_names(cell_data):
    """Return the set of parameter names marked kind='uid' in a component definition.

    UID parameters are raw string identifiers (e.g. inductor branch names for the
    K coupling block) that must not be treated as symbolic expressions.  They are
    passed through variable propagation unchanged, without substitution or
    validation against the resolved-variable environment.
    """
    return {
        var["name"]
        for var in cell_data.get("variables", [])
        if var.get("name") and var.get("kind") == "uid"
    }


def variable_defaults(cell_data):
    out = {}
    function_names = cell_function_names(cell_data)

    def handle_var(var):
        name = var.get("name")
        if not name:
            return

        # UID-kind parameters are pass-through identifiers, not symbolic expressions.
        if var.get("kind") == "uid" or name in KNOWN_GLOBALS or name in function_names:
            return

        default = var.get("default", "")

        if name == "w" and default in ["", None]:
            out[name] = "w"
        elif name in Z0_ALIASES and default in ["", None]:
            out[name] = ""
        else:
            out[name] = str(default)

    for var in cell_data.get("variables", []):
        handle_var(var)

    for var in cell_data.get("simulation_variables", []):
        handle_var(var)

    return out


def expression_function_names(expr):
    return set(re.findall(r"\b[A-Za-z_]\w*\b(?=\s*\()", str(expr)))


def cell_function_names(cell_data):
    names = set()
    for key in ["variables", "simulation_variables"]:
        for var in cell_data.get(key, []) or []:
            names.update(expression_function_names(var.get("default", var.get("value", ""))))
    for inst in cell_data.get("instances", []) or []:
        for value in (inst.get("parameters", {}) or {}).values():
            names.update(expression_function_names(value))
    return names


def is_frequency_placeholder(name, value):
    return name == "w" and value in ["", None]


def is_z0_placeholder(name, value):
    return name in Z0_ALIASES and value in ["", None]


def extract_symbols(expr):
    if expr is None:
        return set()

    expr = str(expr)

    function_names = expression_function_names(expr)

    symbols = set(SYMBOL_RE.findall(expr))
    symbols = {s for s in symbols if not s[0].isdigit()}

    return symbols - KNOWN_GLOBALS - function_names


def substitute_expr(expr, env):
    if expr is None:
        return expr

    expr = str(expr)

    def repl(match):
        name = match.group(0)
        rest = expr[match.end():]
        if re.match(r"\s*\(", rest):
            return name
        if name in env and env[name] not in ["", None]:
            return f"({env[name]})"
        return name

    return SYMBOL_RE.sub(repl, expr)


def resolve_env(env):
    env = dict(env)
    changed = True

    while changed:
        changed = False

        for name, expr in list(env.items()):
            if expr in ["", None]:
                continue

            new_expr = substitute_expr(
                expr,
                {k: v for k, v in env.items() if k != name}
            )

            if new_expr != expr:
                env[name] = new_expr
                changed = True

    return env


def validate_expr_resolved(expr, env, context, missing_sources=None):
    if expr in ["", None]:
        raise ValueError(f"Empty unresolved expression in {context}")

    missing = extract_symbols(expr) - {
        k for k, v in env.items()
        if v not in ["", None]
    }

    if missing:
        msg = (
            f"Unresolved variable(s) {sorted(missing)}\n"
            f"  Expression: {expr}\n"
            f"  Context: {context}\n"
        )

        if missing_sources:
            msg += "  Expected sources checked:\n"
            for src in missing_sources:
                msg += f"    - {src}\n"

        raise ValueError(msg)


def patch_z0_variables_in_data(data, env):
    z0_value = None

    for alias in Z0_ALIASES:
        if alias in env and env[alias] not in ["", None]:
            z0_value = env[alias]
            break

    if z0_value is None:
        return data

    for key in ["variables", "simulation_variables"]:
        for var in data.get(key, []):
            if var.get("name") in Z0_ALIASES:
                var["default"] = z0_value
                var["resolved"] = z0_value

    if "z0" in data:
        data["z0"] = float(z0_value) if _looks_numeric(z0_value) else z0_value

    return data


def _looks_numeric(value):
    try:
        float(str(value))
        return True
    except ValueError:
        return False


def choose_raw_expr(param_name, params, child_defaults, env, inst, cell_path):
    if param_name in params and (params[param_name] not in ["", None] or param_name in {"w", *Z0_ALIASES}):
        raw = params[param_name]
    elif param_name in child_defaults and (child_defaults[param_name] not in ["", None] or param_name in {"w", *Z0_ALIASES}):
        raw = child_defaults[param_name]
    elif param_name in env and env[param_name] not in ["", None]:
        raw = env[param_name]
    else:
        raise ValueError(
            f"No value/default found for parameter {param_name!r} "
            f"on instance {inst.get('uid')} in {cell_path.name}"
        )

    if is_frequency_placeholder(param_name, raw):
        return "w"

    if is_z0_placeholder(param_name, raw):
        for alias in Z0_ALIASES:
            if alias in env and env[alias] not in ["", None]:
                return env[alias]

    return raw


def propagate_variables(
    cell_path,
    builtin_dir,
    script_dir,
    inherited_env=None,
    inherited_simulation_grid=None,
    output_dir=None,
    output_rel_path=None,
    stack=None,
):
    cell_path = Path(cell_path).resolve()
    current_dir = cell_path.parent

    if stack is None:
        stack = []

    if cell_path in stack:
        cycle = " -> ".join(p.name for p in stack + [cell_path])
        raise RuntimeError(f"Recursive cell dependency detected: {cycle}")

    stack = stack + [cell_path]

    data = deepcopy(load_json(cell_path))

    inherited_env = dict(inherited_env or {})
    inherited_simulation_grid = dict(inherited_simulation_grid or {})
    data = overwrite_hb_grid_from_parent(data, inherited_simulation_grid)
    current_simulation_grid = extract_simulation_grid(data) or inherited_simulation_grid

    top_z0 = extract_top_z0(data)
    if top_z0 is not None:
        inherited_env = inject_z0_aliases(inherited_env, top_z0)

    local_defaults = variable_defaults(data)

    env = {}
    env.update(inherited_env)
    env.update(local_defaults)

    if any(alias in env and env[alias] not in ["", None] for alias in Z0_ALIASES):
        z0_value = next(
            env[alias]
            for alias in Z0_ALIASES
            if alias in env and env[alias] not in ["", None]
        )
        env = inject_z0_aliases(env, z0_value)

    env = resolve_env(env)

    resolved_variables = []
    function_names = cell_function_names(data)

    for var in data.get("variables", []):
        var = dict(var)
        name = var.get("name")

        if name in KNOWN_GLOBALS or name in function_names:
            continue

        if name:
            raw_default = var.get("default", "")

            # If a parent cell explicitly supplied a value for this variable (via
            # instance parameters), that value is present in inherited_env and must
            # take precedence over the cell's own default.  This is what allows
            # "exposed variables" set in a parent's block inspector to actually
            # propagate into the child cell's simulation.
            if (
                name in inherited_env
                and inherited_env[name] not in ["", None]
                and not is_frequency_placeholder(name, inherited_env[name])
                and not is_z0_placeholder(name, inherited_env[name])
            ):
                resolved_default = str(inherited_env[name])
            elif is_frequency_placeholder(name, raw_default):
                resolved_default = "w"
            elif is_z0_placeholder(name, raw_default):
                resolved_default = choose_raw_expr(
                    name, {}, {}, env, {"uid": "<cell-variable>"}, cell_path
                )
            else:
                resolved_default = substitute_expr(raw_default, env)

            validate_expr_resolved(
                resolved_default,
                env,
                f"{cell_path.name} variable {name}",
            )

            var["resolved"] = resolved_default
            env[name] = resolved_default

        resolved_variables.append(var)

    if resolved_variables:
        data["variables"] = resolved_variables

    resolved_sim_variables = []

    for var in data.get("simulation_variables", []):
        var = dict(var)
        name = var.get("name")

        if name in KNOWN_GLOBALS or name in function_names:
            continue

        if name:
            raw_default = var.get("default", "")

            if is_frequency_placeholder(name, raw_default):
                resolved_default = "w"
            elif is_z0_placeholder(name, raw_default):
                resolved_default = choose_raw_expr(
                    name, {}, {}, env, {"uid": "<cell-sim-variable>"}, cell_path
                )
            else:
                resolved_default = substitute_expr(raw_default, env)

            validate_expr_resolved(
                resolved_default,
                env,
                f"{cell_path.name} simulation variable {name}",
            )

            var["resolved"] = resolved_default
            env[name] = resolved_default

        resolved_sim_variables.append(var)

    if resolved_sim_variables:
        data["simulation_variables"] = resolved_sim_variables

    if any(alias in env and env[alias] not in ["", None] for alias in Z0_ALIASES):
        z0_value = next(
            env[alias]
            for alias in Z0_ALIASES
            if alias in env and env[alias] not in ["", None]
        )
        env = inject_z0_aliases(env, z0_value)

    env = resolve_env(env)

    instance_interface_maps = {}

    for inst in data.get("instances", []):
        type_name = inst.get("type_name")
        if not type_name:
            continue

        inst_file = resolve_component(type_name, current_dir, builtin_dir, script_dir)
        if inst_file is None:
            raise FileNotFoundError(
                f"Could not resolve component {type_name!r} used in {cell_path.name}"
            )

        child_data = load_json(inst_file)
        child_defaults = variable_defaults(child_data)
        uid_params = uid_param_names(child_data)

        child_env = dict(env)
        child_env.update(child_defaults)

        if any(alias in child_env and child_env[alias] not in ["", None] for alias in Z0_ALIASES):
            z0_value = next(
                child_env[alias]
                for alias in Z0_ALIASES
                if alias in child_env and child_env[alias] not in ["", None]
            )
            child_env = inject_z0_aliases(child_env, z0_value)

        params = inst.get("parameters", {})
        # Preserve the authoring-time expressions so later specialization can
        # re-resolve this child under each concrete parent override.  A shared
        # subcell may be visited with multiple inherited environments; the
        # resolved parameters below represent only the current visit.
        if "raw_parameters" not in inst:
            inst["raw_parameters"] = deepcopy(params)

        all_param_names = set()
        all_param_names.update(params.keys())
        all_param_names.update(child_defaults.keys())
        # UID-kind params are excluded from child_defaults (variable_defaults skips
        # them), but they may still appear in params from the GUI export. Include
        # them so they are written to resolved_parameters.
        for var in child_data.get("variables", []):
            if var.get("kind") == "uid" and var.get("name"):
                all_param_names.add(var["name"])

        resolved_params = {}

        for param_name in sorted(all_param_names):
            # UID-kind parameters are raw string identifiers (e.g. inductor branch
            # names).  Pass them through unchanged without expression substitution
            # or environment validation.
            if param_name in uid_params:
                raw_uid = (
                    params.get(param_name)
                    or next(
                        (v.get("default") for v in child_data.get("variables", [])
                         if v.get("name") == param_name),
                        None,
                    )
                    or ""
                )
                resolved_params[param_name] = str(raw_uid)
                child_env[param_name] = str(raw_uid)
                continue

            raw_expr = choose_raw_expr(
                param_name,
                params,
                child_defaults,
                env,
                inst,
                cell_path,
            )

            resolved_expr = substitute_expr(raw_expr, env)
            missing = extract_symbols(resolved_expr) - {
                k for k, v in env.items()
                if v not in ["", None]
            }

            validate_expr_resolved(
                resolved_expr,
                env,
                f"{cell_path.name} instance {inst.get('uid')} parameter {param_name}",
                missing_sources=[
                    f"instance.parameters of {inst.get('uid')}",
                    f"child defaults in {inst_file.name}",
                    "inherited variables from parent chain",
                    'top-level JSON field "z0"',
                ],
            )

            resolved_params[param_name] = resolved_expr
            child_env[param_name] = resolved_expr

            if param_name in Z0_ALIASES:
                child_env = inject_z0_aliases(child_env, resolved_expr)

        inst["parameters"] = resolved_params
        inst["resolved_parameters"] = resolved_params

        for child_var, child_default in child_defaults.items():
            if child_var not in child_env or child_env[child_var] in ["", None]:
                if is_frequency_placeholder(child_var, child_default):
                    child_env[child_var] = "w"
                elif is_z0_placeholder(child_var, child_default):
                    child_env[child_var] = choose_raw_expr(
                        child_var,
                        {},
                        {},
                        child_env,
                        inst,
                        cell_path,
                    )
                else:
                    child_env[child_var] = child_default

            resolved_child_expr = substitute_expr(child_env[child_var], child_env)

            validate_expr_resolved(
                resolved_child_expr,
                child_env,
                f"{inst_file.name} variable {child_var} via instance {inst.get('uid')}",
            )

            child_env[child_var] = resolved_child_expr

        if child_data.get("type") != "built-in":
            propagate_variables(
                inst_file,
                builtin_dir,
                script_dir,
                inherited_env=child_env,
                inherited_simulation_grid=current_simulation_grid,
                output_dir=output_dir,
                output_rel_path=child_output_relative_path(
                    cell_path,
                    output_rel_path,
                    inst_file,
                    type_name,
                    script_dir,
                    builtin_dir,
                ),
                stack=stack,
            )

    data = patch_z0_variables_in_data(data, env)

    if output_dir is not None:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        out_rel = Path(output_rel_path) if output_rel_path is not None else Path(cell_path.name)
        out_path = output_dir / out_rel
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w") as f:
            json.dump(data, f, indent=2)

    return data


def run_variable_propagation(target_files):
    script_dir = Path(__file__).parent.resolve() if "__file__" in globals() else Path.cwd().resolve()
    builtin_dir = script_dir / "built-in"

    for target in target_files:
        target_path = (script_dir / target).resolve()

        if not target_path.exists():
            data_target = (data_dir_for_script(script_dir) / target).resolve()
            if data_target.exists():
                target_path = data_target
            else:
                raise FileNotFoundError(f"Target file does not exist: {target_path}")

        project_name = Path(target).parent.name or "default_project"
        output_dir = script_dir / "outputs" / project_name / "resolved_variables"

        print(f"Resolving variables for {target}")
        propagate_variables(
            target_path,
            builtin_dir=builtin_dir,
            script_dir=script_dir,
            inherited_env={},
            output_dir=output_dir,
        )
        print(f"  -> Saved resolved cells to {output_dir}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Run variable and parameter propagation for a single JSON file."
    )
    parser.add_argument(
        "file",
        help="Path to the JSON file to process, e.g. example_twpa/twpa.json"
    )

    args = parser.parse_args()
    run_variable_propagation([args.file])
