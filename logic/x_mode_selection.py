import json
import uuid
from pathlib import Path
from copy import deepcopy

from path_utils import resolve_source_target


X_REQUIRED_KEYS = [
    "x_dc_ports",
    "x_dc_currents",
    "x_pump_ports",
    "x_pump_frequencies",
    "x_pump_currents",
    "x_modulation_harmonics",
    "x_pump_harmonics",
]

X_OPTIONAL_DEFAULTS = {
    "x_threewave_mixing": True,
    "x_fourwave_mixing": True,
}

REGULAR_PIPELINE = "regular_pipeline"
X_MERGE_SIMULATION = "x_merge_simulation"
X_REWRITE = "x_rewrite"
INVALID_X_SETTINGS = "invalid_x_settings"
INVALID_X_TOPOLOGY = "invalid_x_topology"

NEXT_STEP_CODES = {
    REGULAR_PIPELINE: 0,
    X_MERGE_SIMULATION: 1,
    X_REWRITE: 2,
    INVALID_X_SETTINGS: 99,
    INVALID_X_TOPOLOGY: 99,
}


def next_step_code(mode):
    return NEXT_STEP_CODES.get(mode, NEXT_STEP_CODES[INVALID_X_SETTINGS])


def load_json(path):
    with open(path, "r") as f:
        return json.load(f)


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    tmp_path.replace(path)


def truthy(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def x_params_enabled(data):
    """Accept both the requested JSON key and a Python-safe mirror."""
    return truthy(data.get("x-params", data.get("x_params", False)))


def normalize_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def normalize_x_settings(data):
    """
    Normalize X-parameter settings in-place and return only the x settings.

    Ports intentionally remain pin labels here. The x_merge/x_rewrite stages can
    later map those labels to generated JosephsonCircuits P block numbers.
    """
    changed = False

    # Keep both spellings present once the user has selected x-params.
    if x_params_enabled(data):
        if data.get("x-params") is not True:
            data["x-params"] = True
            changed = True
        if data.get("x_params") is not True:
            data["x_params"] = True
            changed = True

    # Do not translate hb_* settings into x_* settings.
    # hb_* belongs to HB simulation used for S-parameter extraction; x_* belongs
    # to X-parameter simulation. The UI/user must provide explicit x_* values.

    # Normalize required arrays.
    for key in X_REQUIRED_KEYS:
        if key in data and not isinstance(data[key], list):
            data[key] = normalize_list(data[key])
            changed = True

    # Fill explicit defaults for booleans only; numerical/user-selected fields
    # must be provided by the UI/user.
    for key, default in X_OPTIONAL_DEFAULTS.items():
        if key not in data:
            data[key] = default
            changed = True

    x_settings = {
        key: deepcopy(data[key])
        for key in sorted(set(X_REQUIRED_KEYS) | set(X_OPTIONAL_DEFAULTS) | {"x-params", "x_params"})
        if key in data
    }

    return x_settings, changed

def validate_x_settings(data):
    missing = [key for key in X_REQUIRED_KEYS if key not in data]

    # Allow these to be empty
    ALLOW_EMPTY = {"x_dc_ports", "x_dc_currents"}

    empty = [
        key for key in X_REQUIRED_KEYS
        if key in data
        and len(normalize_list(data[key])) == 0
        and key not in ALLOW_EMPTY
    ]

    errors = []
    if missing:
        errors.append(f"Missing required X-parameter setting(s): {', '.join(missing)}")
    if empty:
        errors.append(f"Empty required X-parameter setting(s): {', '.join(empty)}")

    # Port entries should still be schematic pin labels at this stage, not P numbers.
    for key in ["x_dc_ports", "x_pump_ports"]:
        for item in normalize_list(data.get(key)):
            if not isinstance(item, str) or not item.strip():
                errors.append(
                    f"{key} must contain schematic pin-label strings, got {item!r}. "
                    "Place a pin label in the schematic and store that label here."
                )

    return errors

def get_project_output_dir(script_dir, project_name):
    out_dir = script_dir / "outputs" / project_name
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def get_resolved_dir(script_dir, project_name):
    return script_dir / "outputs" / project_name / "resolved_variables"


def get_stage_input_path(original_file_path, resolved_dir):
    resolved_path = resolved_dir / original_file_path.name
    if resolved_path.exists():
        return resolved_path.resolve()
    return original_file_path.resolve()


def path_is_under(path, parent):
    try:
        Path(path).resolve().relative_to(Path(parent).resolve())
        return True
    except ValueError:
        return False


def candidate_data_roots(current_dir, script_dir):
    """Return candidate directories that may contain project folders."""
    roots = []

    current_dir = Path(current_dir).resolve()
    script_dir = Path(script_dir).resolve()

    # If the current file itself is inside .../data/<project>/..., include that data root.
    parts = current_dir.parts
    for i, part in enumerate(parts):
        if part == "data" and i + 1 < len(parts):
            roots.append(Path(*parts[: i + 1]))

    # Common layouts when this script is app_v2/logic/x_mode_selection.py.
    roots.append(script_dir.parent / "data")      # app_v2/data
    roots.append(script_dir / "data")             # app_v2/logic/data

    # Common layouts relative to the active project/temp folder.
    for ancestor in [current_dir, *current_dir.parents]:
        roots.append(ancestor / "data")           # sibling data folder at/above current_dir
        if ancestor.name == "data":
            roots.append(ancestor)

    # Legacy layouts
    roots.append(script_dir)
    roots.append(current_dir.parent)

    unique = []
    seen = set()
    for root in roots:
        try:
            resolved = root.resolve()
        except Exception:
            resolved = root.absolute()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(resolved)
    return unique


def resolve_component(cell_name, current_dir, builtin_dir, script_dir, resolved_dir=None):
    if not cell_name:
        return None

    raw = Path(str(cell_name).replace("\\", "/"))
    if raw.suffix != ".json":
        raw = raw.with_suffix(".json")

    current_dir = Path(current_dir).resolve()
    builtin_dir = Path(builtin_dir).resolve()

    # Handle resolved_dir if provided (for resolved_variables)
    if resolved_dir is not None:
        resolved_base = Path(resolved_dir).resolve()
        current_base = current_dir

        if path_is_under(current_base, resolved_base):
            local_path = current_base / raw.name
            if local_path.is_file():
                return local_path.resolve()

            local_qualified_path = current_base / raw
            if local_qualified_path.is_file():
                return local_qualified_path.resolve()

        resolved_path = resolved_base / raw
        if resolved_path.is_file():
            return resolved_path.resolve()

        flat_path = resolved_base / raw.name
        if flat_path.is_file():
            return flat_path.resolve()

    # Check for qualified paths (e.g., example_JPA/JPA) in data roots
    if len(raw.parts) > 1:
        local_qualified = current_dir / raw
        if local_qualified.is_file():
            return local_qualified.resolve()
        for root in candidate_data_roots(current_dir, Path(script_dir)):
            data_path = root / raw
            if data_path.is_file():
                return data_path.resolve()

    # Check local unqualified path
    local_path = current_dir / raw.name
    if local_path.is_file():
        return local_path.resolve()

    # Check via resolve_source_target
    source_path = resolve_source_target(Path(script_dir), str(raw))
    if source_path.is_file():
        return source_path.resolve()

    # Check built-ins
    if builtin_dir.exists() and builtin_dir.is_dir():
        for path in builtin_dir.rglob(raw.name):
            if path.is_file():
                return path.resolve()

    return None


def get_memo_class(file_path, memo):
    file_path = Path(file_path).resolve()

    direct = memo.get(str(file_path))
    if direct:
        return direct.get("class", "unknown")

    matches = [v for k, v in memo.items() if Path(k).name == file_path.name]
    if matches:
        return matches[0].get("class", "unknown")

    return "unknown"


def walk_cells(file_path, builtin_dir, script_dir, memo, resolved_dir=None, seen=None):
    """Yield each schematic/built-in JSON reachable from file_path once."""
    if seen is None:
        seen = set()

    file_path = Path(file_path).resolve()
    if file_path in seen or not file_path.exists():
        return

    seen.add(file_path)
    yield file_path

    data = load_json(file_path)
    for inst in data.get("instances", []):
        inst_file = resolve_component(
            inst.get("type_name"),
            file_path.parent,
            builtin_dir,
            script_dir,
            resolved_dir,
        )
        if inst_file:
            yield from walk_cells(inst_file, builtin_dir, script_dir, memo, resolved_dir, seen)


def has_hb_top_block_marker(data):
    return (
        truthy((data.get("simulation") or {}).get("hb", {}).get("top_block", False))
        or truthy(data.get("hb_top_block", False))
    )


def nested_hb_top_block_occurrences(occurrences):
    nested = []
    paths = [
        tuple(item.get("instance_path", []) or [])
        for item in occurrences
    ]
    for item, path in zip(occurrences, paths):
        for other in paths:
            if other == path:
                continue
            if len(other) < len(path) and path[:len(other)] == other:
                nested.append(item)
                break
    return nested


def analyze_topology(file_path, builtin_dir, script_dir, memo, resolved_dir=None):
    classes = {}
    hb_top_blocks = []
    hb_top_block_occurrences = []
    hbsolve_blocks = []
    sparam_blocks = []
    mixtures = []

    file_path = Path(file_path).resolve()
    top_data = load_json(file_path)
    top_is_hb_top = has_hb_top_block_marker(top_data)

    # Unique-file walk is still fine for classes / block inventory.
    for cell_path in walk_cells(file_path, builtin_dir, script_dir, memo, resolved_dir):
        node_class = get_memo_class(cell_path, memo)
        classes[str(cell_path)] = node_class

        data = load_json(cell_path)

        if node_class == "hbsolve_block":
            hbsolve_blocks.append(str(cell_path))
        elif node_class == "sparam_block":
            sparam_blocks.append(str(cell_path))
        elif node_class == "mixture":
            mixtures.append(str(cell_path))

    # Occurrence walk is required for hb_top_block roots.
    for cell_path, instance_path in walk_cell_occurrences(
        file_path,
        builtin_dir,
        script_dir,
        memo,
        resolved_dir,
    ):
        data = load_json(cell_path)
        if has_hb_top_block_marker(data):
            hb_top_blocks.append(str(cell_path))
            hb_top_block_occurrences.append({
                "file": str(cell_path),
                "instance_path": instance_path,
            })

    top_class = get_memo_class(file_path, memo)

    return {
        "top_class": top_class,
        "top_is_hb_top": top_is_hb_top,
        "classes": classes,
        "hb_top_blocks": sorted(set(hb_top_blocks)),
        "hb_top_block_occurrences": hb_top_block_occurrences,
        "nested_hb_top_block_occurrences": nested_hb_top_block_occurrences(hb_top_block_occurrences),
        "hb_top_block_count": len(hb_top_block_occurrences),
        "hbsolve_blocks": sorted(set(hbsolve_blocks)),
        "sparam_blocks": sorted(set(sparam_blocks)),
        "mixtures": sorted(set(mixtures)),
        "has_hb": bool(hbsolve_blocks or hb_top_block_occurrences),
        "has_sparam": bool(sparam_blocks or top_class in {"sparam_block", "sparam_primitive"}),
    }


def walk_cell_occurrences(file_path, builtin_dir, script_dir, memo, resolved_dir=None, instance_path=None, stack=None):
    """Yield reachable cells by instance occurrence, not just unique file path."""
    if instance_path is None:
        instance_path = []
    if stack is None:
        stack = set()

    file_path = Path(file_path).resolve()
    if not file_path.exists():
        return

    # Prevent recursive schematic cycles, but still allow repeated instances.
    stack_key = (file_path, tuple(instance_path))
    if stack_key in stack:
        return

    stack.add(stack_key)

    yield file_path, instance_path

    data = load_json(file_path)
    for inst in data.get("instances", []):
        inst_file = resolve_component(
            inst.get("type_name"),
            file_path.parent,
            builtin_dir,
            script_dir,
            resolved_dir,
        )
        if inst_file:
            child_path = instance_path + [inst.get("uid", inst.get("type_name", "UNKNOWN"))]
            yield from walk_cell_occurrences(
                inst_file,
                builtin_dir,
                script_dir,
                memo,
                resolved_dir,
                child_path,
                stack,
            )

    stack.remove(stack_key)

def choose_x_mode(analysis):
    """
    Decide the first X-parameter branch.

    X_MERGE_SIMULATION is triggered in two distinct situations:

    1. Multiple independent (non-nested) hb_top_block simulation roots exist as
       siblings within the same parent cell — for example, two TWPAs connected
       in series where the top-level container is not itself hb_top_block. These
       sibling HB blocks must be merged into one simulation.

    2. The top cell IS the single hb_top_block root but contains sub-components
       that are also marked hb_top_block (suppressed by skip_hb_top_block_check).
       These sub-components have their own P blocks at their port boundaries;
       x_merge_simulation removes those internal boundary P blocks before the
       X-parameter simulation runs.

    Case 2 is distinct from case 1: the relationship is parent-child, not
    sibling. X_MERGE is not about merging equal-level HB blocks here — it is
    about flattening and removing the boundary P blocks that the sub-component
    left behind. The old rule `hb_top_count > 1 → X_MERGE` conflated both cases
    by using only a count; the new rules use the nested-occurrence data from
    analyze_topology to distinguish them correctly.

    REGULAR_PIPELINE applies only when the top is a pure single HB top block
    whose sub-cells were never independently designated as hb_top_block (so no
    internal P block boundaries exist after flattening).

    X_REWRITE applies when S-parameter cells need to be rewritten to HB
    equivalents before any merging can happen.
    """
    top_class = analysis["top_class"]
    hb_top_count = analysis.get("hb_top_block_count", len(analysis["hb_top_blocks"]))
    nested_hb_tops = analysis.get("nested_hb_top_block_occurrences", [])

    top_is_hbsolve = top_class in {"hbsolve_block", "hbsolve_primitive"}
    top_is_hb_top = analysis.get("top_is_hb_top", False)

    if top_class in {"sparam_primitive", "sparam_block"} and not analysis["has_hb"]:
        return {
            "mode": INVALID_X_TOPOLOGY,
            "reason": (
                "X-parameter simulation was requested for a pure S-parameter network. "
                "No HB/hbsolve content was found in the target dependency tree, so "
                "there is no nonlinear operating point to linearize. Disable x-params "
                "or add an HB/hbsolve block before requesting X-parameters."
            ),
            "use_patched_hbsolve": False,
            "errors": [
                "x-params requires at least one HB/hbsolve block in the target dependency tree."
            ],
        }

    # non_nested_count: hb_top_block occurrences that are not sub-components of
    # another hb_top_block in the hierarchy. This distinguishes sibling HB blocks
    # (each independently a simulation root) from sub-components that happen to
    # carry hb_top_block=true but are suppressed by skip_hb_top_block_check.
    non_nested_count = hb_top_count - len(nested_hb_tops)

    # Case 1: multiple independent sibling HB simulation roots.
    # The top is a container (typically not itself hb_top_block) that holds two
    # or more HB sub-circuits at the same level. Merge them into one simulation.
    # x_merge_simulation.py supports an arbitrary number of HB blocks (not just 2).
    if non_nested_count > 1:
        return {
            "mode": X_MERGE_SIMULATION,
            "reason": (
                f"{non_nested_count} independent hb_top_block=true simulation roots were found "
                "in the hierarchy. Merge the simulations before running the patched hbsolve."
            ),
            "use_patched_hbsolve": True,
        }

    # Case 2: the top IS the single hb_top_block root, but contains one or more
    # nested sub-components that are also hb_top_block (allowed by
    # skip_hb_top_block_check on the top). Those sub-components leave their own
    # P blocks at their port boundaries after flattening. x_merge_simulation
    # removes those internal boundary P blocks.
    if top_is_hb_top and nested_hb_tops:
        return {
            "mode": X_MERGE_SIMULATION,
            "reason": (
                f"The top-level block is hb_top_block=true and contains "
                f"{len(nested_hb_tops)} nested sub-component(s) that also carry "
                "hb_top_block=true (suppressed via skip_hb_top_block_check). "
                "x_merge_simulation removes their internal boundary P blocks before "
                "the X-parameter simulation runs."
            ),
            "use_patched_hbsolve": True,
        }

    if top_class in {"sparam_primitive", "sparam_block"} and analysis["has_hb"]:
        return {
            "mode": X_REWRITE,
            "reason": (
                "The target is classified as an S-parameter block, but its "
                "dependency tree contains HBSolve content. Rewrite the S-parameter "
                "parts into circuit equivalents before X-parameter simulation."
            ),
            "use_patched_hbsolve": True,
        }

    if top_class == "mixture" or analysis["mixtures"]:
        return {
            "mode": X_REWRITE,
            "reason": (
                "The target contains mixed S-parameter and HBSolve content. "
                "Rewrite built-in S blocks to circuit equivalents before X-parameter simulation."
            ),
            "use_patched_hbsolve": True,
        }

    # Pure single HB top block with no independently-hb_top_block sub-components:
    # no internal P block boundaries exist after flattening, so the regular
    # pipeline (merger.py) is sufficient.
    if top_is_hbsolve and top_is_hb_top:
        return {
            "mode": REGULAR_PIPELINE,
            "reason": (
                "The target itself is an HB top block with no nested hb_top_block "
                "sub-components. Continue through the regular pipeline and use the "
                "patched hbsolve at simulation time."
            ),
            "use_patched_hbsolve": True,
        }

    if analysis["has_hb"] and hb_top_count == 0:
        return {
            "mode": REGULAR_PIPELINE,
            "reason": (
                "HB/HBSolve content was found, but no separate hb_top_block=true "
                "simulation roots were found. Continue through the regular pipeline."
            ),
            "use_patched_hbsolve": True,
        }

    if hb_top_count == 1:
        return {
            "mode": REGULAR_PIPELINE,
            "reason": (
                "Exactly one hb_top_block=true simulation root was found. Continue "
                "through the regular pipeline and use the patched hbsolve."
            ),
            "use_patched_hbsolve": True,
        }

    return {
        "mode": REGULAR_PIPELINE,
        "reason": "No X-specific topology case matched; continue with the existing pipeline.",
        "use_patched_hbsolve": False,
    }

def run_x_mode_selection(target_files):
    print("==================================================")
    print(" Running X-Parameter Mode Selection               ")
    print("==================================================\n")

    script_dir = Path(__file__).parent.resolve() if "__file__" in globals() else Path.cwd().resolve()
    builtin_dir = script_dir / "built-in"

    all_results = {}

    for target in target_files:
        target_path = Path(target)
        project_name = target_path.parent.name if target_path.parent.name else "default_project"
        project_output_dir = get_project_output_dir(script_dir, project_name)
        resolved_dir = get_resolved_dir(script_dir, project_name)
        memo_path = project_output_dir / "classification_memo.json"

        result_path = project_output_dir / "x_mode_selection.json"

        if not memo_path.exists():
            print(f"[ERROR] Memo for {project_name} not found. Run classifier first.")
            continue

        with open(memo_path, "r") as f:
            memo = json.load(f)

        original_file_path = resolve_source_target(script_dir, target)
        if not original_file_path.exists():
            print(f"[ERROR] Could not find target file {original_file_path}")
            continue

        stage_input_path = get_stage_input_path(original_file_path, resolved_dir)
        data = load_json(stage_input_path)

        print(f"Project: {project_name} | Target: {target_path.name}")
        print(f"    Input source: {stage_input_path}")

        # Always check for nested HB top blocks, even without x-params
        topology = analyze_topology(stage_input_path, builtin_dir, script_dir, memo, resolved_dir)
        hb_top_count = topology.get("hb_top_block_count", len(topology.get("hb_top_blocks", [])))
        skip_check = data.get("skip_hb_top_block_check", False)

        nested_hb_tops = topology.get("nested_hb_top_block_occurrences", [])

        if nested_hb_tops and not skip_check:
            result = {
                "target": str(original_file_path),
                "stage_input": str(stage_input_path),
                "x_params_requested": False,
                "mode": "NESTED_HB_ERROR",
                "next_step_code": 99,
                "reason": f"Nested hb_top_block=true simulation roots were found ({len(nested_hb_tops)} nested occurrence(s)).",
                "use_patched_hbsolve": False,
                "hb_top_blocks": topology.get("hb_top_blocks", []),
                "nested_hb_top_block_occurrences": nested_hb_tops,
            }
            save_json(result_path, result)
            all_results[str(target)] = result
            print("    [ERROR] Nested HB top blocks detected!")
            for i, item in enumerate(nested_hb_tops, 1):
                path_text = " -> ".join(item.get("instance_path", []) or ["<target>"])
                print(f"      {i}. {item.get('file')} via {path_text}")
            print("-" * 60)
            raise RuntimeError(f"Nested HB top blocks detected in {target}")
        elif nested_hb_tops and skip_check:
            print(f"    [INFO] Nested HB top blocks detected but skipped per skip_hb_top_block_check setting")

        if not x_params_enabled(data):
            result = {
                "target": str(original_file_path),
                "stage_input": str(stage_input_path),
                "x_params_requested": False,
                "mode": REGULAR_PIPELINE,
                "next_step_code": next_step_code(REGULAR_PIPELINE),
                "reason": "x-params is not enabled on the top-level JSON.",
                "use_patched_hbsolve": False,
            }
            save_json(result_path, result)
            all_results[str(target)] = result
            print("    [Decision] regular_pipeline: x-params is not enabled")
            print("-" * 60)
            continue

        x_settings, changed = normalize_x_settings(data)
        errors = validate_x_settings(data)

        if changed:
            # Patch only the current stage input copy if available; otherwise patch the original.
            save_json(stage_input_path, data)
            print("    [Action] Normalized X-parameter keys in the stage input JSON")

        decision = choose_x_mode(topology)
        decision_errors = list(decision.get("errors", []))
        all_errors = errors + decision_errors

        selected_mode = decision["mode"] if not all_errors else (
            decision["mode"] if decision_errors else INVALID_X_SETTINGS
        )

        result = {
            "target": str(original_file_path),
            "stage_input": str(stage_input_path),
            "x_params_requested": True,
            "is_valid": not all_errors,
            "errors": all_errors,
            "mode": selected_mode,
            "next_step_code": next_step_code(selected_mode),
            "reason": decision["reason"] if not errors else "Invalid or incomplete X-parameter settings.",
            "use_patched_hbsolve": decision["use_patched_hbsolve"] if not all_errors else False,
            "x_settings": x_settings,
            "topology": topology,
        }

        save_json(result_path, result)
        all_results[str(target)] = result

        if all_errors:
            print(f"    [Decision] {selected_mode}")
            print(f"    [Reason] {result['reason']}")
            for err in all_errors:
                print(f"      - {err}")
        else:
            print(f"    [Decision] {result['mode']}")
            print(f"    [Reason] {result['reason']}")
            print(f"    [Output] {result_path}")

        print("-" * 60)

    return all_results


def select_next_step_code(target_file):
    """Run selection for one target and return its integer next-step code."""
    import contextlib
    import sys

    with contextlib.redirect_stdout(sys.stderr):
        results = run_x_mode_selection([target_file])
    result = results.get(str(target_file))
    if not result:
        return next_step_code(INVALID_X_SETTINGS)
    return int(result.get("next_step_code", next_step_code(INVALID_X_SETTINGS)))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Select the next pipeline stage for X-parameter simulation.")
    parser.add_argument(
        "targets",
        nargs="*",
        default=[
            "example_add_drop/add_drop.json",
            "example_twpa/twpa.json",
            "example_full/first_half.json",
        ],
        help="Top-level JSON target(s) to analyze.",
    )
    parser.add_argument(
        "--print-code",
        action="store_true",
        help="Print only the integer next-step code for a single target.",
    )

    args = parser.parse_args()

    if args.print_code:
        if len(args.targets) != 1:
            raise SystemExit("--print-code requires exactly one target")
        code = select_next_step_code(args.targets[0])
        print(code)
    else:
        run_x_mode_selection(args.targets)
