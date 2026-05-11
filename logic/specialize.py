import copy
import hashlib
import json
import re
import shutil
from pathlib import Path

from path_utils import resolve_source_target
from variable_propagation import resolve_env, substitute_expr


"""
specialize_components.py

Pipeline stage:

    outputs/<project>/netlisted
        -> outputs/<project>/specialized

Purpose:
    Make parameterized component references explicit and stable.

    Two instances share the same specialized JSON if and only if:
      - they reference the same original component type
      - their effective component parameter values are identical after
        lightweight canonicalization
      - the relevant child implementation identity is identical

Important:
    Simulation settings are preserved. Variable propagation already copied the
    correct simulation settings into every cell before merge/netlist.
"""

STRIP_INSTANCE_PARAMETERS_AFTER_SPECIALIZATION = False

VOLATILE_CHILD_KEYS = {
    "name",
    "about",
    "sketch",
    "position",
    "rotation_degrees",
    "simulation_figure_title",
    "specialized_from",
    "specialized_parameters",
    "specialization_key",
}

SIMULATION_KEYS_NOT_IDENTITY = {
    "simulation_mode",
    "simulation_input_ports",
    "simulation_output_ports",
    "simulation_freq_start",
    "simulation_freq_stop",
    "simulation_freq_points",
    "simulation_sweep_type",
    "simulation_figure_title",
    "hb_top_block",
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
    "multimode",
    "multimode_mode_min",
    "multimode_mode_max",
    "multimode_reference_input_mode",
    "multimode_reference_input_port",
    "multimode_reference_output_port",
    "multimode_output_port",
    "multimode_conversion_output_modes",
    "multimode_symplectic_tolerance",
    "hb_input_field",
    "hb_input_pin_name",
    "hb_output_field",
    "hb_output_pin_name",
    "hb_exposed_pin_to_p_block",
}


def load_json(path):
    with open(path, "r") as f:
        return json.load(f)


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def stable_hash(obj, length=10):
    payload = json.dumps(obj, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:length]


def project_dirs(script_dir, project_name):
    project_dir = script_dir / "outputs" / project_name
    netlisted_dir = project_dir / "netlisted"
    specialized_dir = project_dir / "specialized"
    builtin_dir = script_dir / "built-in"
    return project_dir, netlisted_dir, specialized_dir, builtin_dir


def clean_output_dir(out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.json"):
        old.unlink()


def initialize_specialized_dir(netlisted_dir, specialized_dir):
    """
    Start from an empty specialized/ directory.

    Do NOT bulk-copy netlisted/*.json. The specialized stage writes only:
      - the target/top-level JSON
      - specialized child JSONs that are actually referenced
      - selected metadata files, such as *_json_port_order.json

    This avoids stale unspecialized JSONs such as ABCD_tline.json lingering in
    specialized/.
    """
    if not netlisted_dir.exists():
        raise FileNotFoundError(
            f"Missing netlisted folder: {netlisted_dir}. Run netlisting first."
        )

    clean_output_dir(specialized_dir)

    for src in netlisted_dir.glob("*_json_port_order.json"):
        shutil.copy2(src, specialized_dir / src.name)


def original_type_stem(type_name):
    stem = Path(str(type_name)).stem
    if "__p_" in stem:
        stem = stem.split("__p_")[0]
    return stem


def resolve_component_json(
    type_name,
    current_dir,
    specialized_dir,
    netlisted_dir,
    builtin_dir,
    script_dir,
):
    target_name = Path(type_name).stem + ".json"

    # Important:
    # If the instance still references the original type, prefer netlisted/
    # over specialized/. specialized/ initially contains a straight copy of
    # netlisted/, and later contains generated specialized files too.
    candidates = [
        netlisted_dir / target_name,
        specialized_dir / target_name,
        current_dir / target_name,
    ]

    # If type_name is already specialized, prefer specialized/.
    if "__p_" in Path(type_name).stem:
        candidates = [
            specialized_dir / target_name,
            netlisted_dir / target_name,
            current_dir / target_name,
        ]

    for path in candidates:
        if path.exists():
            return path

    if builtin_dir.exists():
        for path in builtin_dir.rglob(target_name):
            if path.is_file():
                return path

    if "/" in str(type_name) or "\\" in str(type_name):
        path = script_dir / type_name
        if path.suffix != ".json":
            path = path.with_suffix(".json")
        if path.exists():
            return path

    return None


def is_ground_type(type_name):
    return str(type_name).lower() in {"gnd", "ground"}


def is_builtin_primitive_type(type_name):
    return str(type_name) in {"Lj", "NL", "L", "C", "K", "I", "R", "P"}


def strip_redundant_outer_parens(s):
    s = s.strip()
    changed = True
    while changed and s.startswith("(") and s.endswith(")"):
        changed = False
        depth = 0
        balanced_outer = True
        for i, ch in enumerate(s):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0 and i != len(s) - 1:
                    balanced_outer = False
                    break
        if balanced_outer:
            s = s[1:-1].strip()
            changed = True
    return s


def canonical_expr(value):
    """
    Canonicalize parameter expressions for specialization identity.

    Goal:
        Expressions that are physically/arithmetically the same should produce
        the same specialization key.

    Examples that should match:
        (2.466)*(w)/(3e8)*(2890e-6)
        (2.466)*((w))/(300000000.0)*(0.00289)

    We first try SymPy simplification. If SymPy is unavailable or cannot parse
    the expression, fall back to deterministic string cleanup.
    """
    if value is None:
        return None

    s = str(value).strip()
    s = re.sub(r"\s+", "", s)

    # Preserve Julia-style function calls that SymPy does not know about, such
    # as IctoLj(3.4e-6). SymPy may otherwise reinterpret the unknown function
    # call as a flattened symbol/expression, producing invalid Julia like
    # IctoLj3.4e-6.
    if re.search(r"\b[A-Za-z_]\w*\s*\(", s):
        return strip_redundant_outer_parens(s)

    # Normalize Julia-ish syntax for symbolic parsing.
    s_for_sympy = s.replace("π", "pi").replace("^", "**")

    try:
        import sympy as sp

        # Let SymPy create symbols as needed, but make w explicit because it is
        # the frequency variable we intentionally keep symbolic.
        local_dict = {
            "w": sp.Symbol("w"),
            "im": sp.I,
            "pi": sp.pi,
        }

        expr = sp.sympify(s_for_sympy, locals=local_dict)
        expr = sp.simplify(expr)

        # nsimplify helps map 300000000.0 and 3e8 to the same exact value
        # without making arbitrary low-precision rational guesses too aggressive.
        expr = sp.nsimplify(expr)

        out = str(expr)

        # SymPy prints the imaginary unit as "I", but Julia's imaginary unit is
        # "im". If left as "I", Julia resolves it as LinearAlgebra.I
        # (UniformScaling), which breaks calls such as ABCD_shuntY(im*w*C).
        out = re.sub(r"\bI\b", "im", out)

        return out
    except Exception:
        pass

    # Fallback: remove redundant parentheses around simple atoms repeatedly.
    # This handles ((w)) -> w inside larger expressions.
    atom = r"[A-Za-z_]\w*|[0-9]+(?:\.[0-9]*)?(?:e[+-]?[0-9]+)?"
    old = None
    while old != s:
        old = s
        s = strip_redundant_outer_parens(s)
        s = re.sub(rf"\(({atom})\)", r"\1", s)

    return s


def value_from_sources(name, *sources):
    for source in sources:
        if not source:
            continue
        if name in source and source[name] not in ["", None]:
            return source[name]
    return None


def collect_instance_parameter_values(inst, child_data):
    """
    Build concrete parameter map used for specialization.

    Priority:
      1. instance["resolved_parameters"]
      2. instance["parameters"]
      3. child variable defaults/resolved

    Keep w only as the symbolic frequency placeholder if it is explicitly used.
    """
    resolved = inst.get("resolved_parameters", {}) or {}
    params = inst.get("parameters", {}) or {}

    values = {}

    for var in child_data.get("variables", []) or []:
        name = var.get("name")
        if not name:
            continue

        value = value_from_sources(name, resolved, params)
        if value is None and var.get("resolved") not in ["", None]:
            value = var.get("resolved")


        if value is not None:
            values[name] = canonical_expr(value)

    all_names = set(params) | set(resolved)

    for name in sorted(all_names):
        value = value_from_sources(name, resolved, params)
        if value in ["", None]:
            continue

        if name == "w" and str(value).strip() == "w":
            # Do not let a symbolic frequency placeholder create separate
            # component identities.
            continue

        values[name] = canonical_expr(value)

    return dict(sorted(values.items()))


def canonicalize_for_identity(obj):
    """
    Remove volatile/simulation fields and canonicalize expression strings.
    Also reduce specialized child references back to their original type stem so
    nested specialization order does not perturb UUIDs.
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in VOLATILE_CHILD_KEYS or k in SIMULATION_KEYS_NOT_IDENTITY:
                continue

            if k == "type_name":
                out[k] = original_type_stem(v)
            elif k in {"uid", "position", "rotation_degrees"}:
                # UIDs and placement are not component implementation identity.
                continue
            elif k in {"parameters", "resolved_parameters"}:
                out[k] = {
                    name: canonical_expr(val)
                    for name, val in sorted((v or {}).items())
                    if val not in ["", None] and not (name == "w" and str(val).strip() == "w")
                }
            elif k == "variables":
                # Only variable names matter for implementation identity.
                # Sort by name so declaration order doesn't perturb hashes.
                out[k] = sorted(
                    [
                        {"name": var.get("name")}
                        for var in (v or [])
                        if var.get("name") and var.get("name") != "w"
                    ],
                    key=lambda x: x["name"],
                )
            else:
                out[k] = canonicalize_for_identity(v)
        return out

    if isinstance(obj, list):
        return [canonicalize_for_identity(x) for x in obj]

    if isinstance(obj, str):
        return canonical_expr(obj)

    return obj


def specialization_identity(original_type_name, child_data, param_values):
    relevant_child = canonicalize_for_identity(child_data)

    return {
        "original_type_name": original_type_stem(original_type_name),
        "child": relevant_child,
        "parameters": dict(sorted(param_values.items())),
    }


def specialized_name_for(original_type_name, identity):
    base = original_type_stem(original_type_name)
    return f"{base}__p_{stable_hash(identity)}"


def apply_parameter_values_to_child(
    child_data,
    specialized_name,
    param_values,
    original_type_name,
):
    out = copy.deepcopy(child_data)

    out["name"] = specialized_name
    out["specialized_from"] = original_type_stem(original_type_name)
    out["specialized_parameters"] = dict(sorted(param_values.items()))

    new_variables = []
    existing_variable_names = set()

    for var in out.get("variables", []) or []:
        name = var.get("name")
        if not name:
            continue

        existing_variable_names.add(name)

        new_var = copy.deepcopy(var)
        if name in param_values:
            new_var["resolved"] = param_values[name]

        new_variables.append(new_var)

    for name, value in sorted(param_values.items()):
        if name not in existing_variable_names:
            new_variables.append(
                {
                    "name": name,
                    "resolved": value,
                }
            )

    if new_variables:
        out["variables"] = new_variables
    else:
        out.pop("variables", None)

    re_resolve_instance_parameters(out)

    return out


def variable_env_for_specialized_child(cell_data):
    env = {}
    for var in cell_data.get("variables", []) or []:
        name = var.get("name")
        if not name:
            continue
        value = var.get("resolved")
        if value not in ["", None]:
            env[name] = str(value)
    return resolve_env(env)


def re_resolve_instance_parameters(cell_data):
    """
    Recompute internal instance parameters after cloning a parameterized child.

    Variable propagation stores one resolved copy of each shared subcell, so the
    resolved instance parameters can reflect whichever parent override visited
    that subcell last.  The raw_* fields preserve the original expressions;
    specialization uses them here to make each cloned child numerically concrete
    for its own parameter set.
    """
    env = variable_env_for_specialized_child(cell_data)

    for inst in cell_data.get("instances", []) or []:
        raw_params = inst.get("raw_parameters")
        if raw_params is None:
            raw_params = inst.get("parameters", {}) or {}

        names = set(raw_params.keys())
        if not names:
            continue

        resolved = {}
        for name in sorted(names):
            if name in raw_params and raw_params[name] not in ["", None]:
                raw_value = raw_params[name]
            else:
                continue

            if name == "w" and str(raw_value).strip() in {"", "w", "(w)", "((w))"}:
                resolved[name] = "w"
            else:
                substituted = substitute_expr(raw_value, env)
                if substituted == str(raw_value):
                    # No variables from env were resolved; keep the already-merged
                    # value from parameters (which the merger may have resolved at a
                    # deeper level) rather than reverting to the raw expression.
                    existing = (inst.get("parameters") or {}).get(name)
                    resolved[name] = canonical_expr(existing if existing not in ["", None] else raw_value)
                else:
                    resolved[name] = canonical_expr(substituted)

        if resolved:
            inst["parameters"] = resolved
            inst["resolved_parameters"] = resolved


def rewrite_instance_to_specialized(inst, specialized_name, original_type_name, spec_key):
    inst["specialized_from"] = original_type_stem(original_type_name)
    inst["specialization_key"] = spec_key
    inst["type_name"] = specialized_name
    inst.pop("internal_parameter_overrides", None)

    if STRIP_INSTANCE_PARAMETERS_AFTER_SPECIALIZATION:
        inst["parameters"] = {}
        inst["resolved_parameters"] = {}
        inst["parameter_order"] = []
        inst["parameter_kinds"] = {}

    return inst


def should_specialize_instance(inst, child_path, child_data):
    type_name = inst.get("type_name", "")

    if child_path is None or child_data is None:
        return False

    if is_ground_type(type_name):
        return False

    if is_builtin_primitive_type(type_name):
        return False

    has_instance_values = any(
        bool(inst.get(k))
        for k in ["parameters", "resolved_parameters", "internal_parameter_overrides"]
    )
    has_child_variables = bool(child_data.get("variables"))

    return has_instance_values or has_child_variables


def apply_internal_overrides_to_child_data(child_data, overrides):
    out = copy.deepcopy(child_data)
    if not overrides:
        return out

    for dotted_key, value in (overrides or {}).items():
        parts = [part for part in str(dotted_key).split(".") if part]
        if len(parts) < 2:
            continue

        uid = parts[0]
        param_path = parts[1:]
        child_inst = next(
            (item for item in out.get("instances", []) or [] if str(item.get("uid", "")) == uid),
            None,
        )
        if not child_inst:
            continue

        if len(param_path) > 1:
            nested_key = ".".join(param_path)
            child_inst.setdefault("internal_parameter_overrides", {})[nested_key] = str(value)
            continue

        param = param_path[0]
        child_inst.setdefault("parameters", {})[param] = str(value)
        child_inst.setdefault("raw_parameters", {})[param] = str(value)
        child_inst.setdefault("resolved_parameters", {})[param] = str(value)
        order = child_inst.setdefault("parameter_order", [])
        if param not in order:
            order.append(param)
        child_inst.setdefault("parameter_kinds", {}).setdefault(param, "positional")

    return out


def path_is_under(path, parent):
    try:
        Path(path).resolve().relative_to(Path(parent).resolve())
        return True
    except ValueError:
        return False


def specialize_file(
    file_path,
    specialized_dir,
    netlisted_dir,
    builtin_dir,
    script_dir,
    processed,
    created_specializations,
):
    file_path = Path(file_path).resolve()

    if file_path in processed:
        return

    processed.add(file_path)

    data = load_json(file_path)
    changed = False

    for inst in data.get("instances", []) or []:
        type_name = inst.get("type_name")
        if not type_name:
            continue

        child_path = resolve_component_json(
            type_name,
            current_dir=file_path.parent,
            specialized_dir=specialized_dir,
            netlisted_dir=netlisted_dir,
            builtin_dir=builtin_dir,
            script_dir=script_dir,
        )

        if child_path is None:
            continue

        child_data = load_json(child_path)
        if inst.get("internal_parameter_overrides"):
            child_data = apply_internal_overrides_to_child_data(
                child_data,
                inst.get("internal_parameter_overrides") or {},
            )

        if should_specialize_instance(inst, child_path, child_data):
            param_values = collect_instance_parameter_values(inst, child_data)
            identity = specialization_identity(type_name, child_data, param_values)
            spec_key = stable_hash(identity)
            spec_name = specialized_name_for(type_name, identity)
            spec_path = specialized_dir / f"{spec_name}.json"

            if spec_name not in created_specializations:
                specialized_child = apply_parameter_values_to_child(
                    child_data=child_data,
                    specialized_name=spec_name,
                    param_values=param_values,
                    original_type_name=type_name,
                )
                save_json(spec_path, specialized_child)
                created_specializations[spec_name] = spec_path

            rewrite_instance_to_specialized(inst, spec_name, type_name, spec_key)
            changed = True

            specialize_file(
                spec_path,
                specialized_dir,
                netlisted_dir,
                builtin_dir,
                script_dir,
                processed,
                created_specializations,
            )

        else:
            if path_is_under(child_path, specialized_dir) or path_is_under(child_path, netlisted_dir):
                specialize_file(
                    child_path,
                    specialized_dir,
                    netlisted_dir,
                    builtin_dir,
                    script_dir,
                    processed,
                    created_specializations,
                )

    out_path = specialized_dir / file_path.name
    save_json(out_path, data)

    if changed:
        print(f"      -> Specialized references in {file_path.name}")
    else:
        print(f"      -> Copied unchanged {file_path.name}")


def run_specialization_stage(target_files):
    print("==================================================")
    print(" Running Component Specialization Stage           ")
    print("==================================================\n")

    script_dir = Path(__file__).parent.resolve() if "__file__" in globals() else Path.cwd().resolve()
    all_ok = True

    for target in target_files:
        target_path = Path(target)
        project_name = target_path.parent.name if target_path.parent.name else "default_project"

        project_dir, netlisted_dir, specialized_dir, builtin_dir = project_dirs(
            script_dir,
            project_name,
        )

        if not netlisted_dir.exists():
            raise RuntimeError(f"Missing netlisted directory for {project_name}: {netlisted_dir}. Run netlisting stage first.")

        initialize_specialized_dir(netlisted_dir, specialized_dir)

        original_target = resolve_source_target(script_dir, target)
        netlisted_target = netlisted_dir / original_target.name
        specialized_target = specialized_dir / original_target.name

        if not netlisted_target.exists():
            raise RuntimeError(f"Missing netlisted target JSON: {netlisted_target}")

        print(f"Project: {project_name} | Specializing target: {target_path.name}")
        print(f"    Netlisted input:    {netlisted_dir}")
        print(f"    Specialized output: {specialized_dir}")

        processed = set()
        created_specializations = {}

        specialize_file(
            netlisted_target,
            specialized_dir,
            netlisted_dir,
            builtin_dir,
            script_dir,
            processed,
            created_specializations,
        )

        print(f"    [SUCCESS] Wrote specialized target: {specialized_target}")
        print(f"    [SUCCESS] Created {len(created_specializations)} specialized component JSONs.")
        print("-" * 60)

    print("Specialization complete.")
    return all_ok


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Run classifier for a single JSON file."
    )
    parser.add_argument(
        "file",
        help="Path to the JSON file to classify, e.g. example_twpa/twpa.json"
    )

    args = parser.parse_args()
    run_specialization_stage([args.file])
