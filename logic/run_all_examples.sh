#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIPELINE_SCRIPT="$SCRIPT_DIR/run_pipeline.sh"

if [[ ! -f "$PIPELINE_SCRIPT" ]]; then
  echo "Missing run_pipeline.sh at: $PIPELINE_SCRIPT" >&2
  exit 66
fi

# Hardcoded targets
TARGETS=(
  example_full/first_half.json
  example_add_drop/add_drop.json
  example_twpa/twpa.json
  example_two_twpa_x/two_twpas_series.json
  example_twpa_x/twpa.json
  example_add_drop_x/add_drop.json
  example_full_setup_x/first_half.json
  test_fixtures_numeric_ports/MalformedChild.json
  test_fixtures_numeric_string_ports/numeric_string_ports.json
  test_fixtures_x/x_dc_pump_real_shunt_conflict.json
  test_fixtures_x/x_dc_pump_reactive_shunt.json
  test_fixtures_x/x_dc_minimal_reactive_shunt.json
  test_fixtures_x/x_existing_shunt.json
  test_fixtures_x/x_no_existing_shunt.json
)

echo "Running pipeline for ${#TARGETS[@]} targets..."

# Forward all CLI args (like -p 1) directly
"$PIPELINE_SCRIPT" "$@" "${TARGETS[@]}"