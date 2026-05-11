# Circuit Project GUI V2 User Guide

This guide explains how to use the PyQt6 circuit project editor in this repository. It focuses on the workflows a user sees: creating projects, drawing schematics, importing Julia code, configuring simulations, running the pipeline, and reading results.

The app is a graphical front end for hierarchical microwave and Josephson circuit simulation. It stores editable project data as JSON files, exports those files into the simulation pipeline under `logic/`, runs that pipeline, and displays the resulting S-parameter, HB, multimode, and X-parameter CSV outputs.

## Starting The App

Run the GUI from the repository root:

```bash
python3 guiV2/main.py
```

If PyQt6 is missing, install the GUI requirements first:

```bash
python3 -m pip install -r guiV2/requirements.txt
```

The app expects to be run from this repository because it uses relative paths to:

- `logic/` for the simulation pipeline and built-in component catalog.
- `data/` for normal project folders.
- `data/guiV2_pipeline_runs/` for temporary GUI simulation exports.
- `references/` for optional reference-result comparisons.

## Project Structure

A project is a folder. A typical project folder contains:

```text
project.json
top.json
subcell_a.json
subcell_b.json
...
```

`project.json` stores project metadata, recent/open tabs, imported project references, and GUI settings. Each local cell is saved as a separate JSON file next to it.

The GUI ignores:

- `project.json` when loading cells.
- files ending in `.bak`.
- autosave files containing `.autosave.`.

Imported project cells are read-only references. They are loaded into memory from the referenced project, but they are not edited directly unless copied into the current project.

## Project Startup

When the app starts, use the project startup dialog to:

- Open a recent project.
- Choose an existing project folder.
- Create a new project folder.

The app also checks generated data locations and may offer to clear generated pipeline runs. These generated runs can become numerous because every GUI simulation uses a separate temporary folder.

## Main Window Layout

The main window is split into four main areas:

- Project Explorer on the left: local cells, imported projects, built-ins, and results.
- Canvas in the center: schematic drawing and editing.
- Inspector on the right: properties for the selected cell, block, net, pin, or label.
- Bottom panel: debug messages, pipeline output, and simulation results.

The View menu can show or hide the explorer, inspector, bottom panel, grid, and snap-to-grid.

## Common Keyboard Shortcuts

- `i`: add instance from the palette.
- `w`: wire mode.
- `p`: add exported pin.
- `l`: add text label.
- `q`: show properties.
- `o`: open selected block.
- `Esc`: leave the current mode.
- `Del`: delete selected item.
- `Ctrl+S`: save current cell.
- `Ctrl+Z`: undo.
- `Ctrl+Y` or `Ctrl+Shift+Z`: redo.
- `Ctrl+C`: copy.
- `Ctrl+V`: paste.
- `Ctrl+A`: select all.

## Cell Types

The GUI supports several cell types.

### Schematic Cells

Schematic cells are the main editable circuit diagrams. A schematic contains:

- Instances: placed blocks.
- Nets: connectivity groups.
- Wires: visible route segments between endpoints.
- Pins: exported ports of the cell.
- Labels: named internal nets.
- Variables: local defaults or exposed parameters.
- Simulation settings.

Use schematic cells for most user-built circuits.

### Matrix Cells

Matrix cells represent direct matrix-defined components. They have:

- A matrix type: `ABCD`, `S`, `Y`, or `Z`.
- Port names.
- Matrix values.
- Optional variables.

They behave like library components when placed into a schematic.

### Julia-Generated Cells

Julia import can create schematic cells from pasted JosephsonCircuits code. These cells should look and behave like normal GUI-created schematics, but they retain the original Julia source as metadata.

When opening a Julia-generated schematic, the GUI asks whether to open:

- the schematic view, or
- the code/settings view.

### Generated HB Blocks

Generated HB blocks store trusted Julia source and a generated summary. They can be edited through the generated code panel and materialized for pipeline export.

## Creating Cells

Use the Cell menu or the explorer context menu.

### Create Schematic Cell

Choose `Cell -> Create Schematic Cell...`.

You can set:

- Cell name.
- Description.
- Default `z0`.
- Initial number of ports.
- Whether to open immediately.

The initial ports are exported pins. Pin order matters because it defines the external port order of the cell.

### Create Matrix Cell

Choose `Cell -> Create Matrix Cell...`.

Set:

- Cell name.
- Description.
- `z0`.
- Port names.
- Matrix type.
- Matrix values.
- Optional variables.

Matrix cells are useful for compact analytic blocks or imported network descriptions.

### Import Julia Circuit Block

Choose `Cell -> Create Julia HB Block...`.

Paste trusted JosephsonCircuits Julia code. The importer looks for actual simulation calls such as `solveS(...)` and `hbsolve(...)`, extracts the circuit definitions, variables, frequency settings, HB settings, and simulation hierarchy, and creates one or more GUI cells.

Important:

- The import dialog has a trust checkbox because HB source probing may execute Julia code.
- Plotting code and irrelevant runtime code are intentionally ignored where the parser can identify the simulation structure.
- If multiple solve calls depend on each other, the importer creates cells for the lower-level simulations and wrapper cells for higher-level composition.
- If independent simulation branches exist, they are imported as separate branches.
- Reverse-imported cells retain the original Julia source so they can be opened as code or schematic later.

After import, review:

- Imported variables and defaults.
- Simulation frequency sweep.
- HB pump/DC settings.
- Exported pins and pin order.
- Automatically generated node labels.
- Wire layout and label placement.

## Adding Blocks

Use `Insert -> Add Instance...` or press `i`.

The palette contains:

- Built-in HB primitives from `logic/built-in/hbsolve/`.
- Built-in S-parameter blocks from `logic/built-in/ssolve/`.
- Local project cells.
- Imported project cells.

After choosing an item, click the canvas to place it. The block receives:

- A unique UID.
- Port names from the component definition.
- Default parameters.
- A default or saved symbol layout.
- HB instance settings if the referenced cell is marked as an HB top block.

You can also drag built-ins from the project explorer into placement mode.

## Editing Blocks

Select a block and use the inspector.

Common block properties:

- UID: unique within the schematic.
- Type: component/cell name.
- Source: built-in, local, or imported.
- Parameters: editable values or expressions.
- Exposed variables: variables used inside a subcell and exposed to the parent.
- Repeat settings.
- Symbol.
- HB instance settings, when applicable.

Use the block buttons to:

- Open Block.
- Replace Block.
- Edit Symbol.
- Reset Symbol.
- Reset Parameters.
- Delete Block.

### Parameters And Variables

Parameters can be numbers or expressions. The GUI preserves expressions as strings. The reserved variable `w` means simulation angular frequency and cannot be manually added as a normal cell variable.

Variable exposure works as follows:

- If a subcell uses a symbolic value, the parent can see and override that variable.
- If the current cell defines a numeric local variable with the same name, that variable is treated as local and is not exported farther upward.
- Imported Julia code uses this mechanism to keep symbolic block definitions while also importing numerical defaults from Julia `circuitdefs` or assignments.

## Wiring

Press `w` or choose `Insert -> Wire Mode`.

To create a wire:

1. Click a block port.
2. Move to another port.
3. Click the target port.

The GUI creates or merges nets as needed. Wires are drawn as orthogonal routes. The router tries to avoid block rectangles and keeps route endpoints aligned with port sides.

You can also connect a port to an existing net by clicking near the wire/net.

### Wire Bends

Right-click a wire to add or edit bends. Routes are stored in the GUI model so they can round-trip through project saves.

### What To Watch For

Validation warns if:

- A route crosses through a block.
- A wire has dangling endpoints.
- A net has duplicate endpoints.
- A port is left unconnected.

Automatic routing is useful, but complex imported circuits may still need manual cleanup.

## Pins And Labels

Pins and labels attach to nets.

### Exported Pins

Pins define the external interface of a schematic cell. Use `p` or `Insert -> Add Exported Pin`, then click a port/net.

Pin names must be:

- non-empty,
- unique within the cell,
- ordered contiguously.

Pin order is semantic. It defines the external numeric port order after the pipeline resolves ports. Use `Cell -> Edit Port Order` when the order matters.

### Text Labels

Labels name internal nets. Use `l` or `Insert -> Add Text Label`, then click a net.

Ground labels are often named `0`, especially for HB/JosephsonCircuits circuits.

Labels are constrained to stay close to their attached net. If a label drifts too far from its wire, the GUI may snap it back near the nearest route.

## Symbol Editing

Use `View -> Symbol Editor` or the selected block's `Edit Symbol` button.

Symbols control only GUI appearance:

- block width and height,
- port side,
- port relative position,
- label visibility,
- type and UID visibility.

The simulation pipeline uses port names and topology, not the drawn shape.

## Repeat Settings

Repeat settings are mainly for repeated or cascaded HB-style structures.

A block can have:

- `repeat_count`: how many copies are implied.
- `repeat_connections`: expressions for connecting repeated boundaries, for example an output of copy `j` to an input of copy `j+1`.

Repeat support is specialized. Validate and simulate after editing repeat settings because invalid repeat expressions may only become obvious during pipeline export or simulation.

## Simulation Setup

Use `Simulate -> Open Simulation Setup`.

Every cell has:

- Mode: `s` for S-parameter simulation, `x` for X-parameter simulation.
- Frequency start and stop in GHz.
- Number of frequency points.
- Sweep type.
- Figure title.
- `z0`.

### S-Parameter Mode

Set:

- Input port.
- Output port.

Ports should be exported pins of the current cell. The validator also allows some P-block label cases, but exported pins are the recommended path.

### X-Parameter Mode

Set:

- X input port.
- X output port.
- Pump port.
- Pump frequency in GHz.
- Pump current.
- Optional DC port.
- Optional DC current.
- Modulation harmonics.
- Pump harmonics.
- Three-wave and four-wave mixing flags.

Supported X-parameter scope:

- One X input port.
- One X output port.
- One pump port.
- One DC port.
- One X input, one X output, one pump, and one optional DC port are supported.

### HB Top Block Settings

For HB root cells, check `HB top block` in the cell inspector. Then use `HB Simulation Settings`.

HB settings include:

- Pump ports.
- Pump frequencies.
- Pump currents.
- DC ports.
- DC currents.
- Modulation harmonics.
- Pump harmonics.
- Three-wave mixing.
- Four-wave mixing.

If a cell marked as an HB top block is placed as an instance in another cell, its HB settings are copied onto the instance and remain editable there. This lets the parent preserve and override the nested HB setup.

### Skip Nested HB Block Check

`Skip nested HB block check` can be enabled on a schematic cell. It controls how the simulation pipeline classifies the nested HB blocks inside this cell:

- **Without this flag** (default): each nested block marked `hb_top_block` is treated as a pre-computed S-parameter result. The pipeline solves the nested blocks first and then treats their outputs as fixed S-matrices inside the parent.
- **With this flag**: nested `hb_top_block` blocks are treated as ordinary HB components participating in one combined simulation. The pipeline merges all of them into a single solve rather than solving them independently.

Use this flag when you have multiple Josephson elements or HB subsystems that must be solved together because they interact through shared nodes or shared pump fields. Do not use it if the nested blocks are genuinely independent and can be pre-solved on their own.

When this flag is set and more than one `hb_top_block` cell is found in the dependency tree, the pipeline automatically chooses the X merge simulation path.

## Validation

Use `Cell -> Validate Current Cell` before running a simulation.

The validator checks for:

- Invalid or duplicate cell names.
- Duplicate block UIDs.
- Missing referenced cells or built-ins.
- Mismatched port counts.
- Missing or malformed parameters.
- Dangling nets or ports.
- Invalid pins and labels.
- Route endpoints that no longer match a net.
- Routes crossing through blocks.
- Invalid simulation ports.
- Invalid frequency sweep.
- Non-positive `z0`.
- Incomplete X-parameter setup.

Warnings do not block simulation, but errors do.

## Saving

Use:

- `File -> Save Current Cell`
- `File -> Save All`

The GUI writes the active/local cell to `<cell_name>.json` and project metadata to `project.json`. Read-only imported cells are not saved into the current project unless copied locally.

Autosave is also present for dirty local cells. Normal project load ignores autosave files unless they are manually inspected.

## Importing Existing JSON

Use `File -> Import Cell JSON...` to bring an existing pipeline/GUI JSON into the current project.

The GUI converts pipeline-style fields into its internal net, pin, label, instance, simulation, and GUI route model.

Check after import:

- Pin order.
- Port names.
- Labels and ground nodes.
- Simulation setup.
- Variables.
- Imported route geometry.

## Referencing Another Project

Use `File -> Import Project...`.

This adds a read-only project reference. Referenced project cells appear in the explorer and palette. You can place them as instances, open them read-only, or copy them into the current project.

The GUI can also auto-resolve project references under `data/` when aliases match existing project folders.

## Running Simulations

Use `Simulate -> Run Simulation`.

The GUI does the following:

1. Validates the active cell.
2. Creates a temporary run folder under `data/guiV2_pipeline_runs/`.
3. Exports all local cells, plus needed read-only imported cells, to pipeline JSON.
4. Runs `logic/run_pipeline.sh` through a `QProcess`.
5. Streams stdout/stderr into the bottom debug panel.
6. Collects CSV outputs from `logic/outputs/<run_id>/cache/`.
7. Adds results to the Results panel.

Multiple simulations can run at once. Each run gets its own folder and process ID. `Stop Simulation` stops all active simulations.

The pipeline itself may require Julia and JosephsonCircuits to be installed correctly. If Julia fails, inspect the bottom panel and the pipeline log under `logic/pipeline_outputs/`.

## Results And Plotting

Simulation results appear in the Results panel grouped by run time.

For each result, the GUI stores:

- display name,
- cell name,
- row count,
- normalized CSV text,
- source path,
- result kind,
- optional reference comparison status,
- run id.

Double-click a result or choose Open Plot. The plot dialog can show different plot types depending on available sidecar files.

Standard S results can show S-matrix magnitude/phase curves.

Multimode sidecars can show:

- mode outputs,
- signal/idler curves,
- power bars,
- diagnostics.

X-parameter sidecars can show:

- default X transfer magnitude/phase,
- focused XS/XT transfers,
- S versus XS comparisons,
- XFB magnitude/phase.

You can:

- toggle visible curves,
- reset the view,
- set manual Y limits,
- export CSV,
- export PNG,
- copy a figure summary.

## Reference Comparisons

If a cell is marked as a reference-enabled result case, the GUI tries to find matching CSVs under `references/` and compares numeric values with an absolute tolerance of `1e-4`. Result rows show pass/fail/no-reference status.

Values below `1e-6` in absolute magnitude are excluded from comparison when both the output and reference are below that threshold. These are numerically zero (for example, a reflected power of −4000 dB), and the exact tiny value differs between runs without any physical meaning.

This is a convenience check, not a substitute for understanding whether the reference is physically appropriate for a changed design.

## Test Suite

`Simulate -> Run Test Suite` runs a set of regression tests that each exercise the full pipeline on a stored reference circuit and check the simulation outputs against known-good CSVs. Use this after making pipeline changes to catch regressions across the main simulation paths (S-parameter, HB, X-parameter, multi-block, repeated structures).

## Julia Import: Practical Notes

The reverse importer is designed to accept large pasted Julia files and extract the actual simulation graph. In normal cases it ignores plotting and presentation code.

It understands common JosephsonCircuits patterns:

- `solveS(...)`
- `hbsolve(...)`
- circuit arrays of tuples
- `circuitdefs = Dict(...)`
- frequency sweep assignments like `ws = 2*pi*(start:step:stop)*1e9`
- pump/DC source tuples
- modulation and pump harmonics
- simple function wrappers and dependent solve calls

What to check after import:

- Do component parameters use the intended variables?
- Did numerical variable defaults import correctly?
- Are local override variables intentionally local?
- Are HB pump/DC settings populated?
- Are all ground and non-ground node labels visible?
- Is the schematic layout readable enough?
- Are output/input simulation ports correct?

The importer is text-based with targeted parsing and Julia source probing for HB. Highly dynamic Julia code may not reverse-import perfectly. When in doubt, compare the imported schematic JSON to the original circuit tuple or run a simulation sanity check.

## Common Pitfalls

### Pin Order Matters

Pins are not just labels. Their order defines external port numbering in the pipeline. Always check port order before simulating or using a cell as a block.

### Port Names Are Strings Before The Pipeline Resolves Them

The GUI stores port names such as `p1`, `p2`, or even `"1"` as strings. Do not manually convert them to integers in source JSON.

### Built-In And Local Names Can Collide

A placed instance references a `type_name`. Plain names prefer local sibling cells before built-ins in some pipeline stages. Use clear names for local cells to avoid confusing resolution.

### Imported Projects Are Read-Only

You can place imported cells, but editing them directly is not allowed. Copy them into the current project when you need local modifications.

### Labels Are Not Pins

Labels name nets. Pins expose a cell interface. Simulation input/output ports should normally be exported pins.

### `w` Is Reserved

`w` is the simulation angular frequency. It may appear in frequency-dependent matrix or network expressions, but it should not be manually added as a normal variable.

### HB Top Blocks Inside Other Cells Need Care

A block marked as an HB top block carries simulation settings. When nested inside another cell, you must decide which mode the parent uses:

- **Pre-computed mode** (no flag on parent): the nested block is solved on its own first, and its result is treated as a fixed S-matrix inside the parent. Use this when the nested block is independent.
- **Merged mode** (`Skip nested HB block check` on parent): all nested HB blocks are merged into one combined simulation. Use this when the blocks share nodes, a pump, or otherwise cannot be solved independently.

Setting the flag incorrectly in either direction will produce a wrong or failing simulation.

### X-Parameter Mode Is Narrower Than S-Parameter Mode

X mode supports one input, one output, one pump, and one optional DC port.

### Generated Data Accumulates

Every GUI simulation run writes temporary data. Periodically clear `data/guiV2_pipeline_runs/` and `logic/outputs/guiV2_pipeline_*` if disk usage grows.

## Suggested Workflow For A New Circuit

1. Create or open a project.
2. Create small reusable subcells first.
3. Add pins and check their order.
4. Add variables with meaningful defaults.
5. Build the top-level schematic from subcells and built-ins.
6. Wire all ports and add ground/internal labels where useful.
7. Configure simulation setup.
8. Validate the active cell.
9. Save all.
10. Run simulation.
11. Inspect result plots and debug output.
12. If using imported Julia, compare the generated schematic against the original circuit definitions.

## Suggested Workflow For Imported Julia

1. Choose `Create Julia HB Block...`.
2. Paste the full trusted Julia source.
3. Let the importer parse the simulation hierarchy.
4. When asked, open either the schematic or code/settings view.
5. Review the import overview and skipped functions.
6. Check variable values and expressions.
7. Check HB settings and frequency sweep.
8. Check generated labels and pin order.
9. Save the imported cells.
10. Run a simulation and compare against the original Julia script if possible.
