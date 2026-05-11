# GUI V2 Function Map

This document records the current GUI surface area after the first cleanup pass.
The code is still intentionally conservative: behavior stayed in place unless a
missing or broken feature was directly fixed.

## File Layout

- `main.py`: Qt graphics items, canvas behavior, dialogs, project actions,
  import/export, validation, simulation process orchestration, and app startup.
- `gui_core.py`: shared constants, project/cell factory helpers, built-in catalog
  loading, schematic geometry/routing helpers, value coercion helpers, and the
  `Selection` dataclass.
- `plot_widget.py`: result CSV loading and plotting for S-parameter,
  multimode, diagnostic, and X-parameter outputs.

## Core Helpers

- Naming and data creation: `clean_name`, `blank_project`, `blank_cell`.
- Built-in and project discovery: `load_builtin_catalog`,
  `project_folder_summary`.
- Symbol repair and defaults: `default_symbol`, `repair_common_port_symbol`.
- Port and route geometry: `port_point`, `port_side`, `port_escape_point`,
  `clear_port_escape_point`, `block_rect`, `orthogonal_points`,
  `routed_orthogonal_points`, `path_from_points`, `segment_intersects_rect`,
  `compact_points`, `orthogonalize_points`, `align_route_endpoint`.
- Small coercion helpers: `truthy`, `as_list`, `first_value`,
  `repeat_count_value`, `parse_variables_text`.
- Label/wire proximity helpers: `distance_point_to_segment`,
  `closest_point_on_segment`, `closest_point_on_polyline`.

## Canvas And Schematic Editing

- Graphics item classes: `PortItem`, `BlockItem`, `WireItem`, `TagItem`,
  `ValidationMarker`.
- `SchematicView.draw` renders grid, wires, junction dots, blocks, pins, labels,
  validation markers, and live previews.
- Selection helpers: `port_from_item`, `block_from_item`, `wire_from_item`,
  `tag_from_item`.
- Mouse/keyboard handlers: `mousePressEvent`, `mouseMoveEvent`,
  `mouseReleaseEvent`, `contextMenuEvent`, `wheelEvent`, `keyPressEvent`.
- Placement preview: `update_preview_position_from_cursor`,
  `update_placement_preview`, `clear_placement_preview`, `draw_preview`.
  This is now implemented with persistent lightweight preview items instead of a
  full scene redraw on every mouse move.
- Wire preview: `_refresh_wire_preview`, `_nearest_port`,
  `schedule_wire_preview`. Wire preview is enabled and throttled through a short
  timer so moving the mouse does not force a full scene rebuild.
- Wire route refresh: `update_wire_items`.

## Dialogs And Plotting

- `InstancePalette`: searchable tree for built-ins, local cells, and imported
  cells.
- `ProjectStartDialog`: startup flow for recent, existing, and new projects.
- `plot_widget.PlotWidget`: result plotting for standard S parameters, multimode data,
  diagnostics, and X-parameter sidecar CSVs. Its loader/curve helpers are
  grouped by result format: `load_s_csv`, `multimode_output_curves`,
  `signal_idler_curves`, `diagnostics_curves`, `x_default_transfer_curves`,
  `x_focused_transfer_curves`, `s_vs_xs_curves`, `xfb_curves`.

## Main Window Responsibilities

- UI shell: `setup_ui`, `setup_menus`, `setup_shortcuts`, toggle helpers,
  canvas settings, startup checks.
- Project lifecycle: create/open/close project, recent project tracking,
  metadata saving, file watcher refresh, autosave, external file reload.
- Imports: reference projects, auto-resolve data-folder imports, reload/remove
  imports, copy imported cells locally.
- Cell lifecycle: create schematic/matrix cells, duplicate/rename/delete cells,
  tab management, explorer refresh and context menus.
- Schematic model editing: place/replace/open blocks, copy/paste/duplicate,
  rotate, delete, edit symbols, edit repeat settings.
- Net editing: finish wires, connect to existing nets, merge nets, add/delete
  wire bends, pins, and labels, constrain labels to nearby wires.
- Routing: auto-route wires around block bounding boxes and update affected
  routes when instances move.
- Inspector: populate cell/net/tag/block property panels and write edits back to
  the model.
- Simulation setup: S-parameter, X-parameter, and HB fields, including imported
  raw settings preservation.
- Validation: local GUI validation before save/simulation, including missing
  references, duplicate UIDs, dangling ports, invalid pins/labels, route/block
  crossings, z0, and simulation port setup.
- Pipeline adapter: export GUI cells to pipeline JSON, import pipeline JSON back
  into GUI cells, preserve GUI wire routes.
- Simulation jobs: `run_simulation`, `append_simulation_output`,
  `stop_simulation`, `simulation_finished`, `collect_results`.
- Results: normalize/compare CSV, refresh results tree, open plots, export CSV
  and PNG.

## Completion Notes

- Multiple simulations can now run in parallel. Each run gets its own
  `guiV2_pipeline_*` directory, its own `QProcess`, and its own target path, so
  completion callbacks collect the correct result even when jobs finish out of
  order.
- `Stop Simulation` now stops all running simulations instead of assuming a
  single global process.
- Block placement preview now appears as soon as an item is chosen from the
  palette/explorer if the cursor is over the canvas, and it tracks the snapped
  placement point.
- Wire preview is active during wire placement and is timer-throttled to avoid
  rebuilding the full scene on every mouse movement.
- Specialized HB primitives such as `Cj__p_*` are handled in the logic layer so
  drawn Josephson capacitance elements are emitted into generated Julia.

## Still Worth Splitting Later

- `PlotWidget` is self-contained enough to move into `plot_widget.py`.
- `SchematicView` and the graphics item classes are natural candidates for
  `canvas.py`.
- `MainWindow` can be reduced further by moving project IO, inspector builders,
  and pipeline import/export to dedicated modules.
