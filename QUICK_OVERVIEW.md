# Circuit Project GUI V2 Quick Overview

This is the short orientation guide for the application. For full detail, read `USER_GUIDE.md` for day-to-day usage and `TECHNICAL_OVERVIEW.md` for implementation details.

## What The App Does

Circuit Project GUI V2 is a PyQt6 schematic editor and simulation front end for hierarchical microwave and Josephson circuits.

It lets users:

- create project folders with one JSON file per circuit cell;
- draw schematic cells from built-ins, local cells, and imported project cells;
- define pins, labels, variables, parameters, symbols, and simulation settings;
- import JosephsonCircuits Julia code into editable schematic cells;
- run the JSON-to-Julia simulation pipeline;
- inspect and plot S-parameter, HB, multimode, and X-parameter results.

Run it from the repository root:

```bash
python3 guiV2/main.py
```

## Main Files

- `guiV2/main.py`: main app window, canvas, inspectors, import/export, validation, simulation runner.
- `guiV2/gui_core.py`: shared data factories, constants, built-in loading, routing and geometry helpers.
- `guiV2/plot_widget.py`: result plotting.
- `logic/run_pipeline.sh`: shell entry point for the simulation pipeline.
- `logic/julia_hb_importer.py`: Julia source import and probing.
- `logic/built-in/`: built-in component catalog.
- `data/`: user projects and GUI-generated simulation runs.
- `references/`: optional reference outputs for comparisons.

## Project Model

A project is a folder:

```text
project.json
top.json
subcell.json
...
```

`project.json` stores metadata, imports, and GUI state. Each editable local cell is saved as a separate JSON file.

Project cells can be:

- `schematic`: editable graph of blocks, nets, pins, labels, variables, and simulation settings.
- `matrix`: direct matrix-defined reusable block.
- `generated_hb`: source-backed Julia/HB block with generated summary.

Imported project cells are read-only unless copied into the current project.

## GUI Mental Model

The center canvas edits a schematic cell. The left explorer opens cells and library items. The right inspector edits whatever is selected. The bottom panel shows messages, pipeline logs, and results.

Most work follows this loop:

1. Create or open a project.
2. Create schematic or matrix cells.
3. Place built-ins or subcells.
4. Wire ports into nets.
5. Add exported pins and labels.
6. Set variables and parameters.
7. Configure simulation setup.
8. Validate the cell.
9. Save.
10. Run simulation.
11. Inspect plots/results.

## Essential User Concepts

### Pins

Pins define the external interface of a schematic. Pin order is semantic: it becomes the external port order used by the pipeline.

### Labels

Labels name internal nets. They are useful for ground nodes, named internal nodes, and simulation-related nets.

### Ports

Ports are named strings in GUI/project JSON, such as `p1`, `p2`, or `"1"`. The pipeline converts them to integer ports later in `port_resolution.py`.

### Variables

Parameters can be numeric values or expressions. Symbolic identifiers become variables. A local numeric variable with `export: false` stops that variable from being exposed upward.

`w` is reserved for simulation angular frequency.

### HB Top Blocks

A schematic can be marked as an HB top block. HB pump/DC/harmonic settings live on the cell and can also be exposed on an instance when that HB top block is placed inside another schematic.

Two operating modes exist when an HB top block is placed inside another schematic:

- **Pre-computed S-parameter mode** (default): the parent treats the nested HB block as an already-solved S-matrix and does not merge it into its own simulation.
- **Merged simulation mode**: when the parent cell has `skip_hb_top_block_check: true`, all nested HB top blocks are treated as part of one larger combined simulation rather than as pre-computed S-matrices. The pipeline chooses X merge simulation when more than one HB top block is found in the hierarchy.

### X-Parameter Scope

X mode supports one X input, one X output, one pump, and one optional DC port.

## Julia Import In One Paragraph

The Julia importer accepts trusted JosephsonCircuits code, finds the actual `solveS(...)` or `hbsolve(...)` simulation calls, extracts circuit definitions, variables, frequency sweeps, HB settings, and dependency structure, then creates GUI schematic cells. Plotting and unrelated runtime code are ignored when the parser can identify the simulation hierarchy. Imported Julia schematics can be opened either as a normal schematic or as a code/settings view.

## Simulation Flow

When the user runs a simulation, the GUI:

1. Validates the active cell.
2. Creates a run folder under `data/guiV2_pipeline_runs/`.
3. Exports local and needed imported cells as pipeline JSON.
4. Runs `logic/run_pipeline.sh`.
5. Streams pipeline output into the GUI.
6. Reads cache manifests and CSV files from `logic/outputs/<run_id>/cache/`.
7. Stores result records in the project.
8. Opens them through the plotting UI.

The pipeline stages are:

1. classification
2. variable propagation
3. X-mode selection
4. merge or X rewrite/merge
5. port resolution
6. validation
7. netlist generation
8. specialization
9. Julia simulation

## Most Important Invariants

- Port names stay strings until `port_resolution.py`.
- Pin order defines the cell interface.
- Every instance UID must be unique inside a cell.
- Every instance `type_name` must resolve to a local cell, imported cell, or built-in.
- Parameters may be expressions; do not coerce user expressions into numbers.
- `w` is reserved.
- Imported project cells are read-only until copied locally.
- Generated Julia source metadata controls whether a Julia-imported cell can open in code/settings view.

## Common Problems To Check First

- Simulation port is not an exported pin.
- Pin names are duplicated or out of order.
- A block port is dangling.
- A wire endpoint references a missing port after a block was changed.
- A route crosses through a block.
- A parameter is empty or has mismatched parentheses.
- A referenced block name cannot be resolved.
- X mode is missing input/output/pump port settings.
- HB pump/DC settings are attached to the wrong top block or instance.
- Julia import preserved symbolic expressions but numerical defaults need review.

## Developer Change Checklist

When changing the app, check the same behavior in all relevant paths:

- in-memory GUI model;
- save and reopen project;
- import existing JSON;
- export to pipeline JSON;
- run simulation;
- reload/plot results.

When adding a cell field, update:

- `blank_cell()` if it belongs in new cells;
- inspector UI if users edit it;
- `export_pipeline_cell()`;
- `import_pipeline_cell()`;
- `serialize_project_cell()`;
- validation if the field affects correctness.

When touching ports, remember:

- GUI ports are strings;
- resolved pipeline ports are integers;
- schematic child interfaces resolve through `pins[].name`;
- built-ins resolve through `port_names`.

## Test Suite

The GUI includes a built-in regression test suite under `Simulate -> Run Test Suite`. It runs the full pipeline on each reference circuit in `reference_circuits/`, compares the cache CSVs against stored references in `logic/references/`, and reports pass/fail per test.

Tests compare numeric CSV values with an absolute tolerance of `1e-4`. Entries where both the reference and output values are below `1e-6` (numerical zero) are excluded from comparison. When a component has multiple specializations, the comparison matches each output entry against the best-matching reference entry to avoid cross-pairing different parameter variants.

## Where To Read Next

- User workflows: `USER_GUIDE.md`
- Internal architecture: `TECHNICAL_OVERVIEW.md`
- Pipeline stage details: `logic/run_pipeline.sh` and the stage modules in `logic/`

