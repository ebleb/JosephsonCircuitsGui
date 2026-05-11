from pathlib import Path


def resolve_source_target(script_dir, target):
    """Resolve a pipeline target from logic/ first, then app-level data/."""
    script_dir = Path(script_dir).resolve()
    target_path = (script_dir / target).resolve()
    if target_path.exists():
        return target_path

    data_path = (script_dir.parent / "data" / target).resolve()
    if data_path.exists():
        return data_path

    return target_path
