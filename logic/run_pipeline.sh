#!/usr/bin/env bash
set -euo pipefail

PYTHON_BIN="${PYTHON_BIN:-python3}"
CLASSIFIER_SCRIPT="${CLASSIFIER_SCRIPT:-classification.py}"
VARIABLE_PROPAGATION_SCRIPT="${VARIABLE_PROPAGATION_SCRIPT:-variable_propagation.py}"
X_MODE_SELECTION_SCRIPT="${X_MODE_SELECTION_SCRIPT:-x_mode_selection.py}"
MERGE_SCRIPT="${MERGE_SCRIPT:-merger.py}"
X_MERGE_SIMULATION_SCRIPT="${X_MERGE_SIMULATION_SCRIPT:-x_merge_simulation.py}"
X_REWRITE_SCRIPT="${X_REWRITE_SCRIPT:-x_rewrite.py}"
PORT_RESOLUTION_SCRIPT="${PORT_RESOLUTION_SCRIPT:-port_resolution.py}"
VALIDATION_SCRIPT="${VALIDATION_SCRIPT:-validator.py}"
NETLIST_SCRIPT="${NETLIST_SCRIPT:-netlist.py}"
SPECIALIZE_SCRIPT="${SPECIALIZE_SCRIPT:-specialize.py}"
SIMULATION_SCRIPT="${SIMULATION_SCRIPT:-simulation.py}"
X_SIMULATION_SCRIPT="${X_SIMULATION_SCRIPT:-x_simluation.py}"
PLOTTING_SCRIPT="${PLOTTING_SCRIPT:-plotting.py}"

PLOT=0

while getopts ":p:" opt; do
  case "$opt" in
    p)
      PLOT="$OPTARG"
      ;;
    *)
      echo "Usage: $0 [-p 1] <target-json> [target-json ...]" >&2
      exit 64
      ;;
  esac
done

shift $((OPTIND - 1))

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 [-p 1] <target-json> [target-json ...]" >&2
  exit 64
fi

require_script() {
  local script="$1"
  if [[ ! -f "$script" ]]; then
    echo "Missing required script: $script" >&2
    exit 66
  fi
}

run_stage() {
  local script="$1"
  shift
  require_script "$script"
  echo "=========================================="
  echo "STAGE: $script"
  echo "=========================================="
  if ! "$PYTHON_BIN" "$script" "$@"; then
    echo "ERROR: Stage '$script' failed with exit code $?" >&2
    echo "Pipeline aborted. Check the log above for details." >&2
    exit 1
  fi
  echo "SUCCESS: Stage '$script' completed"
  echo
}

REGULAR_PIPELINE=0
X_MERGE_SIMULATION=1
X_REWRITE=2
INVALID_X_SETTINGS=99

run_pipeline() {
  local target="$1"

  echo ""
  echo "##################################################"
  echo "# PIPELINE START"
  echo "# Target: $target"
  echo "##################################################"
  echo ""

  run_stage "$CLASSIFIER_SCRIPT" "$target"
  run_stage "$VARIABLE_PROPAGATION_SCRIPT" "$target"

  require_script "$X_MODE_SELECTION_SCRIPT"
  echo "==> $X_MODE_SELECTION_SCRIPT --print-code $target"
  NEXT_STEP_CODE=$("$PYTHON_BIN" "$X_MODE_SELECTION_SCRIPT" --print-code "$target")
  echo "    next_step_code=$NEXT_STEP_CODE"

  case "$NEXT_STEP_CODE" in
    "$REGULAR_PIPELINE")
      run_stage "$MERGE_SCRIPT" "$target"
      ;;
    "$X_MERGE_SIMULATION")
      run_stage "$X_MERGE_SIMULATION_SCRIPT" "$target"
      ;;
    "$X_REWRITE")
      run_stage "$X_REWRITE_SCRIPT" "$target"
      run_stage "$X_MERGE_SIMULATION_SCRIPT" "$target"
      ;;
    "$INVALID_X_SETTINGS")
      echo "Invalid X-parameter request for: $target" >&2
      target_dir=$(dirname "$target")
      if [[ "$target_dir" == "." ]]; then
        project_name="default_project"
      else
        project_name=$(basename "$target_dir")
      fi
      x_mode_file="outputs/$project_name/x_mode_selection.json"
      if [[ -f "$x_mode_file" ]]; then
        "$PYTHON_BIN" -c 'import json,sys; data=json.load(open(sys.argv[1])); print(data.get("reason", "")); [print(" - " + str(e)) for e in data.get("errors", [])]' "$x_mode_file" >&2
      fi
      exit 65
      ;;
    *)
      echo "Unknown next_step_code for $target: $NEXT_STEP_CODE" >&2
      exit 65
      ;;
  esac

  run_stage "$PORT_RESOLUTION_SCRIPT" "$target"
  run_stage "$VALIDATION_SCRIPT" "$target"
  run_stage "$NETLIST_SCRIPT" "$target"
  run_stage "$SPECIALIZE_SCRIPT" "$target"

  target_dir=$(dirname "$target")
  if [[ "$target_dir" == "." ]]; then
    project_name="default_project"
  else
    project_name=$(basename "$target_dir")
  fi

  x_mode_file="outputs/$project_name/x_mode_selection.json"
  if [[ -f "$x_mode_file" ]]; then
    USE_PATCHED_HBSOLVE=$("$PYTHON_BIN" -c 'import json,sys; data=json.load(open(sys.argv[1])); value=data.get("use_patched_hbsolve", False); print("1" if (value is True or str(value).lower() in {"1","true","yes","y","on"}) else "0")' "$x_mode_file")
  else
    USE_PATCHED_HBSOLVE=0
  fi

  if [[ "$USE_PATCHED_HBSOLVE" == "1" ]]; then
    run_stage "$X_SIMULATION_SCRIPT" "$target"
  else
    run_stage "$SIMULATION_SCRIPT" "$target"
  fi

  if [[ "$PLOT" == "1" ]]; then
    run_stage "$PLOTTING_SCRIPT" "$target"
  fi

  echo ""
  echo "##################################################"
  echo "# PIPELINE COMPLETE"
  echo "# Target: $target"
  echo "##################################################"
  echo ""
}

OUTPUT_DIR="${OUTPUT_DIR:-pipeline_outputs}"
mkdir -p "$OUTPUT_DIR"

safe_name() {
  echo "$1" | sed 's#[/[:space:]]#_#g; s#[^A-Za-z0-9._-]#_#g'
}

status=0

for target in "$@"; do
  log_file="$OUTPUT_DIR/$(safe_name "$target").log"
  echo "==============================================="
  echo "Processing: $target"
  echo "Log file: $log_file"
  echo "==============================================="
  if ( run_pipeline "$target" ) >"$log_file" 2>&1; then
    echo "✓ SUCCESSFULLY COMPLETED: $target"
    echo ""
  else
    exit_code=$?
    echo "✗ FAILED: $target (exit code: $exit_code)" >&2
    echo "  Log file: $log_file" >&2
    echo "  Check the log above or in: $log_file" >&2
    echo ""
    status=1
    # Don't continue with other targets if one fails - exit immediately
    exit "$exit_code"
  fi
done

if [[ "$status" -ne 0 ]]; then
  echo "One or more pipelines failed." >&2
  exit "$status"
fi

echo "All pipelines completed."
echo "Logs written to: $OUTPUT_DIR"
