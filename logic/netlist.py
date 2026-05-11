import json
import shutil
from pathlib import Path

from path_utils import resolve_source_target


def get_project_output_dir(script_dir, project_name):
    out_dir = script_dir / "outputs" / project_name
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def get_validated_dir(script_dir, project_name):
    return script_dir / "outputs" / project_name / "validated"


def get_netlisted_dir(script_dir, project_name):
    out_dir = script_dir / "outputs" / project_name / "netlisted"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def copy_validated_to_netlisted(validated_dir, netlisted_dir):
    """
    Start the netlisting stage from the validated JSON tree.

    If a JSON does not need any netlisting-specific modification, it is still
    copied straight through into outputs/<project>/netlisted.
    """
    if not validated_dir.exists():
        raise FileNotFoundError(
            f"Missing validated folder: {validated_dir}. Run validator first."
        )

    netlisted_dir.mkdir(parents=True, exist_ok=True)

    for old_json in netlisted_dir.glob("*.json"):
        old_json.unlink()

    for src in validated_dir.glob("*.json"):
        shutil.copy2(src, netlisted_dir / src.name)


def resolve_component(
    cell_name,
    current_dir,
    builtin_dir,
    script_dir,
    netlisted_dir=None,
    validated_dir=None,
):
    target_name = cell_name if cell_name.endswith(".json") else f"{cell_name}.json"
    target_basename = Path(target_name).name

    # Prefer netlisted copies first, because this is the current stage output.
    if netlisted_dir is not None:
        path = netlisted_dir / target_basename
        if path.is_file():
            return path

    # Then validated files.
    if validated_dir is not None:
        path = validated_dir / target_basename
        if path.is_file():
            return path

    if "/" in cell_name or "\\" in cell_name:
        target_path = script_dir / cell_name
        if target_path.suffix != ".json":
            target_path = target_path.with_suffix(".json")
        return target_path if target_path.is_file() else None

    local_path = current_dir / target_name
    if local_path.is_file():
        return local_path

    if builtin_dir.exists():
        for path in builtin_dir.rglob(target_name):
            if path.is_file():
                return path

    return None


def load_json(path):
    with open(path, "r") as f:
        return json.load(f)


def get_memo_class(file_path, memo):
    file_path = Path(file_path).resolve()

    direct = memo.get(str(file_path))
    if direct:
        return direct.get("class", "unknown")

    matches = [
        v for k, v in memo.items()
        if Path(k).name == file_path.name
    ]

    if matches:
        return matches[0].get("class", "unknown")

    return "unknown"


def require_int_port(value, context):
    if not isinstance(value, int):
        raise ValueError(
            f"{context}: expected integer port after variable propagation/validation, "
            f"got {value!r} ({type(value).__name__})"
        )

    if value < 1:
        raise ValueError(f"{context}: expected port >= 1, got {value!r}")

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


def extract_julia_ports(file_path):
    """Return JSON-defined external port order as a list of (uid, internal_port) pairs."""
    data = load_json(file_path)

    ports = []
    pins = data.get("pins", [])

    for external_port, pin in enumerate(pins, start=1):
        pin_name = pin.get("name")
        uid = pin.get("instance_uid")

        if not pin_name:
            raise ValueError(f"{file_path.name}: exported pin #{external_port} is missing name: {pin}")

        if not uid:
            raise ValueError(
                f"{file_path.name}: exported pin {pin_name!r} "
                f"(external port {external_port}) is missing instance_uid: {pin}"
            )

        internal_port = validate_port_on_instance(
            data,
            uid,
            pin.get("port"),
            f"{file_path.name} exported pin {pin_name!r}",
        )

        ports.append((uid, internal_port))

    return ports


def extract_hb_input_field(file_path):
    data = load_json(file_path)
    return data.get("hb_input_field")


def extract_hb_input_pin_name(file_path):
    data = load_json(file_path)
    return data.get("hb_input_pin_name")


def extract_hb_output_field(file_path):
    data = load_json(file_path)
    return data.get("hb_output_field")


def extract_hb_output_pin_name(file_path):
    data = load_json(file_path)
    return data.get("hb_output_pin_name")


def julia_port_array(ports):
    items = [f'("{uid}", {port})' for uid, port in ports]
    return "[" + ", ".join(items) + "]"


def execute_netlist_tree(
    file_path,
    builtin_dir,
    script_dir,
    memo,
    output_jl_file,
    output_json,
    netlisted_dir,
    validated_dir,
    processed=None,
):
    if processed is None:
        processed = set()

    file_path = Path(file_path).resolve()

    if file_path in processed:
        return

    processed.add(file_path)

    node_class = get_memo_class(file_path, memo)

    if node_class in ["sparam_block", "hbsolve_block"]:
        cell_name = file_path.stem
        ports = extract_julia_ports(file_path)

        output_json[cell_name] = ports

        hb_input_field = extract_hb_input_field(file_path)
        hb_input_pin_name = extract_hb_input_pin_name(file_path)
        hb_output_field = extract_hb_output_field(file_path)
        hb_output_pin_name = extract_hb_output_pin_name(file_path)

        if hb_input_field is not None:
            output_json[f"{cell_name}__hb_input_field"] = hb_input_field

        if hb_input_pin_name is not None:
            output_json[f"{cell_name}__hb_input_pin_name"] = hb_input_pin_name

        if hb_output_field is not None:
            output_json[f"{cell_name}__hb_output_field"] = hb_output_field

        if hb_output_pin_name is not None:
            output_json[f"{cell_name}__hb_output_pin_name"] = hb_output_pin_name

        with open(output_jl_file, "a") as f:
            f.write(f"# JSON-defined external port order for cell: {cell_name}\n")
            f.write(f"# This order is the exact order of pins[] in {file_path.name}.\n")
            f.write(f"ports_{cell_name} = {julia_port_array(ports)}\n")

            if hb_input_field is not None:
                f.write(f"hb_input_field_{cell_name} = {int(hb_input_field)}\n")

            if hb_input_pin_name is not None:
                f.write(f'hb_input_pin_name_{cell_name} = "{hb_input_pin_name}"\n')

            if hb_output_field is not None:
                f.write(f"hb_output_field_{cell_name} = {int(hb_output_field)}\n")

            if hb_output_pin_name is not None:
                f.write(f'hb_output_pin_name_{cell_name} = "{hb_output_pin_name}"\n')

            f.write(f'println("{cell_name} JSON port order: ", ports_{cell_name})\n')

            if hb_input_field is not None:
                f.write(
                    f'println("{cell_name} HB input field: ", '
                    f"hb_input_field_{cell_name})\n"
                )

            if hb_output_field is not None:
                f.write(
                    f'println("{cell_name} HB output field: ", '
                    f"hb_output_field_{cell_name})\n"
                )

            f.write("\n")

        print(f"      -> Generated JSON port order for cell: {cell_name}")

        data = load_json(file_path)

        for inst in data.get("instances", []):
            type_name = inst.get("type_name")
            if not type_name:
                continue

            inst_file = resolve_component(
                type_name,
                file_path.parent,
                builtin_dir,
                script_dir,
                netlisted_dir=netlisted_dir,
                validated_dir=validated_dir,
            )

            if inst_file:
                execute_netlist_tree(
                    inst_file,
                    builtin_dir,
                    script_dir,
                    memo,
                    output_jl_file,
                    output_json,
                    netlisted_dir,
                    validated_dir,
                    processed,
                )


def run_netlist_generator(target_files):
    print("==================================================")
    print(" Running JSON Port Order Generator                ")
    print("==================================================\n")

    script_dir = Path(__file__).parent.resolve() if "__file__" in globals() else Path.cwd().resolve()
    builtin_dir = script_dir / "built-in"

    for target in target_files:
        target_path = Path(target)
        project_name = target_path.parent.name if target_path.parent.name else "default_project"

        project_output_dir = get_project_output_dir(script_dir, project_name)
        validated_dir = get_validated_dir(script_dir, project_name)
        netlisted_dir = get_netlisted_dir(script_dir, project_name)

        memo_path = project_output_dir / "classification_memo.json"
        if not memo_path.exists():
            raise RuntimeError(f"Classification memo for {project_name} not found. Run classifier first.")

        if not validated_dir.exists():
            raise RuntimeError(f"Validated folder not found: {validated_dir}. Run validator before netlisting.")

        with open(memo_path, "r") as f:
            memo = json.load(f)

        copy_validated_to_netlisted(validated_dir, netlisted_dir)

        original_file_path = resolve_source_target(script_dir, target)
        validated_file_path = validated_dir / original_file_path.name
        netlisted_file_path = netlisted_dir / original_file_path.name

        if not validated_file_path.exists():
            raise RuntimeError(f"Target validated JSON not found: {validated_file_path}")

        if not netlisted_file_path.exists():
            raise RuntimeError(f"Target netlisted JSON not found after copy: {netlisted_file_path}")

        output_jl_file = netlisted_dir / f"{project_name}_json_port_order.jl"
        output_json_file = netlisted_dir / f"{project_name}_json_port_order.json"

        with open(output_jl_file, "w") as f:
            f.write("# ==========================================\n")
            f.write("# Auto-Generated JSON Port Ordering\n")
            f.write(f"# Project: {project_name}\n")
            f.write("# Reads from outputs/<project>/validated\n")
            f.write("# Writes to outputs/<project>/netlisted\n")
            f.write("# External port order is pins[] order.\n")
            f.write("# ==========================================\n\n")

        output_json = {}

        print(f"Project: {project_name} | Building JSON-defined port order")
        print(f"    Validated input: {validated_dir}")
        print(f"    Netlisted output: {netlisted_dir}")

        execute_netlist_tree(
            netlisted_file_path,
            builtin_dir,
            script_dir,
            memo,
            output_jl_file,
            output_json,
            netlisted_dir,
            validated_dir,
        )

        with open(output_json_file, "w") as f:
            json.dump(output_json, f, indent=2)

        print(f"    [SUCCESS] Copied validated JSONs to: {netlisted_dir}")
        print(f"    [SUCCESS] Saved Julia port order to: {output_jl_file.name}")
        print(f"    [SUCCESS] Saved JSON port order to:  {output_json_file.name}")
        print("-" * 60)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Run netlist port-order generator for a single JSON file."
    )
    parser.add_argument(
        "file",
        help="Path to the JSON file to netlist, e.g. example_twpa/twpa.json"
    )

    args = parser.parse_args()
    run_netlist_generator([args.file])
