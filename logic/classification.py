import json
from pathlib import Path

# --- Classification & Resolution Logic ---


def normalize_json_path(value):
    """Return a normalized relative .json Path for a cell/type reference."""
    raw = Path(str(value).replace("\\", "/"))
    if raw.suffix != ".json":
        raw = raw.with_suffix(".json")
    return raw


def candidate_data_roots(current_dir, script_dir):
    """
    Return candidate directories that may contain project folders.

    This is intentionally generous because GUI pipeline temp folders may live
    under logic/guiV2_pipeline_*, while reusable example projects may live under
    app_v2/data/<project> or logic/data/<project>.
    """
    roots = []

    current_dir = Path(current_dir).resolve()
    script_dir = Path(script_dir).resolve()

    def add(path):
        if path is not None:
            roots.append(Path(path))

    # If the current file itself is inside .../data/<project>/..., include that data root.
    parts = current_dir.parts
    for i, part in enumerate(parts):
        if part == "data" and i + 1 < len(parts):
            add(Path(*parts[: i + 1]))

    # Common layouts when this script is app_v2/logic/classification.py.
    add(script_dir.parent / "data")      # app_v2/data
    add(script_dir / "data")             # app_v2/logic/data

    # Common layouts relative to the active project/temp folder.
    for ancestor in [current_dir, *current_dir.parents]:
        add(ancestor / "data")           # sibling data folder at/above current_dir
        if ancestor.name == "data":
            add(ancestor)

    # Legacy layouts: project folders directly beside the script or current folder.
    add(script_dir)
    add(current_dir.parent)

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


def debug_candidate_paths(cell_name, current_dir, builtin_dir, script_dir):
    raw = normalize_json_path(cell_name)
    current_dir = Path(current_dir).resolve()
    out = []

    if len(raw.parts) > 1:
        out.append(current_dir / raw)
        for root in candidate_data_roots(current_dir, script_dir):
            out.append(root / raw)
    else:
        out.append(current_dir / raw.name)

    builtin_dir = Path(builtin_dir)
    out.append(builtin_dir / raw)
    out.append(builtin_dir / raw.name)

    unique = []
    seen = set()
    for path in out:
        path = Path(path)
        if path in seen:
            continue
        seen.add(path)
        unique.append(path)
    return unique

def resolve_builtin_component(cell_name, builtin_dir):
    """Resolve built-in primitive/block definitions from logic/built-in."""
    builtin_dir = Path(builtin_dir)
    if not builtin_dir.exists() or not builtin_dir.is_dir():
        return None

    raw = normalize_json_path(cell_name)
    target_name = raw.name

    direct = builtin_dir / raw
    if direct.is_file():
        return direct.resolve()

    for path in builtin_dir.rglob(target_name):
        if path.is_file():
            return path.resolve()

    return None


def resolve_component(cell_name, current_dir, builtin_dir, script_dir):
    """
    Resolve a referenced component for classification.

    Important behavior:
      - Qualified refs like example_twpa/twpa are preserved and checked under
        data roots first, e.g. app_v2/data/example_twpa/twpa.json.
      - Unqualified refs are checked locally first, then built-ins.
      - Built-ins are still available from logic/built-in.
    """
    raw = normalize_json_path(cell_name)
    current_dir = Path(current_dir).resolve()
    script_dir = Path(script_dir).resolve()

    candidates = []

    if len(raw.parts) > 1:
        # Preserve qualified/project-relative paths.
        candidates.append(current_dir / raw)
        for root in candidate_data_roots(current_dir, script_dir):
            candidates.append(root / raw)
    else:
        # Local cell in the same project/folder.
        candidates.append(current_dir / raw.name)

    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()

    builtin_path = resolve_builtin_component(cell_name, builtin_dir)
    if builtin_path is not None:
        return builtin_path

    return None


def classify_cell(file_path, builtin_dir, script_dir, memo=None):
    if memo is None:
        memo = {}

    file_path = Path(file_path).resolve()

    error_result = {"class": "unknown", "hb_elements": set(), "sp_elements": set()}
    if not file_path.exists():
        return error_result

    if file_path in memo:
        return memo[file_path]

    with open(file_path, "r") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError:
            return error_result

    # Base Case
    if data.get("type") == "built-in":
        subtype = data.get("subtype", "").lower()
        if "hb" in subtype:
            result = {
                "class": "hbsolve_primitive",
                "hb_elements": {file_path.stem},
                "sp_elements": set(),
            }
        else:
            result = {
                "class": "sparam_primitive",
                "hb_elements": set(),
                "sp_elements": {file_path.stem},
            }

        memo[file_path] = result
        return result

    # Recursive Case
    has_hb_primitive = False
    has_sp_component = False
    has_hb_block = False

    hb_reasons = set()
    sp_reasons = set()

    skip_hb_top_block_check = str(data.get("skip_hb_top_block_check", "")).lower() in {"true", "1", "yes"}

    current_dir = file_path.parent
    instances = data.get("instances", [])

    for inst in instances:
        type_name = inst.get("type_name")
        if not type_name:
            continue

        inst_file = resolve_component(type_name, current_dir, builtin_dir, script_dir)
        if not inst_file:
            checked = "\n".join(
                f"    - {path}"
                for path in debug_candidate_paths(type_name, current_dir, builtin_dir, script_dir)
            )
            raise FileNotFoundError(
                f"Classifier could not resolve component {type_name!r} used in {file_path.name}.\n"
                "Checked candidate locations:\n"
                f"{checked}"
            )

        inst_result = classify_cell(inst_file, builtin_dir, script_dir, memo)
        inst_class = inst_result["class"]

        if inst_class == "mixture":
            # A child mixture makes the parent a mixture too, but keep parent in memo.
            result = {
                "class": "mixture",
                "hb_elements": set(inst_result.get("hb_elements", set())),
                "sp_elements": set(inst_result.get("sp_elements", set())),
            }
            memo[file_path] = result
            return result

        if inst_class == "hbsolve_primitive":
            has_hb_primitive = True
            hb_reasons.add(f"{type_name} (HB primitive)")
        elif inst_class in ["sparam_primitive", "sparam_block"]:
            has_sp_component = True
            sp_reasons.add(f"{type_name} ({inst_class})")
        elif inst_class == "hbsolve_block":
            # An hbsolve_block marked as hb_top_block is self-contained: from the
            # parent's perspective it behaves like an sparam_block (its result is a
            # pre-computed S-matrix). A non-top hbsolve_block must be merged/flattened
            # into the parent's HB simulation.
            # Exception: if the parent has skip_hb_top_block_check=True, all nested
            # hb_top_block sub-blocks are treated as part of the parent's HB simulation
            # (the parent itself is an hb_top_block container).
            _top_val = inst.get("hb", {}).get("top_block", False)
            child_is_hb_top = str(_top_val).lower() in ("true", "1", "yes")
            if child_is_hb_top and not skip_hb_top_block_check:
                has_sp_component = True
                sp_reasons.add(f"{type_name} (HB top block, treated as sparam)")
            else:
                has_hb_block = True
                hb_reasons.add(f"{type_name} (HB block)")
        else:
            raise ValueError(
                f"Classifier got unknown class for component {type_name!r} "
                f"resolved to {inst_file}: {inst_class!r}"
            )

    # Aggregate
    # Mixing HB primitives or non-top HB blocks with sparam components is not allowed.
    if (has_hb_primitive or has_hb_block) and has_sp_component:
        final_class = "mixture"

    elif has_sp_component:
        final_class = "sparam_block"

    elif has_hb_primitive or has_hb_block:
        final_class = "hbsolve_block"

    else:
        final_class = "sparam_block"

    result = {
        "class": final_class,
        "hb_elements": hb_reasons,
        "sp_elements": sp_reasons,
    }

    memo[file_path] = result
    return result


def save_memo(memo, output_file):
    """Serializes the memo to JSON."""
    serializable_memo = {}
    for path, data in memo.items():
        serializable_memo[str(path)] = {
            "class": data["class"],
            "hb_elements": sorted(list(data["hb_elements"])),
            "sp_elements": sorted(list(data["sp_elements"])),
        }
    with open(output_file, "w") as f:
        json.dump(serializable_memo, f, indent=2)
    print(f"  -> Saved classification state to '{output_file}'")


def run_classifier(target_files):
    print("==================================================")
    print(" Running Classification Engine (Bottom-Up)        ")
    print("==================================================\n")

    script_dir = Path(__file__).parent.resolve() if "__file__" in globals() else Path.cwd().resolve()
    builtin_dir = script_dir / "built-in"

    for target in target_files:
        target_path = Path(target)
        project_name = target_path.parent.name if target_path.parent.name else "default_project"

        # Isolate outputs into a project-specific directory
        output_dir = script_dir / "outputs" / project_name
        output_dir.mkdir(parents=True, exist_ok=True)

        file_path = (script_dir / target).resolve()
        if not file_path.exists():
            # Also support app_v2/data/<project>/target.json when classifier is run from logic/.
            data_file_path = (script_dir.parent / "data" / target).resolve()
            if data_file_path.exists():
                file_path = data_file_path
            else:
                raise RuntimeError(f"Could not find target file: '{target}'.")

        print(f"Classifying Project: {project_name} | Target: {target_path.name}")

        memo_file = output_dir / "classification_memo.json"

        # Merge with the existing memo so parallel runs do not erase each other's
        # entries.  The current target is always re-classified from scratch (its
        # old entry may be stale from a previous x_rewrite update).
        memo = {}
        if memo_file.exists():
            try:
                with open(memo_file, "r") as f:
                    serialized = json.load(f)
                target_key = str(file_path)
                for k, v in serialized.items():
                    if k == target_key:
                        continue
                    # Also drop any stale resolved_variables copy of this target
                    # written by x_rewrite; its class is derived fresh each run.
                    k_path = Path(k)
                    if k_path.name == file_path.name and "resolved_variables" in k_path.parts:
                        continue
                    memo[Path(k)] = {
                        "class": v.get("class", "unknown"),
                        "hb_elements": set(v.get("hb_elements", [])),
                        "sp_elements": set(v.get("sp_elements", [])),
                    }
            except (json.JSONDecodeError, KeyError, TypeError):
                memo = {}

        classify_cell(file_path, builtin_dir, script_dir, memo)
        save_memo(memo, memo_file)
        print("-" * 50)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Run classifier for a single JSON file."
    )
    parser.add_argument(
        "file",
        help="Path to the JSON file to classify, e.g. example_twpa/twpa.json",
    )

    args = parser.parse_args()
    run_classifier([args.file])
