# Circuit Project GUI V2 Technical Overview

This document describes how the application works under the hood. It is intended for engineers who need to modify the GUI, import logic, JSON adapters, or simulation pipeline.

## Repository Layout

Important paths:

```text
guiV2/
  main.py                 Main PyQt6 application, canvas, inspectors, project IO, simulation orchestration.
  gui_core.py             Shared constants, factories, catalog loading, routing helpers, geometry helpers.
  plot_widget.py          Matplotlib-based result plotting widget.
  README.md               Short GUI startup notes.
  GUI_FUNCTIONS.md        Existing function map.

logic/
  run_pipeline.sh         Shell orchestrator for the JSON-to-Julia pipeline.
  classification.py       Classifies cell hierarchy as S, HB, mixed, etc.
  variable_propagation.py Resolves variables through hierarchy.
  x_mode_selection.py     Decides regular pipeline, X merge, or X rewrite path.
  merger.py               Regular hierarchy merge/flattening.
  x_rewrite.py            Rewrites compatible S blocks into HB equivalents for X mode.
  x_merge_simulation.py   X-mode flattening and boundary handling.
  port_resolution.py      Converts named topology ports to integer ports.
  validator.py            Validates numeric topology and simulation fields.
  netlist.py              Generates port-order metadata and Julia netlist support.
  specialize.py           Creates parameter-specialized component JSONs.
  simulation.py           Generates/runs ordinary S/HB Julia simulations.
  x_simluation.py         Generates/runs X-parameter Julia simulations.
  plotting.py             Optional non-GUI plotting stage.
  julia_hb_importer.py    Julia source probing and reverse import into GUI/pipeline cells.
  built-in/               Built-in HB and S-parameter components.

data/
  <project>/              User projects.
  guiV2_pipeline_runs/    Temporary GUI simulation export folders.

references/
  <fixture>/              Reference outputs used for comparison and regression checks.
```

## Process Boundaries

The GUI and pipeline are deliberately separated.

The GUI:

- stores editable projects as GUI-friendly JSON;
- lets users draw schematics, edit symbols, define variables, and configure simulations;
- exports the active project to temporary pipeline JSON;
- launches `logic/run_pipeline.sh`;
- reads generated CSV/cache artifacts back into the project.

The pipeline:

- does not know about Qt;
- consumes JSON files from the temporary run folder;
- writes staged artifacts under `logic/outputs/<project_name>/`;
- generates Julia scripts;
- runs Julia and writes cache CSVs.

This separation is why the GUI has both import and export adapters.

## Main GUI Data Model

Project data is created by `blank_project()` in `guiV2/gui_core.py`.

Important project fields:

- `name`: project name.
- `path`: project folder path.
- `version`: metadata version.
- `default_cell`: preferred initial cell.
- `default_z0`: default impedance.
- `recent_cells`: recent local cells.
- `imports`: referenced project metadata.
- `importedCells`: read-only imported cells, grouped by alias.
- `cells`: local and temporary read-only cells loaded into this GUI session.
- `results`: collected simulation result records.
- `gui`: GUI state such as last open tabs.

Cells are created by `blank_cell()`.

Important cell fields:

- `id`: GUI session/project UUID.
- `name`: file and library name.
- `type`: `schematic`, `matrix`, or `generated_hb`.
- `description`
- `readOnly`
- `dirty`
- `instances`
- `nets`
- `pins`
- `labels`
- `variables`
- `z0`
- `simulation`
- `gui`

### Schematic Instances

Each placed block stores:

- `type_name`: referenced cell or built-in name.
- `uid`: unique block UID inside the cell.
- `parameters`: current parameter values.
- `parameter_defaults`: defaults copied from the library item.
- `parameter_order`: display/export order.
- `parameter_kinds`: positional, keyword, UID, etc.
- `position`: canvas coordinates.
- `port_names`: named interface ports.
- `port_count`: length of `port_names`.
- `rotation_degrees`
- `repeat_count`
- `repeat_connections`
- `symbol`
- `symbol_port_layout`
- `source`: local, built-in, or imported.
- `source_project`: imported project alias when relevant.
- optional HB settings copied from a referenced HB top block.

### Nets, Wires, Pins, Labels

The GUI internal topology is net-based.

Each net contains:

- `id`
- `endpoints`: block port references, `{instance_uid, port}`.
- `pins`: IDs of exported pin tags on that net.
- `labels`: IDs of text labels on that net.
- `route_segments`: visual wire segments.

Each route segment contains:

- `wire_id`
- `source`
- `target`
- `points`
- `auto`

Pipeline JSON uses point-to-point `wires[]`. The GUI converts between internal nets and exported wires in `export_pipeline_cell()` and `import_pipeline_cell()`.

Pins are stored as GUI tags with:

- `id`
- `name`
- `order`
- `net_id`
- `instance_uid`
- `port`
- `position`
- `display_visible`

Labels are stored similarly, without pin order.

## GUI Files And Responsibilities

### `guiV2/gui_core.py`

This file contains stable shared helpers:

- Constants: `ROOT`, `LOGIC_DIR`, `DATA_DIR`, `PIPELINE_DATA_DIR`, `DEFAULT_BUILTINS_DIR`, `RESERVED_VARIABLE_NAMES`.
- Data factories: `blank_project()`, `blank_cell()`.
- Naming: `clean_name()`.
- Built-in loading: `load_builtin_catalog()`.
- Project summary helpers.
- Symbol helpers: `default_symbol()`, `repair_common_port_symbol()`.
- Geometry and routing helpers: `port_point()`, `port_side()`, `block_rect()`, `routed_orthogonal_points()`, `compact_points()`, etc.
- Coercion helpers: `truthy()`, `as_list()`, `first_value()`, `repeat_count_value()`, `parse_variables_text()`.
- The `Selection` dataclass.

### `guiV2/main.py`

This is the central application file. It owns:

- Qt graphics item classes: `PortItem`, `BlockItem`, `WireItem`, `TagItem`, `ValidationMarker`.
- Worker threads: `_JuliaProbeThread`, `_JuliaImportThread`.
- Generated-cell panel: `GeneratedCellWidget`.
- Canvas: `SchematicView`.
- Palette and startup dialogs.
- Main window: `MainWindow`.

`MainWindow` handles:

- menus and shortcuts;
- project lifecycle;
- cell lifecycle;
- imported project references;
- schematic editing;
- routing;
- inspector panels;
- validation;
- pipeline export/import;
- Julia import;
- simulation process management;
- result collection and plotting.

### `guiV2/plot_widget.py`

`PlotWidget` wraps Matplotlib for result display. It loads stored result CSV text plus optional sidecar files from the simulation cache.

Supported plot families include:

- ordinary S-matrix magnitude/phase;
- multimode output curves;
- signal/idler curves;
- multimode power bars;
- multimode diagnostics;
- X default transfer magnitude/phase;
- focused XS/XT transfer plots;
- S versus XS comparison;
- XFB magnitude/phase;
- generic curves for unknown CSVs.

## Built-In Catalog

Built-ins are JSON files under `logic/built-in/`.

Built-in groups:

- `logic/built-in/hbsolve/`: JosephsonCircuits/HB primitives such as `L`, `C`, `R`, `P`, `GND`, `Lj`, `Cj`, `K`, `NL`, `I`.
- `logic/built-in/ssolve/abcd/`: ABCD-style S-parameter blocks.
- `logic/built-in/ssolve/s/`: direct S-matrix blocks.

`load_builtin_catalog()` reads these files and builds palette/library items with:

- name,
- source,
- path,
- group,
- port names/count,
- variables,
- symbol.

The GUI also exposes local cells and imported cells through `library_items()`, so they can be placed using the same instance workflow as built-ins.

## Project Load And Save

### Loading

`MainWindow.load_project_dir()`:

1. Creates a blank project from the folder name.
2. Reads `project.json` if present.
3. Loads each sibling JSON cell except backups/autosaves/project metadata.
4. Converts pipeline/project JSON into GUI cells with `import_pipeline_cell()`.
5. Reloads imported projects.
6. Opens the default cell or the first available cell.
7. Restores last open tabs.
8. Refreshes explorer, tabs, inspector, canvas, and file watcher.

Only raw cell types in `{"schematic", "matrix", "generated_hb"}` are loaded.

### Saving

`save_project_metadata()` writes `project.json`.

`save_current_cell()` and `save_all()` write local non-read-only cells using `serialize_project_cell()`.

For normal schematic/matrix cells, `serialize_project_cell()` delegates to `export_pipeline_cell()`. For `generated_hb`, it writes a special generated-source cell format.

Reverse-imported Julia schematics now preserve:

- `generated_source`
- `generated_from`
- `generated_language`

This metadata is required for the code/schematic opening prompt.

## Canvas Rendering And Editing

`SchematicView.draw()` rebuilds the scene:

1. Clears prior graphics items.
2. Draws grid if enabled.
3. Draws wires and route segments.
4. Draws junctions.
5. Draws blocks and ports.
6. Draws pins and labels.
7. Draws validation markers.
8. Draws placement or wire previews.

The canvas uses `QGraphicsScene`/`QGraphicsView`.

### Placement

When placing an instance:

1. The user chooses a library item.
2. `pending_instance` is set.
3. The canvas shows a preview at the snapped cursor position.
4. On click, `place_instance()` builds a GUI instance from the library item.
5. It chooses a unique UID, copies parameters/symbol/ports, and optionally copies HB settings from HB top-block definitions.
6. It checks block collision.
7. It marks the cell dirty and redraws.

### Wiring

Wire mode stores a pending source endpoint. When the user selects a target endpoint or existing net:

- `finish_wire()` creates or merges nets.
- `finish_wire_to_net()` attaches a port to an existing net.
- `auto_route_points()` computes a routed path around obstacles.
- route endpoints are kept aligned when blocks move.

The GUI model is net-centric, while exported pipeline JSON is wire-centric.

### Selection

The selected object is represented by `Selection(kind, id)`. Kinds include instances, nets, wires, pins, labels, and other tag-like objects.

The inspector populates based on the current selection.

## Inspector Logic

`refresh_inspector()` delegates to:

- `populate_cell_inspector()`
- `populate_instance_inspector()`
- `populate_net_inspector()`
- `populate_tag_inspector()`

The inspector is not just display; it writes directly into the model. Most edits call `mark_dirty()`, and structural edits generally call `record_undo()` first.

### Cell Inspector

Shows:

- name,
- description,
- `z0`,
- pins,
- simulation summary,
- simulation setup button,
- HB top-block controls,
- port order editor,
- matrix/generated-HB controls,
- variable table.

### Instance Inspector

Shows:

- UID,
- type,
- source,
- parameters,
- exposed variables,
- repeat settings,
- symbol controls,
- block opening/replacement/reset/delete actions.

If the instance references a cell marked as `hb_top_block`, the inspector copies HB settings onto the instance and exposes an `HB Instance Simulation Settings` button.

## Variables And Parameter Propagation

The GUI and pipeline both support symbolic parameter expressions.

Key concepts:

- `w` is reserved for simulation angular frequency.
- Numeric-looking parameters are treated as local concrete values.
- Non-numeric expressions are scanned for identifiers.
- Identifiers not already defined become inferred variables.
- Variables can be propagated upward through cell instances.
- A local variable with `export: False` prevents propagation farther upward.

Important GUI functions:

- `_collect_propagated_vars()`: finds variables used inside a referenced subcell.
- `inferred_cell_variables()`: builds the exported variable list from explicit variables plus identifiers found in parameters.
- `add_cell_variable()`: adds local variables with `scope: "cell"` and `export: False`.
- `library_items()`: filters out variables where `export` is false when exposing a cell to a parent.

This behavior is especially important for Julia import: the importer preserves symbolic component expressions but also imports numerical defaults from `circuitdefs` and assignments.

## Simulation Settings Model

The GUI stores simulation setup under each cell's `simulation` object:

```json
{
  "mode": "s",
  "input_ports": [],
  "output_ports": [],
  "freq_start": 2.0,
  "freq_stop": 20.0,
  "freq_points": 200,
  "sweep_type": "linear",
  "figure_title": "...",
  "x": {
    "input_port": "",
    "output_port": "",
    "pump_port": "",
    "pump_frequency": 7.12,
    "pump_current": "1.85e-6",
    "dc_port": "",
    "dc_current": "",
    "modulation_harmonics": 10,
    "pump_harmonics": 20,
    "threewave_mixing": true,
    "fourwave_mixing": true
  }
}
```

HB-specific settings are stored as top-level fields on cells and instances:

- `hb_top_block`
- `skip_hb_top_block_check`
- `hb_pump_ports`
- `hb_pump_frequencies`
- `hb_pump_currents`
- `hb_dc_ports`
- `hb_dc_currents`
- `hb_modulation_harmonics`
- `hb_pump_harmonics`
- `hb_threewave_mixing`
- `hb_fourwave_mixing`

These top-level fields map directly to the pipeline JSON contract.

## GUI Validation

`validate_cell()` performs local checks before simulation.

For normal schematics it checks:

- valid cell name;
- duplicate local cell names;
- duplicate UIDs;
- port count consistency;
- repeat count;
- missing references;
- symbol/port mismatches;
- missing/malformed parameters;
- dangling nets;
- missing instances or ports in endpoints;
- bad route endpoints;
- routes crossing blocks;
- dangling block ports;
- pin name uniqueness and contiguous order;
- invalid pin/label net IDs;
- simulation port validity;
- positive `z0`;
- frequency start/stop/points;
- X-mode required ports;
- empty schematic warning.

For `generated_hb`, validation is simpler:

- source exists;
- exported ports exist;
- `z0` is positive.

GUI validation is intentionally earlier and friendlier than pipeline validation, but pipeline stages still perform stricter structural checks after export.

## Pipeline Export Adapter

`export_pipeline_cell()` converts GUI cells to pipeline input JSON.

For schematic cells, it exports:

- `name`
- `type`
- `instances`
- `wires`
- `pins`
- `labels`
- `variables`
- `z0`
- simulation fields such as `simulation_input_ports`, `simulation_freq_start`, etc.
- HB fields
- X fields when `simulation.mode == "x"`
- `gui.wire_routes`
- `reference`
- `generated_source` metadata when present.

### Net-To-Wire Export

GUI nets can have more than two endpoints. Pipeline input uses `wires[]`.

The exporter builds point-to-point wires from route segments where possible. It also uses pins and labels to select representative endpoints for exported tags.

Labels are exported with:

- `name`
- `position`
- `net_source_uid`
- `net_source_port`

Pins are exported with:

- `name`
- `instance_uid`
- `port`

### Instance Export

For each instance, export preserves:

- `type_name`
- `uid`
- `parameters`
- `parameter_defaults`
- `parameter_order`
- `parameter_kinds`
- `position`
- `port_count`
- `port_names`
- `rotation_degrees`
- `repeat_count`
- `repeat_connections`
- `symbol_port_layout`
- `symbol`
- instance-level HB settings when present.

If an instance came from an imported project, `type_name` may be exported as `alias/type_name`.

## Pipeline Import Adapter

`import_pipeline_cell()` converts pipeline/project JSON into GUI cells.

It handles:

- normal schematic cells;
- matrix cells;
- `generated_hb` cells;
- instance symbol repair;
- imported project path prefixes in `type_name`;
- wires to internal nets;
- GUI wire route restoration;
- pins and labels attached to nets;
- simulation settings;
- HB settings;
- X settings;
- generated Julia source metadata.

The conversion builds a union-find over wire endpoints so multiple point-to-point wires that share connectivity become a single GUI net.

Label positions of `[0, 0]` are treated as missing/placeholder positions and are moved near the anchor endpoint.

## Julia Reverse Import

`logic/julia_hb_importer.py` contains two related systems:

1. Older generated-cell probing for `generated_hb`.
2. New reverse import that creates ordinary schematic cells from Julia source.

The GUI runs reverse import through `_JuliaImportThread`, which calls:

```python
import_julia_simulation_hierarchy(source, name_hint=name)
```

### Goals

The reverse importer tries to accept large pasted Julia files and extract the actual simulation hierarchy rather than importing plotting or analysis code.

It supports:

- `solveS(...)`
- `hbsolve(...)`
- circuit tuple arrays;
- `circuitdefs = Dict(...)`;
- assignments for frequency sweeps, pump/DC sources, and harmonics;
- Julia functions that build networks/connections;
- dependent solve calls;
- parallel solve branches;
- direct S-matrix function bodies;
- HB source probing.

### High-Level Reverse Import Flow

`import_julia_simulation_hierarchy()`:

1. Calls `parse_solve_call_cells(source)`.
2. If cells were extracted, returns them with an import summary.
3. If no cells were extracted and the source appears HB-like, probes Julia source and materializes the result into a schematic.
4. Raises an error if no simulation hierarchy can be extracted.

`parse_solve_call_cells()`:

- strips or ignores comments/runtime sections where possible;
- finds relevant solve calls;
- finds Julia function blocks and dependencies;
- builds direct S cells, reverse solve cells, or wrapper cells;
- records skipped functions and reasons.

### Reverse-Imported Schematic Cells

`build_reverse_solve_cell()` creates a pipeline-style schematic with:

- built-in-like instances using the same component naming as GUI-created schematics;
- wires derived from circuit/network connections;
- pins derived from P blocks and solve ports;
- labels for ground and non-ground nodes;
- variables from function parameters, `@variables`, assignments, and `circuitdefs`;
- simulation settings from assignments and call arguments;
- HB settings for `hbsolve`;
- `generated_from: "julia_reverse_import"`;
- `generated_source`.

The GUI then imports this pipeline-style cell using `import_pipeline_cell()`.

### Layout And Labels

The importer assigns positions automatically and normalizes layout with a minimum margin. Coupling components such as `K` are positioned separately from ordinary two-port blocks. Node labels are added automatically for both ground and non-ground nodes where enough topology information exists.

The output should use the same schematic component representation as normal user-created cells, so imported schematics can be edited and exported like hand-built schematics.

### Julia Source View Prompt

Reverse-imported schematics preserve `generated_source`. `MainWindow.is_julia_source_schematic_cell()` detects these cells. When opened, `_refresh_view()` asks whether to show:

- the normal schematic canvas, or
- `GeneratedCellWidget`, which displays the Julia source, settings, and variables.

This prompt is triggered by:

- import completion;
- tab activation;
- explorer double-click;
- explorer context-menu open;
- opening a selected block;
- copying imported generated cells.

## Generated HB Source Path

`generated_hb` cells are not normal schematics. They store:

- trusted Julia source,
- generated summary,
- display components,
- pins,
- variables,
- HB settings.

`GeneratedCellWidget` displays their code/settings and, when possible, a generated schematic preview. Editing source or regenerating summary uses `_JuliaProbeThread`, which calls `probe_julia_source()`.

For pipeline export, `export_pipeline_cell()` calls `materialize_generated_hb_cell()` so the generated HB source becomes ordinary pipeline-compatible schematic JSON.

## Simulation Pipeline

The GUI starts the pipeline with:

```python
process.start("bash", [str(LOGIC_DIR / "run_pipeline.sh"), str(rel)])
```

The working directory is `logic/`, and `rel` is the temporary target path relative to `data/`.

`logic/run_pipeline.sh` runs:

1. `classification.py`
2. `variable_propagation.py`
3. `x_mode_selection.py --print-code`
4. Depending on the next-step code:
   - `merger.py`, or
   - `x_merge_simulation.py`, or
   - `x_rewrite.py` then `x_merge_simulation.py`
5. `port_resolution.py`
6. `validator.py`
7. `netlist.py`
8. `specialize.py`
9. `simulation.py` or `x_simluation.py`
10. `plotting.py`, only if `-p 1` was passed.

The GUI calls the pipeline without `-p 1` and handles plotting itself.

### Stage Outputs

For a target under a temporary folder named `guiV2_pipeline_<id>`, pipeline outputs are written under:

```text
logic/outputs/guiV2_pipeline_<id>/
```

Important stage directories:

- `classification_memo.json`
- `resolved_variables/`
- `x_mode_selection.json`
- `merged/`
- `resolved_ports/`
- `validated/`
- `netlisted/`
- `specialized/`
- `cache/`
- `plots/` if plotting is enabled.

### Classification

`classification.py` resolves component references and classifies each cell. It distinguishes built-ins, schematic cells, HB trees, S-parameter trees, and mixed cases.

Component lookup is path-sensitive:

- Plain `type_name` values are resolved locally first, then under `built-in/`.
- Slash-containing references are interpreted as path-like project references.

When a cell contains instances whose definitions carry `hb_top_block: true`, the classifier normally treats those instances as pre-computed S-parameter blocks (the nested HB top block is already fully solved). The parent cell is then classified as `sparam_block` or `mixture`.

If the parent cell itself carries `skip_hb_top_block_check: true`, the classifier instead treats nested `hb_top_block` instances as ordinary HB blocks participating in the parent's own solve. This allows multiple HB top blocks to be merged into one combined simulation. The `hb_top_count` metric records how many `hb_top_block: true` nodes are present in the structural dependency tree (counted with repetition, not by unique file).

### Variable Propagation

`variable_propagation.py` resolves variables through hierarchy, substitutes instance parameters where appropriate, and prepares resolved-variable JSONs.

This stage expects topology ports to be named strings.

### X Mode Selection

`x_mode_selection.py` decides whether the target uses:

- regular pipeline,
- X merge simulation,
- X rewrite then X merge,
- invalid X settings.

The decision is written to `x_mode_selection.json`.

The priority order for the non-regular paths is:

1. `hb_top_count > 1` → `X_MERGE_SIMULATION`. Multiple HB top blocks in the hierarchy must always be merged into one simulation; this check takes priority.
2. `sparam_block + has_hb` → `X_REWRITE`. A top-level S-parameter block that contains HB elements is rewritten then merged.

This ordering matters for circuits such as `double_pumped_JPA` where the parent uses `skip_hb_top_block_check` and both the parent and child carry `hb_top_block: true`, giving `hb_top_count = 2`. Without the priority rule, the check on `sparam_block` would incorrectly choose `X_REWRITE`.

### Merge And X Rewrite/Merge

`merger.py` handles regular flattening and HB collapse.

`x_rewrite.py` converts compatible S-parameter built-ins into equivalent HB networks when X-parameter simulation requires it.

`x_merge_simulation.py` handles X-specific flattening, boundary P blocks, duplicate z0 shunt cleanup, and X port field mapping.

### Port Resolution

`port_resolution.py` is the single intended boundary where topology ports change from names to integers.

Before this stage:

- `wire.source_port`
- `wire.target_port`
- `pin.port`
- `label.port`

should be strings.

After this stage, they must be integers. Numeric-looking strings such as `"1"` are valid named ports before resolution and must not be coerced early.

### Validation

`validator.py` validates numeric topology after port resolution. It checks flattened primitives, HB requirements, P-block exposure, and S-parameter topology.

### Netlist

`netlist.py` generates Julia port-order metadata and helper outputs in `netlisted/`.

### Specialization

`specialize.py` creates parameter-specialized component files. Specialized names look like:

```text
Component__p_<hash>.json
```

The `__p_<hash>` suffix is a deterministic hash of the component's specialization identity: original type name, structural child data (topology only, not placement), and resolved parameter values. Variable order in the child data is sorted before hashing so that declaration order in the JSON file does not affect the identity.

#### `raw_parameters`

Instance objects carry both `parameters` (current resolved values) and `raw_parameters` (the original unsubstituted expressions from the schematic). The specialize stage uses `raw_parameters` as its starting point for re-resolving parameters in each clone's variable environment, rather than the already-substituted `parameters` field. This is necessary because a shared pre-specialization JSON may reflect one parent's variable substitutions, while other parents need fresh substitution.

When `substitute_expr(raw_value, env)` returns the input unchanged — meaning no variables from the current environment were resolved — the stage falls back to the existing `parameters` value. This preserves any substitution the merger already performed at a deeper hierarchical level, rather than reverting to an unresolved symbolic expression.

#### Hash Stability

The specialization hash covers only the component's structural identity, not formatting details. In particular:

- Variable lists are sorted by name before hashing so file-order differences do not produce different hashes for the same effective parameters.
- The `uid`, `position`, and `rotation_degrees` fields are excluded from the identity.
- Specialized child references are reduced back to their original type stem so nested specialization order is not visible in ancestor hashes.

### Simulation

`simulation.py` generates Julia scripts for:

- direct built-in S-parameter matrices;
- regular `solveS` network composition;
- HB `hbsolve` circuits;
- repeated HB groups;
- multimode sidecars when enabled.

`x_simluation.py` generates X-parameter HB scripts and sidecars:

- compatibility S CSV,
- `*_x_XFB.csv`,
- `*_x_XS_full.csv`,
- `*_x_XT_full.csv`,
- `*_x_modes.json`.

Both simulation paths write cache manifests under `cache/`.

## GUI Simulation Orchestration

`MainWindow.run_simulation()`:

1. Validates the active cell.
2. Creates `data/guiV2_pipeline_runs/guiV2_pipeline_<random>/`.
3. Calls `export_all_local_cells()`.
4. Adds needed read-only imported cells.
5. Applies `skip_hb_top_block_check` patching if requested.
6. Writes every exported cell JSON to the temp folder.
7. Starts `logic/run_pipeline.sh` through `QProcess`.
8. Stores the process, target path, and active cell name by run ID.

The app supports concurrent simulation processes. `simulation_processes`, `simulation_targets`, and `simulation_cells` are dictionaries keyed by run ID.

`append_simulation_output()` prefixes stdout/stderr lines with the run ID and appends them to the bottom messages panel.

`simulation_finished()` removes the process from tracking and calls `collect_results()` on success.

## Result Collection

`collect_results()` reads:

- `cache/x_cache_manifest.json`
- `cache/cache_manifest.json`
- `*.csv` files when manifests are missing.

It creates project result records:

- `name`
- `cell`
- `rows`
- `csv`
- `source_path`
- `absolute_path`
- `kind`: `s` or `x`
- `manifest_key`
- `ref_status`
- `run_id`

CSV normalization adds headers when the file is headerless numeric data.

Reference comparison looks under `references/`, finds matching manifests, and compares CSV arrays with NumPy using a default tolerance of `1e-4`. Entries where both the output value and the reference value are below `1e-6` in absolute magnitude are excluded from the comparison; these are numerically zero and their exact value is simulation noise (for example, S11 at −4000 dB versus −6000 dB).

## Result Plotting Internals

`open_result_plot()` creates a dialog containing:

- a plot type combo box;
- a `PlotWidget`;
- curve visibility list;
- Matplotlib navigation toolbar;
- Y-range controls;
- export buttons.

`PlotWidget` loads the stored result CSV and uses sidecar files referenced by the original absolute cache path. It redraws when plot type or visibility changes.

## File Watching And Autosave

The app tracks loaded cell file modification times. File watcher refresh and autosave are managed by:

- `setup_file_safety()`
- `refresh_file_watcher()`
- `autosave_dirty_cells()`
- `on_file_changed()`

Autosave files are ignored on normal project load.

## Undo/Redo

Undo and redo are cell-scoped snapshots.

Important functions:

- `record_undo()`
- `current_cell_snapshot()`
- `push_undo_snapshot()`
- `restore_cell_snapshot()`
- `undo()`
- `redo()`

The app serializes the active cell to JSON for snapshots. Many structural edits call `record_undo()` before mutating the model. Some small text edits mark dirty without pushing a snapshot for every keystroke.

## Read-Only Imported Cells

Imported project cells are loaded into `project["importedCells"]` and can be opened as read-only copies in `project["cells"]` with IDs like:

```text
import:<alias>:<cell_id>
```

When placed as blocks, imported instances store:

- `source: "imported:<alias>"`
- `source_project: <alias>`
- `type_name: <cell name>`

When exported to the pipeline, imported references may become `alias/type_name` so pipeline component resolution can find them.

## HB Top Block Instances

The app supports cells marked as HB top blocks being placed into other cells.

Mechanism:

- `library_items()` includes HB settings in local/imported cell library entries.
- `place_instance()` and `replace_selected_block()` copy HB settings onto the instance.
- `populate_instance_inspector()` detects referenced HB top blocks and exposes instance-level HB settings.
- `export_pipeline_cell()` preserves HB settings on instances.

This lets a parent cell carry the HB simulation setup of a nested imported/generated block.

## X-Parameter Support

The GUI stores X settings under `cell["simulation"]["x"]` and exports them as top-level pipeline fields when `simulation.mode == "x"`:

- `x-params`
- `x_input_ports`
- `x_output_ports`
- `x_pump_ports`
- `x_pump_frequencies`
- `x_pump_currents`
- `x_dc_ports`
- `x_dc_currents`
- `x_modulation_harmonics`
- `x_pump_harmonics`
- `x_threewave_mixing`
- `x_fourwave_mixing`

Supported X-parameter scope:

- one X input;
- one X output;
- one pump;
- one optional DC bias.

The X pipeline may choose rewrite or merge behavior depending on cell classification and built-in capabilities.

## Important Invariants

### Named Ports Before Port Resolution

GUI and early pipeline topology must use string port names. Integer topology ports before `port_resolution.py` are considered a stage leak.

### Pin Order Defines External Interface

For schematic cells, external port numbering is defined by `pins[]` order. The GUI stores an explicit `order` field and validates contiguous ordering.

### Component Names Must Resolve

Every instance `type_name` must resolve to a local cell, imported cell, or built-in JSON. Ambiguous names can produce surprising behavior because local names can shadow built-ins.

### `w` Is Reserved

`w` is reserved for simulation frequency. The GUI blocks manual cell variables named `w`.

### Generated Source Metadata Controls Code View

Reverse-imported Julia schematic cells are detected by non-empty `generated_source`. If that metadata is missing, the GUI treats the cell as an ordinary schematic.

## Implementation Notes

These are areas to treat carefully when editing the code:

- `guiV2/main.py` is large and tightly coupled; changes in inspector/export/import often interact.
- Net-to-wire export is lossy if arbitrary manual route information does not map cleanly to point-to-point wires.
- Pin order is semantic and easy to disturb during import/export.
- Julia reverse import is text-based with targeted parsers, plus Julia source probing. Very dynamic Julia may not import exactly.
- HB top-block nesting has two operating modes. Default mode treats nested `hb_top_block` cells as pre-computed S-matrices. Setting `skip_hb_top_block_check: true` on the parent makes the pipeline merge all nested HB blocks into one combined simulation. Both modes must work correctly; the classification and x-mode selection stages both read this flag.
- `raw_parameters` on instances store original unsubstituted expressions. The merger and variable propagation stages may resolve `parameters` further down the hierarchy. If `specialize.py` tries to re-resolve `raw_parameters` in a context that lacks those variables, it must fall back to the already-resolved `parameters` value, not revert to the raw expression.
- X-parameter support is scoped to one X input, one X output, one pump, and one optional DC bias.
- Generated temp data and pipeline outputs can accumulate quickly.
- Pipeline stages assume ports remain strings until `port_resolution.py`.
- Reference comparison matches by exact `__p_<hash>` key first, then falls back to best-error matching for components whose hash changed between pipeline versions.

## Maintenance Tips

### When Changing The GUI Model

Check all three paths:

1. live in-memory model;
2. project save/load;
3. pipeline export/import.

A field added only to the in-memory cell may disappear after save/reopen or after simulation export.

### When Changing Import Logic

Test:

- direct project JSON import;
- project open from disk;
- Julia reverse import;
- generated HB source blocks;
- imported project references;
- copy imported cell locally.

### When Changing Simulation Settings

Update:

- `blank_cell()`;
- inspector dialogs;
- `export_pipeline_cell()`;
- `import_pipeline_cell()`;
- validation;
- any relevant pipeline stage.

### When Changing Ports

Remember:

- GUI and input JSON use named string ports.
- `pins[]` order matters.
- pipeline-resolved JSON uses integers.
- built-in primitives map by `port_names`.
- child schematics map by `pins[].name`.

### When Changing Variables

Review:

- cell variable table;
- instance exposed variables;
- `inferred_cell_variables()`;
- `library_items()` variable filtering;
- `variable_propagation.py`;
- Julia import defaults.

## GUI Test Suite

`TestSuiteDialog` (under `Simulate -> Run Test Suite`) provides a built-in regression harness. It runs each entry in `_TEST_MANIFEST`, which lists 15 test cases covering S-parameter, HB, X-parameter, repeated, and multi-block simulation paths.

Each test:

1. Copies the corresponding `reference_circuits/<dir>/` folder into a fresh temp dir.
2. Runs the full pipeline on the specified target JSON.
3. Compares all cache CSVs against stored references in `logic/references/<test_name>/`.

Manifest key matching works as follows:

- Exact `__p_<hash>` key match is tried first. Because the parameter hash is a deterministic function of the effective parameters, identical pipelines produce identical keys.
- If the exact key is not in the reference (hash changed due to a code update), a best-error fallback is used: for each unmatched output entry, the reference entry with the same base component name that gives the minimum comparison error is selected. This prevents false failures when the hash computation changed but the actual numerical results are correct.

## Suggested Tests

At minimum, after a change in this application, run:

```bash
python -m py_compile guiV2/main.py guiV2/gui_core.py guiV2/plot_widget.py logic/julia_hb_importer.py
```

For pipeline changes, run representative examples through:

```bash
cd logic
./run_pipeline.sh ../data/example_add_drop/add_drop.json
./run_pipeline.sh ../data/example_twpa/twpa.json
./run_pipeline.sh ../data/test_fixtures_x/x_existing_shunt.json
```

Use examples that cover:

- pure S-parameter hierarchy;
- regular HB hierarchy;
- reverse-imported Julia schematic;
- generated HB source blocks;
- X rewrite;
- X merge;
- imported project references;
- matrix cells;
- numeric-looking string ports;
- repeated HB groups.
