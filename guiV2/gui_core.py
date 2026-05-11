from __future__ import annotations

import json
import math
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QPointF, QRectF
from PyQt6.QtGui import QPainterPath


ROOT = Path(__file__).resolve().parents[1]
LOGIC_DIR = ROOT / "logic"
DATA_DIR = ROOT / "data"
PIPELINE_DATA_DIR = DATA_DIR / "guiV2_pipeline_runs"
DEFAULT_BUILTINS_DIR = LOGIC_DIR / "built-in"
RESERVED_VARIABLE_NAMES = {"w"}
GENERATED_ENTRY_LIMIT = 100
MAX_LABEL_DISTANCE_PIXELS = 30.0


def clean_name(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_]", "_", str(value or "").strip())
    if not text:
        return ""
    if not re.match(r"[A-Za-z_]", text[0]):
        text = "_" + text
    return text


def default_symbol(ports: list[str]) -> dict[str, Any]:
    if len(ports) == 1:
        return {
            "shape": "rectangle",
            "width": 80,
            "height": 50,
            "port_layout": [{"port": ports[0], "side": "bottom", "position": 0.5, "label_visible": True}],
            "show_type_name": True,
            "show_uid": True,
        }
    return {
        "shape": "rectangle",
        "width": 120,
        "height": 70,
        "label_position": "center",
        "show_type_name": True,
        "show_uid": True,
        "port_layout": [
            {
                "port": port,
                "side": "left" if i % 2 == 0 else "right",
                "position": 0.5 if len(ports) <= 2 else (i + 1) / (len(ports) + 1),
                "label_visible": True,
            }
            for i, port in enumerate(ports)
        ],
    }


def repair_common_port_symbol(inst: dict[str, Any]) -> None:
    symbol = inst.get("symbol")
    if not symbol:
        return
    layout = symbol.get("port_layout", [])
    ports = {str(port) for port in inst.get("port_names", [])}
    if "P_0" not in ports:
        return
    for entry in layout:
        port = str(entry.get("port", ""))
        if port == "P_0":
            entry["side"] = "bottom"
            entry["position"] = 0.5
        elif port == "P_pin":
            entry["side"] = "left"
            entry["position"] = 0.5
        elif port in {"P_2", "P_0+2"}:
            entry["side"] = "right"
            entry["position"] = 0.5
        elif port in {"P_N", "P_j"}:
            entry["side"] = "left"
            entry["position"] = 0.5
    inst["symbol_port_layout"] = symbol.get("port_layout", [])


def blank_project(name: str, path: str) -> dict[str, Any]:
    return {
        "name": name,
        "path": path,
        "version": 1,
        "default_cell": "",
        "default_z0": 50.0,
        "recent_cells": [],
        "imports": [],
        "importedCells": {},
        "cells": {},
        "results": [],
        "gui": {"last_open_tabs": [], "layout": {}},
    }


def blank_cell(name: str, cell_type: str = "schematic") -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "name": name,
        "type": cell_type,
        "description": "",
        "readOnly": False,
        "dirty": True,
        "instances": [],
        "nets": [],
        "pins": [],
        "labels": [],
        "variables": [],
        "simulation": {
            "mode": "s",
            "z0": 50.0,
            "input_ports": [],
            "output_ports": [],
            "freq_start": 2.0,
            "freq_stop": 20.0,
            "freq_points": 200,
            "sweep_type": "linear",
            "figure_title": name,
            "hb": {
                "top_block": False,
                "disable_child_top_block": False,
                "pump_ports": [],
                "pump_frequencies": [],
                "pump_currents": [],
                "dc_ports": [],
                "dc_currents": [],
                "modulation_harmonics": 10,
                "pump_harmonics": 20,
                "threewave_mixing": True,
                "fourwave_mixing": True,
            },
            "x": {
                "input_port": "",
                "output_port": "",
                "pump_port": "",
                "pump_frequencies": [7.12],
                "pump_frequency": 7.12,
                "pump_currents": ["1.85e-6"],
                "pump_current": "1.85e-6",
                "dc_port": "",
                "dc_current": "",
                "modulation_harmonics": [10],
                "pump_harmonics": [20],
                "threewave_mixing": True,
                "fourwave_mixing": True,
            },
        },
        "gui": {"version": 1, "viewport": {"zoom": 1, "pan": [0, 0]}, "wire_routes": [], "last_selected": []},
    }


def load_builtin_catalog(builtins_dir: Path | None = None) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    base_dir = Path(builtins_dir or DEFAULT_BUILTINS_DIR)
    if not base_dir.exists():
        return items
    for path in sorted(base_dir.rglob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        try:
            rel = path.relative_to(LOGIC_DIR).as_posix()
        except ValueError:
            rel = path.relative_to(base_dir).as_posix()
        if raw.get("library_group"):
            group = raw["library_group"]
        elif "/hbsolve/" in f"/{rel}":
            group = "HB Primitive Blocks"
        elif "/ssolve/abcd/" in f"/{rel}":
            group = "S-Parameter ABCD Blocks"
        else:
            group = "S-Parameter Blocks"
        ports = [str(p) for p in raw.get("port_names", [])]
        symbol = default_symbol(ports)
        if raw.get("symbol_port_layout"):
            symbol["port_layout"] = [
                {
                    "port": str(entry.get("port", ports[i] if i < len(ports) else f"p{i + 1}")),
                    "side": entry.get("side", "left" if i % 2 == 0 else "right"),
                    "position": float(entry.get("position", 0.5)),
                    "label_visible": entry.get("label_visible", True),
                }
                for i, entry in enumerate(raw.get("symbol_port_layout", []))
            ]
        items.append(
            {
                "id": f"builtin:{raw.get('name') or path.stem}",
                "name": raw.get("name") or path.stem,
                "source": "built-in",
                "path": rel,
                "actual_path": str(path),
                "group": group,
                "port_names": ports,
                "port_count": len(ports),
                "variables": raw.get("variables", []),
                "symbol": symbol,
            }
        )
    return items


def project_folder_summary(path: Path) -> str:
    meta = path / "project.json"
    name = clean_name(path.name) or path.name
    imports: list[dict[str, Any]] = []
    default_cell = ""
    unresolved = False
    if meta.exists():
        try:
            raw = json.loads(meta.read_text(encoding="utf-8"))
            name = str(raw.get("name") or name)
            imports = list(raw.get("imports", []))
            default_cell = str(raw.get("default_cell", ""))
        except Exception:
            unresolved = True
    cells = [p for p in path.glob("*.json") if p.name != "project.json" and not p.name.endswith(".bak") and ".autosave." not in p.name]
    missing_imports = [item for item in imports if item.get("enabled", True) and not Path(str(item.get("path", ""))).exists()]
    unresolved = unresolved or bool(missing_imports)
    try:
        modified = datetime.fromtimestamp(max([path.stat().st_mtime] + [p.stat().st_mtime for p in cells])).strftime("%Y-%m-%d %H:%M")
    except Exception:
        modified = "unknown"
    return "\n".join(
        [
            f"Project name: {name}",
            f"Project path: {path}",
            f"Cells: {len(cells)}",
            f"Imported projects: {len(imports)}",
            f"Default cell: {default_cell or 'none'}",
            f"Last modified: {modified}",
            f"Unresolved or missing references: {'yes' if unresolved else 'no'}",
        ]
    )


def port_point(inst: dict[str, Any], port_name: str) -> QPointF:
    symbol = inst.get("symbol") or default_symbol(inst.get("port_names", []))
    layout = next((p for p in symbol.get("port_layout", []) if str(p.get("port")) == str(port_name)), None)
    if not layout:
        layout = {"side": "right", "position": 0.5}
    w = float(symbol.get("width", 120)) / 2
    h = float(symbol.get("height", 70)) / 2
    pos = float(layout.get("position", 0.5))
    side = layout.get("side", "right")
    if side == "left":
        local = QPointF(-w, -h + 2 * h * pos)
    elif side == "top":
        local = QPointF(-w + 2 * w * pos, -h)
    elif side == "bottom":
        local = QPointF(-w + 2 * w * pos, h)
    else:
        local = QPointF(w, -h + 2 * h * pos)
    rotation = float(inst.get("rotation_degrees", 0))
    if rotation != 0:
        angle_rad = math.radians(rotation)
        cos_a = math.cos(angle_rad)
        sin_a = math.sin(angle_rad)
        local = QPointF(local.x() * cos_a - local.y() * sin_a, local.x() * sin_a + local.y() * cos_a)
    p = inst.get("position", [0, 0])
    return QPointF(float(p[0]) + local.x(), float(p[1]) + local.y())


def port_side(inst: dict[str, Any], port_name: str) -> str:
    symbol = inst.get("symbol") or default_symbol(inst.get("port_names", []))
    layout = next((p for p in symbol.get("port_layout", []) if str(p.get("port")) == str(port_name)), None)
    return str((layout or {}).get("side", "right"))


def port_escape_point(inst: dict[str, Any], port_name: str, distance: float = 28.0) -> QPointF:
    point = port_point(inst, port_name)
    side = port_side(inst, port_name)
    if side == "left":
        return QPointF(point.x() - distance, point.y())
    if side == "top":
        return QPointF(point.x(), point.y() - distance)
    if side == "bottom":
        return QPointF(point.x(), point.y() + distance)
    return QPointF(point.x() + distance, point.y())


def clear_port_escape_point(inst: dict[str, Any], port_name: str, obstacles: list[QRectF], distance: float = 28.0) -> QPointF:
    point = port_point(inst, port_name)
    preferred = port_escape_point(inst, port_name, distance)
    candidates = [
        preferred,
        QPointF(point.x() - distance, point.y()),
        QPointF(point.x() + distance, point.y()),
        QPointF(point.x(), point.y() - distance),
        QPointF(point.x(), point.y() + distance),
    ]

    def score(candidate: QPointF) -> tuple[int, float]:
        blocked = sum(1 for rect in obstacles if rect.contains(candidate))
        return (blocked, (candidate.x() - preferred.x()) ** 2 + (candidate.y() - preferred.y()) ** 2)

    return min(candidates, key=score)


def orthogonal_path(a: QPointF, b: QPointF) -> QPainterPath:
    return path_from_points(orthogonal_points(a, b))


def orthogonal_points(a: QPointF, b: QPointF) -> list[list[float]]:
    mid_x = (a.x() + b.x()) / 2
    return [[a.x(), a.y()], [mid_x, a.y()], [mid_x, b.y()], [b.x(), b.y()]]


def block_rect(inst: dict[str, Any], margin: float = 16.0) -> QRectF:
    symbol = inst.get("symbol") or default_symbol(inst.get("port_names", []))
    w = float(symbol.get("width", 120)) / 2
    h = float(symbol.get("height", 70)) / 2
    pos = inst.get("position", [0, 0])
    cx, cy = float(pos[0]), float(pos[1])
    rotation = float(inst.get("rotation_degrees", 0))
    if rotation != 0:
        angle_rad = math.radians(rotation)
        cos_a, sin_a = math.cos(angle_rad), math.sin(angle_rad)
        corners = [(-w, -h), (w, -h), (w, h), (-w, h)]
        xs = [cx + lx * cos_a - ly * sin_a for lx, ly in corners]
        ys = [cy + lx * sin_a + ly * cos_a for lx, ly in corners]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
    else:
        min_x, max_x = cx - w, cx + w
        min_y, max_y = cy - h, cy + h
    return QRectF(min_x - margin, min_y - margin, (max_x - min_x) + 2 * margin, (max_y - min_y) + 2 * margin)


def segment_intersects_rect(a: list[float], b: list[float], rect: QRectF) -> bool:
    ax, ay = float(a[0]), float(a[1])
    bx, by = float(b[0]), float(b[1])
    if math.isclose(ay, by):
        left, right = sorted([ax, bx])
        return rect.top() <= ay <= rect.bottom() and max(left, rect.left()) <= min(right, rect.right())
    if math.isclose(ax, bx):
        top, bottom = sorted([ay, by])
        return rect.left() <= ax <= rect.right() and max(top, rect.top()) <= min(bottom, rect.bottom())
    return False


def compact_points(points: list[list[float]]) -> list[list[float]]:
    compacted: list[list[float]] = []
    for point in points:
        if compacted and math.isclose(compacted[-1][0], point[0]) and math.isclose(compacted[-1][1], point[1]):
            continue
        compacted.append([float(point[0]), float(point[1])])
    return compacted


def route_intersection_count(points: list[list[float]], obstacles: list[QRectF]) -> int:
    total = 0
    for a, b in zip(points, points[1:]):
        total += sum(1 for rect in obstacles if segment_intersects_rect(a, b, rect))
    return total


def same_direction_overlap_length(
    a: list[float],
    b: list[float],
    c: list[float],
    d: list[float],
    tolerance: float = 1e-6,
) -> float:
    ax, ay = float(a[0]), float(a[1])
    bx, by = float(b[0]), float(b[1])
    cx, cy = float(c[0]), float(c[1])
    dx, dy = float(d[0]), float(d[1])
    a_horizontal = math.isclose(ay, by, abs_tol=tolerance)
    c_horizontal = math.isclose(cy, dy, abs_tol=tolerance)
    a_vertical = math.isclose(ax, bx, abs_tol=tolerance)
    c_vertical = math.isclose(cx, dx, abs_tol=tolerance)
    if a_horizontal and c_horizontal and math.isclose(ay, cy, abs_tol=tolerance):
        a0, a1 = sorted([ax, bx])
        c0, c1 = sorted([cx, dx])
        return max(0.0, min(a1, c1) - max(a0, c0))
    if a_vertical and c_vertical and math.isclose(ax, cx, abs_tol=tolerance):
        a0, a1 = sorted([ay, by])
        c0, c1 = sorted([cy, dy])
        return max(0.0, min(a1, c1) - max(a0, c0))
    return 0.0


def route_overlap_length(points: list[list[float]], occupied_segments: list[tuple[list[float], list[float]]]) -> float:
    total = 0.0
    for a, b in zip(points, points[1:]):
        for c, d in occupied_segments:
            total += same_direction_overlap_length(a, b, c, d)
    return total


def route_length(points: list[list[float]]) -> float:
    return sum(abs(float(a[0]) - float(b[0])) + abs(float(a[1]) - float(b[1])) for a, b in zip(points, points[1:]))


def align_route_endpoint(points: list[list[float]], at_start: bool, port_side_value: str) -> None:
    if len(points) < 2:
        return
    endpoint_idx = 0 if at_start else -1
    neighbor_idx = 1 if at_start else -2
    endpoint = points[endpoint_idx]
    neighbor = points[neighbor_idx]
    if port_side_value in {"left", "right"}:
        neighbor[1] = endpoint[1]
    else:
        neighbor[0] = endpoint[0]


def orthogonalize_points(points: list[list[float]]) -> list[list[float]]:
    if len(points) < 2:
        return compact_points(points)
    fixed = [[float(points[0][0]), float(points[0][1])]]
    for point in points[1:]:
        current = [float(point[0]), float(point[1])]
        previous = fixed[-1]
        if not (math.isclose(previous[0], current[0]) or math.isclose(previous[1], current[1])):
            if abs(previous[0] - current[0]) >= abs(previous[1] - current[1]):
                current[1] = previous[1]
            else:
                current[0] = previous[0]
        fixed.append(current)
    return compact_points(fixed)


def routed_orthogonal_points(
    a: QPointF,
    b: QPointF,
    obstacles: list[QRectF],
    occupied_segments: list[tuple[list[float], list[float]]] | None = None,
    lane_spacing: float = 18.0,
) -> list[list[float]]:
    raw_occupied = occupied_segments or []
    min_x, max_x = sorted([float(a.x()), float(b.x())])
    min_y, max_y = sorted([float(a.y()), float(b.y())])
    search_margin = 260.0
    occupied: list[tuple[list[float], list[float]]] = []
    for c, d in raw_occupied:
        cx, cy = float(c[0]), float(c[1])
        dx, dy = float(d[0]), float(d[1])
        if (
            max(cx, dx) < min_x - search_margin
            or min(cx, dx) > max_x + search_margin
            or max(cy, dy) < min_y - search_margin
            or min(cy, dy) > max_y + search_margin
        ):
            continue
        occupied.append((c, d))
    padding = 24.0
    xs = {float(a.x()), float(b.x())}
    ys = {float(a.y()), float(b.y())}
    for rect in obstacles:
        if (
            rect.right() < min_x - search_margin
            or rect.left() > max_x + search_margin
            or rect.bottom() < min_y - search_margin
            or rect.top() > max_y + search_margin
        ):
            continue
        for x in (rect.left() - padding, rect.right() + padding):
            xs.add(float(x))
        for y in (rect.top() - padding, rect.bottom() + padding):
            ys.add(float(y))
    for c, d in occupied:
        cx, cy = float(c[0]), float(c[1])
        dx, dy = float(d[0]), float(d[1])
        if math.isclose(cy, dy):
            for y in (cy - lane_spacing, cy + lane_spacing):
                ys.add(float(y))
            xs.add(cx)
            xs.add(dx)
        elif math.isclose(cx, dx):
            for x in (cx - lane_spacing, cx + lane_spacing):
                xs.add(float(x))
            ys.add(cy)
            ys.add(dy)

    x_values = sorted(xs)
    y_values = sorted(ys)
    x_index = {x: i for i, x in enumerate(x_values)}
    y_index = {y: i for i, y in enumerate(y_values)}
    start = (float(a.x()), float(a.y()))
    goal = (float(b.x()), float(b.y()))
    nodes = [(x, y) for x in x_values for y in y_values]
    node_set = set(nodes)

    def blocked(p0: tuple[float, float], p1: tuple[float, float]) -> bool:
        if p0 == p1:
            return False
        segment = [[p0[0], p0[1]], [p1[0], p1[1]]]
        return any(segment_intersects_rect(segment[0], segment[1], rect) for rect in obstacles)

    def edge_cost(p0: tuple[float, float], p1: tuple[float, float]) -> float:
        segment = [[p0[0], p0[1]], [p1[0], p1[1]]]
        length = route_length(segment)
        overlap = route_overlap_length(segment, occupied)
        return length + overlap * 1000.0

    import heapq

    frontier: list[tuple[float, int, tuple[float, float]]] = []
    counter = 0
    heapq.heappush(frontier, (0.0, counter, start))
    came_from: dict[tuple[float, float], tuple[float, float] | None] = {start: None}
    cost_so_far: dict[tuple[float, float], float] = {start: 0.0}

    while frontier:
        _, _, current = heapq.heappop(frontier)
        if current == goal:
            break
        x, y = current
        neighbors: list[tuple[float, float]] = []
        xi = x_index[x]
        yi = y_index[y]
        if xi > 0 and (x_values[xi - 1], y) in node_set:
            neighbors.append((x_values[xi - 1], y))
        if xi + 1 < len(x_values) and (x_values[xi + 1], y) in node_set:
            neighbors.append((x_values[xi + 1], y))
        if yi > 0 and (x, y_values[yi - 1]) in node_set:
            neighbors.append((x, y_values[yi - 1]))
        if yi + 1 < len(y_values) and (x, y_values[yi + 1]) in node_set:
            neighbors.append((x, y_values[yi + 1]))
        for nxt in neighbors:
            if blocked(current, nxt):
                continue
            new_cost = cost_so_far[current] + edge_cost(current, nxt)
            if nxt not in cost_so_far or new_cost < cost_so_far[nxt]:
                cost_so_far[nxt] = new_cost
                heuristic = abs(nxt[0] - goal[0]) + abs(nxt[1] - goal[1])
                bends = 0.001
                counter += 1
                heapq.heappush(frontier, (new_cost + heuristic + bends, counter, nxt))
                came_from[nxt] = current

    if goal in came_from:
        route: list[list[float]] = []
        current: tuple[float, float] | None = goal
        while current is not None:
            route.append([current[0], current[1]])
            current = came_from[current]
        route.reverse()
        return compact_points(route)

    candidates = [
        orthogonal_points(a, b),
        [[a.x(), a.y()], [a.x(), b.y()], [b.x(), b.y()]],
    ]
    for rect in obstacles:
        for x in [rect.left() - padding, rect.right() + padding]:
            candidates.append([[a.x(), a.y()], [x, a.y()], [x, b.y()], [b.x(), b.y()]])
        for y in [rect.top() - padding, rect.bottom() + padding]:
            candidates.append([[a.x(), a.y()], [a.x(), y], [b.x(), y], [b.x(), b.y()]])

    def score(points: list[list[float]]) -> tuple[int, float, float, int]:
        compacted = compact_points(points)
        return (
            route_intersection_count(compacted, obstacles),
            route_overlap_length(compacted, occupied),
            route_length(compacted),
            len(compacted),
        )

    best = min(candidates, key=score)
    return compact_points(best)


def path_from_points(points: list[list[float]]) -> QPainterPath:
    if not points:
        return QPainterPath()
    path = QPainterPath(QPointF(float(points[0][0]), float(points[0][1])))
    for point in points[1:]:
        path.lineTo(float(point[0]), float(point[1]))
    return path


def parse_variables_text(text: str) -> list[dict[str, str]]:
    variables: list[dict[str, str]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if "=" in line:
            name, value = line.split("=", 1)
        else:
            name, value = line, ""
        cleaned = clean_name(name)
        if cleaned and cleaned not in RESERVED_VARIABLE_NAMES:
            variables.append({"name": cleaned, "default": value.strip(), "value": value.strip(), "scope": "cell"})
    return variables


def truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def first_value(value: Any, fallback: Any = "") -> Any:
    values = as_list(value)
    return values[0] if values else fallback


def repeat_count_value(value: Any, fallback: int = 1) -> int:
    try:
        return int(str(value).strip().rstrip("rR"))
    except (TypeError, ValueError):
        return fallback


def distance_point_to_segment(point: tuple[float, float], seg_start: tuple[float, float], seg_end: tuple[float, float]) -> float:
    px, py = point
    x1, y1 = seg_start
    x2, y2 = seg_end
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return math.sqrt((px - x1) ** 2 + (py - y1) ** 2)
    t = max(0, min(1, ((px - x1) * dx + (py - y1) * dy) / (dx ** 2 + dy ** 2)))
    closest_x, closest_y = x1 + t * dx, y1 + t * dy
    return math.sqrt((px - closest_x) ** 2 + (py - closest_y) ** 2)


def closest_point_on_segment(point: tuple[float, float], seg_start: tuple[float, float], seg_end: tuple[float, float]) -> tuple[float, float]:
    px, py = point
    x1, y1 = seg_start
    x2, y2 = seg_end
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return (x1, y1)
    t = max(0, min(1, ((px - x1) * dx + (py - y1) * dy) / (dx ** 2 + dy ** 2)))
    return (x1 + t * dx, y1 + t * dy)


def closest_point_on_polyline(point: tuple[float, float], points: list[list[float]]) -> tuple[float, float]:
    if len(points) < 2:
        return (points[0][0], points[0][1]) if points else (0, 0)
    min_dist = float("inf")
    closest = (0, 0)
    for i in range(len(points) - 1):
        seg_start = (points[i][0], points[i][1])
        seg_end = (points[i + 1][0], points[i + 1][1])
        dist = distance_point_to_segment(point, seg_start, seg_end)
        if dist < min_dist:
            min_dist = dist
            closest = closest_point_on_segment(point, seg_start, seg_end)
    return closest


SIMULATION_RAW_KEYS = {
    "simulation_mode",
    "simulation_variables",
    "hb_mode_analysis_enabled",
    "multimode",
    "multimode_mode_min",
    "multimode_mode_max",
    "multimode_reference_input_mode",
    "multimode_reference_input_port",
    "multimode_reference_output_port",
    "multimode_output_port",
    "multimode_conversion_output_modes",
    "multimode_symplectic_tolerance",
    "hb_input_field",
    "hb_input_pin_name",
    "hb_output_field",
    "hb_output_pin_name",
}


@dataclass
class Selection:
    kind: str
    id: str
