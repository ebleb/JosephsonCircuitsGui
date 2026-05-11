#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import math
import os
import re
import shutil
import sys
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QEventLoop, QFileSystemWatcher, QPointF, QRectF, QSettings, Qt, QProcess, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QBrush, QColor, QCursor, QFont, QKeySequence, QPainter, QPainterPath, QPainterPathStroker, QPen, QPixmap, QShortcut

from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsPathItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QHeaderView,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QProgressDialog,
    QRadioButton,
    QScrollArea,
    QSplitter,
    QStackedWidget,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QToolBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)


from plot_widget import NavigationToolbar2QT, PlotWidget
from gui_core import (
    DATA_DIR,
    DEFAULT_BUILTINS_DIR,
    GENERATED_ENTRY_LIMIT,
    LOGIC_DIR,
    MAX_LABEL_DISTANCE_PIXELS,
    PIPELINE_DATA_DIR,
    RESERVED_VARIABLE_NAMES,
    ROOT,
    SIMULATION_RAW_KEYS,
    Selection,
    align_route_endpoint,
    as_list,
    blank_cell,
    blank_project,
    block_rect,
    clean_name,
    clear_port_escape_point,
    closest_point_on_polyline,
    distance_point_to_segment,
    compact_points,
    default_symbol,
    first_value,
    load_builtin_catalog,
    orthogonal_points,
    orthogonalize_points,
    parse_variables_text,
    path_from_points,
    port_point,
    port_side,
    project_folder_summary,
    repair_common_port_symbol,
    repeat_count_value,
    route_intersection_count,
    route_length,
    route_overlap_length,
    routed_orthogonal_points,
    segment_intersects_rect,
    truthy,
)

if str(LOGIC_DIR) not in sys.path:
    sys.path.insert(0, str(LOGIC_DIR))
from julia_hb_importer import (
    build_generated_cell,
    build_generated_s_cell,
    detect_julia_simulation_type,
    import_julia_simulation_hierarchy,
    materialize_generated_hb_cell,
    probe_julia_source,
)


VARIABLE_GLOBAL_NAMES = RESERVED_VARIABLE_NAMES | {
    "im", "pi", "exp", "sqrt", "sin", "cos", "tan", "log", "log10", "abs", "real", "imag",
    "z0", "Z0", "z_0", "Z_0",
}


class PortItem(QGraphicsEllipseItem):
    def __init__(self, uid: str, port: str, x: float, y: float, parent: QGraphicsItem):
        super().__init__(-12, -12, 24, 24, parent)
        self.uid = uid
        self.port = port
        self.setPos(x, y)
        self.setBrush(QBrush(QColor("#236a5b")))
        self.setPen(QPen(QColor("#236a5b")))
        self.setZValue(3)

    def paint(self, painter, option, widget):
        painter.setBrush(self.brush())
        painter.setPen(self.pen())
        painter.drawEllipse(-5, -5, 10, 10)

    def shape(self):
        path = QPainterPath()
        path.addEllipse(-12, -12, 24, 24)
        return path


class BlockItem(QGraphicsRectItem):
    def __init__(self, inst: dict[str, Any], selected: bool, coupled: bool = False):
        self.inst = inst
        symbol = inst.get("symbol") or default_symbol(inst.get("port_names", []))
        w = float(symbol.get("width", 120))
        h = float(symbol.get("height", 70))
        super().__init__(-w / 2, -h / 2, w, h)
        self.setPos(float(inst.get("position", [0, 0])[0]), float(inst.get("position", [0, 0])[1]))
        self.setRotation(float(inst.get("rotation_degrees", 0)))
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        if selected:
            self.setBrush(QBrush(QColor("#e8f0ed")))
            self.setPen(QPen(QColor("#236a5b"), 2))
        elif coupled:
            self.setBrush(QBrush(QColor("#fff3e0")))
            self.setPen(QPen(QColor("#e67e22"), 2))
        else:
            self.setBrush(QBrush(QColor("#ffffff")))
            self.setPen(QPen(QColor("#53615d"), 1.2))
        self.setZValue(2)
        title = QGraphicsSimpleTextItem(str(inst.get("type_name", "")), self)
        title.setFont(QFont("Sans Serif", 10, QFont.Weight.Bold))
        title.setBrush(QBrush(QColor("#1c2421")))
        title_rect = title.boundingRect()
        title.setPos(-title_rect.width() / 2, -8)
        uid_text = QGraphicsSimpleTextItem(str(inst.get("uid", "")), self)
        uid_text.setFont(QFont("Sans Serif", 8))
        uid_text.setBrush(QBrush(QColor("#66716d")))
        uid_rect = uid_text.boundingRect()
        uid_text.setPos(-uid_rect.width() / 2, 10)
        repeat_count = repeat_count_value(inst.get("repeat_count", 1))
        if repeat_count > 1:
            repeat_text = QGraphicsSimpleTextItem(f"{repeat_count}x", self)
            repeat_text.setFont(QFont("Sans Serif", 8, QFont.Weight.Bold))
            repeat_text.setBrush(QBrush(QColor("#8e4b32")))
            repeat_rect = repeat_text.boundingRect()
            repeat_text.setPos(w / 2 - repeat_rect.width() - 6, -h / 2 + 4)
        for layout in symbol.get("port_layout", []):
            port = str(layout.get("port"))
            local = port_point({**inst, "position": [0, 0], "rotation_degrees": 0}, port)
            PortItem(str(inst.get("uid")), port, local.x(), local.y(), self)
            if layout.get("label_visible", True):
                pin_label = str(layout.get("label") or port)
                label = QGraphicsSimpleTextItem(pin_label, self)
                label.setFont(QFont("Sans Serif", 8, QFont.Weight.Bold))
                label.setBrush(QBrush(QColor("#33413d")))
                label_rect = label.boundingRect()
                margin = 8
                outside_gap = 10
                side = str(layout.get("side", "right"))
                if side == "left":
                    x = -w / 2 - outside_gap - label_rect.width()
                    y = local.y() - label_rect.height() / 2
                elif side == "right":
                    x = w / 2 + outside_gap
                    y = local.y() - label_rect.height() / 2
                elif side == "top":
                    x = local.x() - label_rect.width() / 2
                    y = -h / 2 - outside_gap - label_rect.height()
                else:
                    x = local.x() - label_rect.width() / 2
                    y = h / 2 + outside_gap
                label.setPos(x, y)


class WireItem(QGraphicsPathItem):
    def __init__(self, net_id: str, wire_id: str, points: list[list[float]], selected: bool):
        super().__init__(path_from_points(points))
        self.net_id = net_id
        self.wire_id = wire_id
        self.points = points
        self.setPen(QPen(QColor("#9a6b16" if selected else "#26352f"), 4 if selected else 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        self.setZValue(4)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)

    def shape(self) -> QPainterPath:
        stroker = QPainterPathStroker()
        stroker.setWidth(4)
        stroker.setCapStyle(Qt.PenCapStyle.RoundCap)
        stroker.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        return stroker.createStroke(self.path())


class TagItem(QGraphicsSimpleTextItem):
    def __init__(self, kind: str, tag: dict[str, Any], color: QColor, selected: bool):
        super().__init__(str(tag.get("name", "")))
        self.kind = kind
        self.tag_id = str(tag.get("id", ""))
        self.net_id = str(tag.get("net_id", ""))
        self.setFont(QFont("Sans Serif", 10, QFont.Weight.Bold))
        self.setBrush(QBrush(QColor("#ffffff") if selected else color))
        pos = tag.get("position", [0, 0])
        self.setPos(float(pos[0]), float(pos[1]))
        self.setZValue(5)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, kind != "pin")
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        if selected:
            marker = QGraphicsRectItem(self.boundingRect().adjusted(-4, -2, 4, 2), self)
            marker.setBrush(QBrush(color))
            marker.setPen(QPen(color, 0))
            marker.setZValue(-1)


class ValidationMarker(QGraphicsEllipseItem):
    def __init__(self, x: float, y: float, text_value: str, error_message: str):
        super().__init__(-8, -8, 16, 16)
        self.error_message = error_message
        self.setPos(x, y)
        self.setBrush(QBrush(QColor("#b3261e")))
        self.setPen(QPen(QColor("#b3261e")))
        self.setZValue(10)
        self.setAcceptHoverEvents(True)
        label = QGraphicsSimpleTextItem(text_value, self)
        label.setBrush(QBrush(QColor("#ffffff")))
        label.setFont(QFont("Sans Serif", 9, QFont.Weight.Bold))
        label.setPos(-3, -8)

    def hoverEnterEvent(self, event) -> None:
        self.setToolTip(self.error_message)
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event) -> None:
        self.setToolTip("")
        super().hoverLeaveEvent(event)


class _JuliaProbeThread(QThread):
    probe_done = pyqtSignal(dict)
    probe_failed = pyqtSignal(str)

    def __init__(self, source: str, timeout: int = 120) -> None:
        super().__init__()
        self._source = source
        self._timeout = timeout

    def run(self) -> None:
        try:
            probe = probe_julia_source(self._source, timeout=self._timeout)
            self.probe_done.emit(probe)
        except Exception as exc:
            self.probe_failed.emit(str(exc))


class _JuliaImportThread(QThread):
    import_done = pyqtSignal(dict)
    import_failed = pyqtSignal(str)

    def __init__(self, source: str, name_hint: str) -> None:
        super().__init__()
        self._source = source
        self._name_hint = name_hint

    def run(self) -> None:
        try:
            imported = import_julia_simulation_hierarchy(self._source, name_hint=self._name_hint)
            self.import_done.emit(imported)
        except Exception as exc:
            self.import_failed.emit(str(exc))


def _draw_generated_circuit(scene: "QGraphicsScene", display_comps: list[dict]) -> None:
    """Draw a simplified schematic for a generated circuit into a QGraphicsScene."""
    if not display_comps:
        text = scene.addText("No circuit data available")
        text.setDefaultTextColor(QColor("#aaa"))
        return
    COMP_W, COMP_H = 80, 36
    HALF_W, HALF_H = COMP_W // 2, COMP_H // 2
    TYPE_COLORS = {
        "R": "#e8d5b0", "L": "#b0cce8", "C": "#b0e8b8", "Lj": "#d0b0e8",
        "Cj": "#e8b0d0", "I": "#e8e0b0", "P": "#c0c0c0", "NL": "#e0b0b0",
    }
    pen = QPen(QColor("#444"), 1.5)
    node_endpoints: dict[str, list[tuple[float, float]]] = {}
    for comp in display_comps:
        x, y = float(comp["x"]), float(comp["y"])
        t = comp["type"]
        color = QColor(TYPE_COLORS.get(t, "#ddd"))
        rect = scene.addRect(x - HALF_W, y - HALF_H, COMP_W, COMP_H,
                             pen, QBrush(color))
        label = scene.addSimpleText(f"{comp['name']}\n{comp['value']}")
        label.setPos(x - HALF_W + 4, y - HALF_H + 2)
        font = label.font()
        font.setPointSize(7)
        label.setFont(font)
        p1x, p1y = x - HALF_W, y
        p2x, p2y = x + HALF_W, y
        node_endpoints.setdefault(comp["node1"], []).append((p1x, p1y))
        node_endpoints.setdefault(comp["node2"], []).append((p2x, p2y))
    gnd_pen = QPen(QColor("#888"), 1.5)
    for px, py in node_endpoints.get("0", []):
        scene.addLine(px, py, px, py + 20, gnd_pen)
        scene.addLine(px - 10, py + 20, px + 10, py + 20, gnd_pen)
        scene.addLine(px - 6, py + 26, px + 6, py + 26, gnd_pen)
        scene.addLine(px - 2, py + 32, px + 2, py + 32, gnd_pen)
    wire_pen = QPen(QColor("#333"), 1.5)
    for node, pts in node_endpoints.items():
        if node == "0" or len(pts) < 2:
            continue
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        mid_y = sorted(ys)[len(ys) // 2]
        x_min, x_max = min(xs), max(xs)
        scene.addLine(x_min, mid_y, x_max, mid_y, wire_pen)
        for px, py in pts:
            if abs(py - mid_y) > 2:
                scene.addLine(px, mid_y, px, py, wire_pen)


class GeneratedCellWidget(QWidget):
    """Panel shown instead of the schematic canvas for generated_hb / generated_s cells."""

    def __init__(self, window: "MainWindow") -> None:
        super().__init__(window)
        self._window = window
        self._loaded_cell_id: str | None = None
        self._inner_tabs = QTabWidget()
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.addWidget(self._inner_tabs)

    def load_cell(self, cell: dict, *, ask_view: bool = False) -> None:
        cell_id = cell.get("id")
        if cell_id and cell_id == self._loaded_cell_id and not ask_view:
            return
        self._loaded_cell_id = cell_id
        self._inner_tabs.clear()
        cell_type = cell.get("type", "")

        code_widget = QWidget()
        code_layout = QVBoxLayout(code_widget)
        code_layout.setContentsMargins(8, 8, 8, 8)

        if cell_type == "generated_hb":
            type_text = "HB (hbsolve)"
        elif cell_type == "generated_s":
            type_text = "S-parameter (solveS)"
        else:
            type_text = "Imported Julia schematic"
        type_label = QLabel(f"<b>Type:</b> {type_text}")
        code_layout.addWidget(type_label)

        sim = cell.get("simulation", {}) or {}
        freq_start = sim.get("freq_start", cell.get("simulation_freq_start", "?"))
        freq_stop = sim.get("freq_stop", cell.get("simulation_freq_stop", "?"))
        freq_points = sim.get("freq_points", cell.get("simulation_freq_points", "?"))
        freq_label = QLabel(
            f"<b>Frequency sweep:</b> {freq_start} – {freq_stop} GHz, "
            f"{freq_points} points"
        )
        code_layout.addWidget(freq_label)

        if cell_type == "generated_hb":
            summary = cell.get("generated_summary", {}) or {}
            counts = summary.get("primitive_counts", {}) or {}
            counts_str = ", ".join(f"{k}:{v}" for k, v in sorted(counts.items())) or "none"
            code_layout.addWidget(QLabel(
                f"<b>Components:</b> {summary.get('component_count', 0)}  "
                f"<b>Nodes:</b> {summary.get('node_count', 0)}  ({counts_str})"
            ))

        code_layout.addWidget(QLabel("<b>Julia source:</b>"))
        src_edit = QPlainTextEdit(str(cell.get("generated_source", "")))
        src_edit.setReadOnly(True)
        src_edit.setMaximumHeight(220)
        font = src_edit.font()
        font.setFamily("Monospace")
        font.setPointSize(9)
        src_edit.setFont(font)
        code_layout.addWidget(src_edit)

        code_layout.addWidget(QLabel("<b>Variables:</b>"))
        variables = cell.get("variables", []) or []
        var_table = QTableWidget(len(variables), 3)
        var_table.setHorizontalHeaderLabels(["Name", "Default", "Value"])
        var_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        var_table.setMaximumHeight(120)
        for row, var in enumerate(variables):
            var_table.setItem(row, 0, QTableWidgetItem(str(var.get("name", ""))))
            var_table.setItem(row, 1, QTableWidgetItem(str(var.get("default", ""))))
            var_table.setItem(row, 2, QTableWidgetItem(str(var.get("value", var.get("default", "")))))
        code_layout.addWidget(var_table)

        btn_row = QHBoxLayout()
        if cell_type == "generated_hb":
            edit_btn = QPushButton("Edit Source")
            edit_btn.clicked.connect(self._window.edit_generated_hb_source)
            btn_row.addWidget(edit_btn)
            refresh_btn = QPushButton("Regenerate Summary")
            refresh_btn.clicked.connect(self._window.refresh_generated_hb_summary)
            btn_row.addWidget(refresh_btn)
        btn_row.addStretch()
        code_layout.addLayout(btn_row)
        code_layout.addStretch()

        self._inner_tabs.addTab(code_widget, "Code & Settings")

        if cell_type == "generated_hb":
            display_comps = cell.get("generated_display_components") or []
            schematic_widget = QWidget()
            s_layout = QVBoxLayout(schematic_widget)
            s_layout.setContentsMargins(0, 0, 0, 0)
            schematic_view = QGraphicsView()
            schematic_view.setScene(QGraphicsScene(schematic_view))
            schematic_view.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            schematic_view.setBackgroundBrush(QBrush(QColor("#fbfcfa")))
            _draw_generated_circuit(schematic_view.scene(), display_comps)
            schematic_view.fitInView(
                schematic_view.scene().itemsBoundingRect().adjusted(-40, -40, 40, 40),
                Qt.AspectRatioMode.KeepAspectRatio,
            )
            s_layout.addWidget(schematic_view)
            self._inner_tabs.addTab(schematic_widget, "Schematic")

        n_tabs = self._inner_tabs.count()
        preferred_tab = "code"
        if n_tabs > 1:
            choice = QMessageBox.question(
                self._window,
                "Open View",
                f"This cell has a code view and a schematic view.\n"
                f"Which tab would you like to start with?\n\n"
                f"Yes = Schematic   No = Code & Settings",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            preferred_tab = "schematic" if choice == QMessageBox.StandardButton.Yes else "code"
        if preferred_tab == "schematic" and n_tabs > 1:
            self._inner_tabs.setCurrentIndex(1)
        else:
            self._inner_tabs.setCurrentIndex(0)


class SchematicView(QGraphicsView):
    def __init__(self, window: "MainWindow"):
        super().__init__()
        self.window = window
        self.setScene(QGraphicsScene(self))
        self.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self.setOptimizationFlag(QGraphicsView.OptimizationFlag.DontSavePainterState, True)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.BoundingRectViewportUpdate)
        self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
        self.setSceneRect(-2000, -2000, 4000, 4000)
        self.pending_wire: dict[str, str] | None = None
        self.drag_snapshot: str | None = None
        self.preview_pos: QPointF | None = None
        self._wire_preview_item: QGraphicsPathItem | None = None
        self._snap_indicator_item: QGraphicsEllipseItem | None = None
        self._placement_preview_items: list[QGraphicsItem] = []
        self._wire_preview_timer = QTimer(self)
        self._wire_preview_timer.setSingleShot(True)
        self._wire_preview_timer.setInterval(45)
        self._wire_preview_timer.timeout.connect(self._refresh_wire_preview)
        self.setBackgroundBrush(QBrush(QColor("#fbfcfa")))
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def drawBackground(self, painter, rect) -> None:
        painter.fillRect(rect, self.backgroundBrush())
        if not self.window.show_grid or self.window.grid_size <= 0:
            return
        step = float(self.window.grid_size)
        left = math.floor(rect.left() / step) * step
        top = math.floor(rect.top() / step) * step
        pen = QPen(QColor("#e1e7e3"))
        pen.setWidth(0)
        painter.setPen(pen)
        x = left
        while x <= rect.right():
            painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
            x += step
        y = top
        while y <= rect.bottom():
            painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
            y += step

    def draw(self) -> None:
        scene = self.scene()
        self._wire_preview_item = None
        self._snap_indicator_item = None
        self._placement_preview_items = []
        scene.clear()
        cell = self.window.active_cell()
        if not cell or cell.get("type") != "schematic":
            text = scene.addText("No cell open")
            text.setDefaultTextColor(QColor("#c8d0ca"))
            font = text.font()
            font.setPointSize(24)
            text.setFont(font)
            rect = text.boundingRect()
            text.setPos(-rect.width() / 2, -rect.height() / 2)
            if cell and cell.get("type") == "generated_hb":
                text.setPlainText("Generated Julia HB block")
                rect = text.boundingRect()
                text.setPos(-rect.width() / 2, -rect.height() / 2)
            return
        selected = self.window.selected
        for net in cell.get("nets", []):
            endpoints = net.get("endpoints", [])
            if len(endpoints) < 2:
                continue
            routes = net.get("route_segments", [])
            if not routes:
                hub = endpoints[0]
                hub_inst = self.window.find_instance(hub.get("instance_uid"))
                if not hub_inst:
                    continue
                a = port_point(hub_inst, str(hub.get("port")))
                routes = []
                for idx, ep in enumerate(endpoints[1:], start=1):
                    inst = self.window.find_instance(ep.get("instance_uid"))
                    if not inst:
                        continue
                    b = port_point(inst, str(ep.get("port")))
                    routes.append({"wire_id": f"{net.get('id')}_{idx}", "points": self.window.auto_route_points(hub, ep) or orthogonal_points(a, b), "auto": True})
            for route in routes:
                points = route.get("points", [])
                should_repair = (
                    not points
                    or self.window.route_crosses_block(route)
                )
                if should_repair:
                    source = route.get("source", endpoints[0])
                    target = route.get("target", endpoints[-1])
                    source_inst = self.window.find_instance(source.get("instance_uid"))
                    target_inst = self.window.find_instance(target.get("instance_uid"))
                    if not source_inst or not target_inst:
                        continue
                    points = self.window.auto_route_points(
                        source,
                        target,
                        exclude_wire_id=str(route.get("wire_id", "")),
                    )
                    route["points"] = points
                    route["auto"] = True
                item = WireItem(str(net.get("id")), str(route.get("wire_id", net.get("id"))), points, bool(selected and selected.kind == "net" and selected.id == net.get("id")))
                scene.addItem(item)
        # Draw junction dots wherever 3+ wire-segment endpoints share the same position
        point_counts: dict[tuple[float, float], int] = {}
        for net in cell.get("nets", []):
            for route in net.get("route_segments", []):
                pts = route.get("points", [])
                if pts:
                    k0 = (round(pts[0][0], 1), round(pts[0][1], 1))
                    kn = (round(pts[-1][0], 1), round(pts[-1][1], 1))
                    point_counts[k0] = point_counts.get(k0, 0) + 1
                    point_counts[kn] = point_counts.get(kn, 0) + 1
        dot_pen = QPen(Qt.PenStyle.NoPen)
        dot_brush = QBrush(QColor("#26352f"))
        for (px, py), count in point_counts.items():
            if count >= 3:
                dot = QGraphicsEllipseItem(px - 5, py - 5, 10, 10)
                dot.setBrush(dot_brush)
                dot.setPen(dot_pen)
                dot.setZValue(6)
                scene.addItem(dot)
        coupled_uids: set[str] = set()
        if selected and selected.kind == "instance":
            sel_inst = next((i for i in cell.get("instances", []) if str(i.get("uid", "")) == selected.id), None)
            if sel_inst and sel_inst.get("type_name") == "K":
                params = sel_inst.get("parameters", {})
                for key in ("inductor_a", "inductor_b"):
                    val = str(params.get(key, "")).strip()
                    if val:
                        coupled_uids.add(val)
        for inst in cell.get("instances", []):
            is_selected = bool(selected and selected.kind == "instance" and selected.id == inst.get("uid"))
            is_coupled = str(inst.get("uid", "")) in coupled_uids
            block = BlockItem(inst, is_selected, coupled=is_coupled)
            scene.addItem(block)
        for pin in cell.get("pins", []):
            self.add_text_tag("pin", pin, QColor("#8e4b32"))
        for label in cell.get("labels", []):
            self.add_text_tag("label", label, QColor("#236a5b"))
        self.draw_validation_markers(cell)
        self.update_placement_preview()
        self.schedule_wire_preview()

    def _nearest_port(self, pos: QPointF, radius: float = 30.0) -> tuple[dict | None, "QPointF | None"]:
        cell = self.window.active_cell()
        if not cell:
            return None, None
        exclude_uid = str(self.pending_wire.get("instance_uid", "")) if self.pending_wire else ""
        best_port: dict | None = None
        best_pos: QPointF | None = None
        best_dist = radius
        for inst in cell.get("instances", []):
            if str(inst.get("uid", "")) == exclude_uid:
                continue
            for port_name in inst.get("port_names", []):
                pp = port_point(inst, str(port_name))
                dist = math.hypot(pp.x() - pos.x(), pp.y() - pos.y())
                if dist < best_dist:
                    best_dist = dist
                    best_port = {"instance_uid": str(inst.get("uid", "")), "port": str(port_name)}
                    best_pos = pp
        return best_port, best_pos

    def _show_snap_indicator(self, pos: QPointF, radius: float) -> None:
        ind = QGraphicsEllipseItem(pos.x() - radius, pos.y() - radius, radius * 2, radius * 2)
        ind.setPen(QPen(QColor("#2f6fed"), 2))
        ind.setBrush(QBrush(QColor(47, 111, 237, 60)))
        ind.setZValue(20)
        self.scene().addItem(ind)
        self._snap_indicator_item = ind

    def _refresh_wire_preview(self) -> None:
        pos = self.preview_pos
        scene = self.scene()
        if self._snap_indicator_item and self._snap_indicator_item.scene():
            scene.removeItem(self._snap_indicator_item)
            self._snap_indicator_item = None
        if not self.pending_wire or pos is None:
            if self._wire_preview_item and self._wire_preview_item.scene():
                scene.removeItem(self._wire_preview_item)
                self._wire_preview_item = None
            if pos is not None and self.window.mode in {"wire", "pin", "label"} and not self.window.pending_instance:
                _, start_snap_pos = self._nearest_port(pos, radius=40.0)
                if start_snap_pos:
                    self._show_snap_indicator(start_snap_pos, 13.0)
            return
        snap_port, snap_pos = self._nearest_port(pos)
        effective_pos = snap_pos if snap_pos else pos
        if snap_pos:
            self._show_snap_indicator(snap_pos, 8.0)
        inst = self.window.find_instance(self.pending_wire.get("instance_uid"))
        if not inst:
            return
        start = port_point(inst, str(self.pending_wire.get("port")))
        if snap_port:
            preview_points = self.window.auto_route_points(self.pending_wire, snap_port)
        else:
            obstacles = self.window.route_obstacles(exclude_uids=set())
            occupied = self.window.existing_wire_segments()
            escape_obstacles = self.window.route_obstacles(exclude_uids={str(self.pending_wire.get("instance_uid", ""))})
            escape = clear_port_escape_point(inst, str(self.pending_wire.get("port")), escape_obstacles)
            middle = routed_orthogonal_points(escape, effective_pos, obstacles, occupied_segments=occupied)
            preview_points = compact_points([[start.x(), start.y()], [escape.x(), escape.y()]] + middle)
        path = path_from_points(preview_points)
        pen = QPen(QColor("#2f6fed"), 2, Qt.PenStyle.DashLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        if self._wire_preview_item is None or self._wire_preview_item.scene() is None:
            self._wire_preview_item = QGraphicsPathItem(path)
            self._wire_preview_item.setPen(pen)
            self._wire_preview_item.setZValue(19)
            scene.addItem(self._wire_preview_item)
        else:
            self._wire_preview_item.setPath(path)

    def schedule_wire_preview(self) -> None:
        if not self._wire_preview_timer.isActive():
            self._wire_preview_timer.start()

    def clear_placement_preview(self) -> None:
        scene = self.scene()
        for item in self._placement_preview_items:
            if item.scene() is scene:
                scene.removeItem(item)
        self._placement_preview_items = []

    def update_preview_position_from_cursor(self) -> None:
        viewport_pos = self.viewport().mapFromGlobal(QCursor.pos())
        if self.viewport().rect().contains(viewport_pos):
            self.preview_pos = self.mapToScene(viewport_pos)

    def update_placement_preview(self) -> None:
        self.clear_placement_preview()
        pos = self.preview_pos
        if pos is None:
            return
        scene = self.scene()
        pen = QPen(QColor("#2f6fed"), 1.5, Qt.PenStyle.DashLine)
        brush = QBrush(QColor(47, 111, 237, 34))
        if self.window.pending_instance:
            ref = self.window.find_library_item(self.window.pending_instance)
            ports = list(ref.get("port_names", [])) if ref else []
            symbol = (ref.get("symbol") if ref else None) or default_symbol(ports)
            w = float(symbol.get("width", 120))
            h = float(symbol.get("height", 70))
            point = self.window.snap_point(pos)
            rect = QGraphicsRectItem(-w / 2, -h / 2, w, h)
            rect.setPos(point)
            rect.setPen(pen)
            rect.setBrush(brush)
            rect.setZValue(20)
            scene.addItem(rect)
            self._placement_preview_items.append(rect)
            label = QGraphicsSimpleTextItem(str(self.window.pending_instance))
            label.setBrush(QBrush(QColor("#2f6fed")))
            label.setPos(point.x() + w / 2 + 8, point.y() - h / 2)
            label.setZValue(21)
            scene.addItem(label)
            self._placement_preview_items.append(label)
        if self.window.pending_tag:
            text = self.window.pending_tag.get("name", "")
            label = QGraphicsSimpleTextItem(text)
            label.setBrush(QBrush(QColor("#8e4b32" if self.window.pending_tag.get("kind") == "pin" else "#236a5b")))
            label.setFont(QFont("Sans Serif", 10, QFont.Weight.Bold))
            label.setPos(pos.x() + 12, pos.y() - 22)
            label.setZValue(21)
            scene.addItem(label)
            self._placement_preview_items.append(label)

    def add_text_tag(self, kind: str, item: dict[str, Any], color: QColor) -> None:
        selected = bool(self.window.selected and self.window.selected.kind == kind and self.window.selected.id == item.get("id"))
        self.scene().addItem(TagItem(kind, item, color, selected))

    def draw_validation_markers(self, cell: dict[str, Any]) -> None:
        seen: set[str] = set()
        for inst in cell.get("instances", []):
            uid = str(inst.get("uid", ""))
            ref_name = f"{inst.get('source_project')}/{inst.get('type_name')}" if inst.get("source_project") else inst.get("type_name")
            missing = not self.window.find_library_item(str(ref_name)) and not self.window.find_library_item(str(inst.get("type_name", "")))
            duplicate = uid in seen
            seen.add(uid)
            symbol_ports = {str(port.get("port")) for port in inst.get("symbol", {}).get("port_layout", [])}
            actual_ports = {str(port) for port in inst.get("port_names", [])}
            if missing or duplicate or (symbol_ports and symbol_ports != actual_ports):
                pos = inst.get("position", [0, 0])
                if missing:
                    msg = f"Block type '{inst.get('type_name')}' not found in library"
                    text = "!"
                elif duplicate:
                    msg = f"Duplicate instance UID: {uid}"
                    text = "!"
                else:
                    msg = "Symbol port layout does not match actual ports"
                    text = "?"
                self.add_marker(float(pos[0]) + 48, float(pos[1]) - 46, text, msg)
        valid_net_ids = {net.get("id") for net in cell.get("nets", [])}
        all_pins = cell.get("pins", [])
        all_labels = cell.get("labels", [])
        for net in cell.get("nets", []):
            net_id = net.get("id")
            endpoints = net.get("endpoints", [])
            net_has_pin = any(pin.get("net_id") == net_id for pin in all_pins)
            net_has_label = any(label.get("net_id") == net_id for label in all_labels)
            if len(endpoints) >= 2 or net_has_pin or net_has_label:
                continue
            endpoint = next(iter(endpoints), None)
            if not endpoint:
                continue
            inst = self.window.find_instance(endpoint.get("instance_uid"))
            if inst:
                pos = port_point(inst, str(endpoint.get("port", "")))
                msg = f"Net '{net_id}' is dangling; connect it to at least two block ports"
                self.add_marker(pos.x() + 12, pos.y() - 12, "!", msg)
        for item in list(all_pins) + list(all_labels):
            if item.get("net_id") not in valid_net_ids:
                pos = item.get("position", [0, 0])
                kind = "Pin" if item in all_pins else "Label"
                msg = f"{kind} '{item.get('name')}' is attached to an invalid net"
                self.add_marker(float(pos[0]) - 16, float(pos[1]) - 16, "!", msg)

    def add_marker(self, x: float, y: float, text_value: str, error_message: str = "") -> None:
        marker = ValidationMarker(x, y, text_value, error_message)
        self.scene().addItem(marker)

    def draw_preview(self) -> None:
        self.update_placement_preview()

    def mousePressEvent(self, event):
        self.setFocus()
        item = self.itemAt(event.position().toPoint())
        port = self.port_from_item(item)
        pos = self.mapToScene(event.position().toPoint())
        tag = self.tag_from_item(item)
        if not port and self.window.mode in {"wire", "pin", "label"}:
            port, _ = self._nearest_port(pos, radius=40.0)
        if self.window.pending_instance:
            point = self.window.snap_point(pos)
            self.window.place_instance(self.window.pending_instance, [point.x(), point.y()])
            self.window.pending_instance = None
            self.window.mode = "select"
            self.preview_pos = None
            self.clear_placement_preview()
            return
        wire = self.wire_from_item(item)
        if self.window.mode == "wire":
            if port and not self.pending_wire:
                self.pending_wire = port
                self.window.statusBar().showMessage(f"Wire from {port['instance_uid']}.{port['port']}")
                return
            if port and self.pending_wire:
                self.window.finish_wire(self.pending_wire, port)
                self.pending_wire = None
                return
            if not port and self.pending_wire:
                snap_port, _ = self._nearest_port(pos)
                if snap_port:
                    self.window.finish_wire(self.pending_wire, snap_port)
                    self.pending_wire = None
                    return
            if wire and not port and self.pending_wire:
                self.window.finish_wire_to_net(self.pending_wire, wire.net_id, pos)
                self.pending_wire = None
                return
        if self.window.mode == "pin":
            if not port:
                self.window.add_message("Warning", "Pins must be placed by clicking a block port.")
                return
            self.window.add_pin_or_label("pin", port, self.window.pending_tag.get("name") if self.window.pending_tag else None)
            self.window.mode = "select"
            self.window.pending_tag = None
            self.preview_pos = None
            self.clear_placement_preview()
            return
        if self.window.mode == "label":
            if wire:
                self.window.add_pin_or_label_to_net("label", wire.net_id, pos, self.window.pending_tag.get("name") if self.window.pending_tag else None)
            elif port:
                self.window.add_pin_or_label("label", port, self.window.pending_tag.get("name") if self.window.pending_tag else None)
            else:
                self.window.add_message("Warning", "Labels must be placed by clicking a wire or block port.")
                return
            self.window.mode = "select"
            self.window.pending_tag = None
            self.preview_pos = None
            self.clear_placement_preview()
            return
        block = self.block_from_item(item)
        if tag:
            self.window.selected = Selection(tag.kind, tag.tag_id)
            self.window.refresh_inspector()
            self.draw()
            if event.button() == Qt.MouseButton.LeftButton:
                self.drag_snapshot = self.window.current_cell_snapshot()
            super().mousePressEvent(event)
            return
        if block:
            self.window.selected = Selection("instance", str(block.inst.get("uid")))
            self.window.refresh_inspector()
            self.draw()
            if event.button() == Qt.MouseButton.LeftButton:
                self.drag_snapshot = self.window.current_cell_snapshot()
        super().mousePressEvent(event)

    def contextMenuEvent(self, event):
        item = self.itemAt(event.pos())
        port = self.port_from_item(item)
        wire = self.wire_from_item(item)
        tag = self.tag_from_item(item)
        block = self.block_from_item(item)
        menu = QMenu(self)
        if port:
            start_wire = menu.addAction("Start wire")
            add_pin = menu.addAction("Add pin here")
            add_label = menu.addAction("Add label here")
            show_name = menu.addAction("Show port name")
            chosen = menu.exec(event.globalPos())
            if chosen == start_wire:
                self.window.set_mode("wire")
                self.pending_wire = port
            elif chosen == add_pin:
                self.window.add_pin_or_label("pin", port)
            elif chosen == add_label:
                self.window.add_pin_or_label("label", port)
            elif chosen == show_name:
                self.window.add_message("Info", f"{port['instance_uid']}.{port['port']}")
            return
        if tag:
            self.window.selected = Selection(tag.kind, tag.tag_id)
            rename = menu.addAction("Rename pin" if tag.kind == "pin" else "Rename label")
            if tag.kind == "pin":
                reorder = menu.addAction("Change pin order")
            delete = menu.addAction("Delete pin" if tag.kind == "pin" else "Delete label")
            highlight = menu.addAction("Highlight net")
            chosen = menu.exec(event.globalPos())
            if chosen == rename:
                self.window.rename_tag(tag.kind, tag.tag_id)
            elif tag.kind == "pin" and chosen == reorder:
                self.window.edit_port_order()
            elif chosen == delete:
                self.window.delete_tag(tag.kind, tag.tag_id)
            elif chosen == highlight:
                self.window.selected = Selection("net", tag.net_id)
                self.window.refresh_inspector()
                self.draw()
            return
        if wire:
            add_bend = menu.addAction("Add bend")
            delete_wire = menu.addAction("Delete wire")
            add_label = menu.addAction("Add label")
            add_pin = menu.addAction("Add pin")
            highlight = menu.addAction("Highlight net")
            chosen = menu.exec(event.globalPos())
            pos = self.mapToScene(event.pos())
            if chosen == add_bend:
                self.window.add_wire_bend(wire.net_id, wire.wire_id, pos)
            elif chosen == delete_wire:
                self.window.delete_wire(wire.net_id, wire.wire_id)
            elif chosen == add_label:
                self.window.add_pin_or_label_to_net("label", wire.net_id, pos)
            elif chosen == add_pin:
                self.window.add_pin_or_label_to_net("pin", wire.net_id, pos)
            elif chosen == highlight:
                self.window.selected = Selection("net", wire.net_id)
                self.window.refresh_inspector()
                self.draw()
            return
        if block:
            self.window.selected = Selection("instance", str(block.inst.get("uid")))
            props = menu.addAction("Open variables/properties")
            open_block = menu.addAction("Open cell / built-in")
            edit_symbol = menu.addAction("Edit symbol")
            repeat = menu.addAction("Repeat...")
            rotate = menu.addAction("Rotate")
            duplicate = menu.addAction("Duplicate")
            delete = menu.addAction("Delete")
            chosen = menu.exec(event.globalPos())
            if chosen == props:
                self.window.refresh_inspector()
            elif chosen == open_block:
                self.window.open_selected_block()
            elif chosen == edit_symbol:
                self.window.edit_symbol()
            elif chosen == repeat:
                self.window.edit_repeat_settings()
            elif chosen == rotate:
                self.window.rotate_selected()
            elif chosen == duplicate:
                self.window.duplicate_selected()
            elif chosen == delete:
                self.window.delete_selected()
            return
        add_instance = menu.addAction("Add instance")
        add_pin = menu.addAction("Add pin")
        add_label = menu.addAction("Add label")
        start_wiring = menu.addAction("Start wiring")
        paste = menu.addAction("Paste")
        select_all = menu.addAction("Select all")
        fit = menu.addAction("Fit view")
        settings = menu.addAction("Open canvas settings")
        chosen = menu.exec(event.globalPos())
        if chosen == add_instance:
            self.window.show_palette()
        elif chosen == add_pin:
            self.window.set_mode("pin")
        elif chosen == add_label:
            self.window.set_mode("label")
        elif chosen == start_wiring:
            self.window.set_mode("wire")
        elif chosen == paste:
            self.window.paste_selection()
        elif chosen == select_all:
            self.window.select_all()
        elif chosen == fit:
            self.window.fit_canvas()
        elif chosen == settings:
            self.window.open_canvas_settings()

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        cell = self.window.active_cell()
        if not cell:
            return
        changed = False
        moved_uids: set[str] = set()
        for item in self.scene().selectedItems():
            block = self.block_from_item(item)
            if block:
                point = self.window.snap_point(block.pos())
                block.inst["position"] = [round(point.x(), 3), round(point.y(), 3)]
                moved_uids.add(str(block.inst.get("uid", "")))
                changed = True
            tag = self.tag_from_item(item)
            if tag:
                self.window.set_tag_position(tag.kind, tag.tag_id, tag.pos())
                if tag.kind == "label":
                    label = self.window.find_tag(tag.kind, tag.tag_id)
                    if label:
                        label["kind"] = "label"
                        self.window.constrain_label_to_net(label)
                changed = True
        if changed:
            for inst in cell.get("instances", []):
                if inst.get("uid") in moved_uids:
                    if self.window.check_collision(cell, inst, exclude_uids={inst.get("uid")}):
                        self.window.add_message("Warning", "Cannot move blocks: would cause overlap. Move reverted.")
                        if self.drag_snapshot:
                            import json
                            snapshot = json.loads(self.drag_snapshot)
                            for snap_inst in snapshot.get("instances", []):
                                if snap_inst.get("uid") in moved_uids:
                                    matching = next((i for i in cell.get("instances", []) if i.get("uid") == snap_inst.get("uid")), None)
                                    if matching:
                                        matching["position"] = snap_inst.get("position", [0, 0])
                        self.draw()
                        self.drag_snapshot = None
                        return
            if self.drag_snapshot:
                self.window.push_undo_snapshot(self.drag_snapshot)
            for uid in moved_uids:
                self.window.update_routes_for_instance(uid)
            self.window.mark_dirty(cell)
            self.draw()
        self.drag_snapshot = None

    def mouseMoveEvent(self, event):
        self.preview_pos = self.mapToScene(event.position().toPoint())
        super().mouseMoveEvent(event)
        moved_uids: set[str] = set()
        if self.window.snap_to_grid and event.buttons() & Qt.MouseButton.LeftButton:
            for item in self.scene().selectedItems():
                if self.block_from_item(item) or self.tag_from_item(item):
                    item.setPos(self.window.snap_point(item.pos()))
        if event.buttons() & Qt.MouseButton.LeftButton:
            for item in self.scene().selectedItems():
                block = self.block_from_item(item)
                if block:
                    point = self.window.snap_point(block.pos()) if self.window.snap_to_grid else block.pos()
                    block.inst["position"] = [round(point.x(), 3), round(point.y(), 3)]
                    moved_uids.add(str(block.inst.get("uid", "")))
                tag = self.tag_from_item(item)
                if tag and tag.kind == "label":
                    label = self.window.find_tag(tag.kind, tag.tag_id)
                    if label:
                        label["position"] = [round(item.pos().x(), 3), round(item.pos().y(), 3)]
                        label["kind"] = "label"
                        self.window.constrain_label_to_net(label)
                        constrained_pos = label.get("position", [0, 0])
                        item.setPos(constrained_pos[0], constrained_pos[1])
            for uid in moved_uids:
                self.window.update_route_endpoints_for_instance(uid)
            if moved_uids:
                self.update_wire_items()
        if self.window.pending_instance or self.window.pending_tag:
            self.update_placement_preview()
        if self.window.mode in {"wire", "pin", "label"} or self.pending_wire:
            self.schedule_wire_preview()

    def update_wire_items(self) -> None:
        cell = self.window.active_cell()
        if not cell:
            return
        route_points: dict[tuple[str, str], list[list[float]]] = {}
        for net in cell.get("nets", []):
            for route in net.get("route_segments", []):
                route_points[(str(net.get("id")), str(route.get("wire_id", net.get("id"))))] = route.get("points", [])
        for item in self.scene().items():
            if isinstance(item, WireItem):
                points = route_points.get((item.net_id, item.wire_id))
                if points:
                    item.points = points
                    item.setPath(path_from_points(points))

    def wheelEvent(self, event):
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.scale(factor, factor)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_R and not event.isAutoRepeat():
            inst = self.window.find_instance(self.window.selected.id if self.window.selected and self.window.selected.kind == "instance" else None)
            if inst:
                self.window.record_undo()
                inst["rotation_degrees"] = (inst.get("rotation_degrees", 0) + 90) % 360
                self.window.update_routes_for_instance(str(inst.get("uid", "")))
                self.window.mark_dirty()
                self.draw()
                return
        super().keyPressEvent(event)

    def port_from_item(self, item: QGraphicsItem | None) -> dict[str, str] | None:
        while item:
            if isinstance(item, PortItem):
                return {"instance_uid": item.uid, "port": item.port}
            item = item.parentItem()
        return None

    def block_from_item(self, item: QGraphicsItem | None) -> BlockItem | None:
        while item:
            if isinstance(item, BlockItem):
                return item
            item = item.parentItem()
        return None

    def wire_from_item(self, item: QGraphicsItem | None) -> WireItem | None:
        while item:
            if isinstance(item, WireItem):
                return item
            item = item.parentItem()
        return None

    def tag_from_item(self, item: QGraphicsItem | None) -> TagItem | None:
        while item:
            if isinstance(item, TagItem):
                return item
            item = item.parentItem()
        return None


class InstancePalette(QDialog):
    def __init__(self, items: list[dict[str, Any]], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add Instance")
        self.items = items
        self.selected_name: str | None = None
        layout = QVBoxLayout(self)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search built-ins, local cells, imported cells")
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Name", "Ports"])
        self.tree.header().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.search)
        layout.addWidget(self.tree)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        layout.addWidget(buttons)
        buttons.rejected.connect(self.reject)
        self.search.textChanged.connect(self.populate)
        self.tree.itemDoubleClicked.connect(self.choose)
        self.setMinimumSize(640, 520)
        self.populate()

    def category_path(self, item: dict[str, Any]) -> list[str]:
        source = str(item.get("source", "local"))
        if source == "built-in":
            group = str(item.get("group") or "Other Built-ins")
            return ["Built-ins"] + [part.strip() for part in group.split("/") if part.strip()]
        if source.startswith("imported:"):
            alias = source.split(":", 1)[1] or str(item.get("source_project", "imported"))
            return ["Imported Projects", alias]
        return ["Current Project"]

    def get_or_create_group(self, roots: dict[tuple[str, ...], QTreeWidgetItem], path: list[str]) -> QTreeWidgetItem:
        parent = self.tree.invisibleRootItem()
        current_path: list[str] = []
        current_item = None
        for part in path:
            current_path.append(part)
            key = tuple(current_path)
            current_item = roots.get(key)
            if current_item is None:
                current_item = QTreeWidgetItem([part, ""])
                current_item.setFlags(current_item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                roots[key] = current_item
                parent.addChild(current_item)
            parent = current_item
        assert current_item is not None
        return current_item

    def populate(self) -> None:
        q = self.search.text().lower()
        self.tree.clear()
        roots: dict[tuple[str, ...], QTreeWidgetItem] = {}
        ordered = sorted(
            self.items,
            key=lambda item: (
                0 if item.get("source") == "built-in" else 1 if str(item.get("source", "")).startswith("imported:") else 2,
                "/".join(self.category_path(item)),
                str(item.get("name", "")).lower(),
            ),
        )
        for item in ordered:
            name = item["name"]
            group = " / ".join(self.category_path(item))
            ports = ", ".join(item.get("port_names", [])) or "no ports"
            if q and q not in name.lower() and q not in group.lower() and q not in ports.lower():
                continue
            parent = self.get_or_create_group(roots, self.category_path(item))
            row = QTreeWidgetItem([name, ports])
            row.setToolTip(0, group)
            row.setData(0, Qt.ItemDataRole.UserRole, name)
            parent.addChild(row)
        self.tree.expandAll()

    def choose(self, row: QTreeWidgetItem) -> None:
        selected = row.data(0, Qt.ItemDataRole.UserRole)
        if not selected:
            return
        self.selected_name = selected
        self.accept()


class ProjectStartDialog(QDialog):
    def __init__(self, recent_paths: list[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Select Project")
        self.choice: str | None = None
        self.path: str | None = None
        self.setMinimumWidth(680)
        layout = QVBoxLayout(self)
        title = QLabel("Open a project folder or create a new one.")
        title.setFont(QFont("Sans Serif", 12, QFont.Weight.Bold))
        layout.addWidget(title)
        self.recent = QListWidget()
        self.recent.itemSelectionChanged.connect(self.update_summary)
        self.recent.itemDoubleClicked.connect(self.open_selected_recent)
        for path in recent_paths:
            item = QListWidgetItem(path)
            item.setData(Qt.ItemDataRole.UserRole, path)
            self.recent.addItem(item)
        layout.addWidget(QLabel("Recent projects"))
        layout.addWidget(self.recent)
        self.summary = QLabel("No recent project selected.")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        buttons_row = QWidget()
        buttons_layout = QVBoxLayout(buttons_row)
        open_existing = QPushButton("Open Existing Project Folder")
        create_new = QPushButton("Create New Project")
        import_reference = QPushButton("Open Then Reference Another Project")
        browse_location = QPushButton("Browse Project Location")
        buttons_layout.addWidget(open_existing)
        buttons_layout.addWidget(create_new)
        buttons_layout.addWidget(import_reference)
        buttons_layout.addWidget(browse_location)
        layout.addWidget(buttons_row)
        close_buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        layout.addWidget(close_buttons)
        open_existing.clicked.connect(lambda: self.pick_folder("open"))
        create_new.clicked.connect(lambda: self.pick_folder("new"))
        import_reference.clicked.connect(lambda: self.pick_folder("open_and_reference"))
        browse_location.clicked.connect(lambda: self.pick_folder("open"))
        close_buttons.rejected.connect(self.reject)
        if self.recent.count():
            self.recent.setCurrentRow(0)

    def update_summary(self) -> None:
        item = self.recent.currentItem()
        if not item:
            self.summary.setText("No recent project selected.")
            return
        path = Path(item.data(Qt.ItemDataRole.UserRole))
        self.summary.setText(project_folder_summary(path))

    def open_selected_recent(self, item: QListWidgetItem) -> None:
        self.choice = "open"
        self.path = item.data(Qt.ItemDataRole.UserRole)
        self.accept()

    def pick_folder(self, choice: str) -> None:
        title = "Create Project Folder" if choice == "new" else "Open Project Folder"
        directory = QFileDialog.getExistingDirectory(self, title)
        if not directory:
            return
        self.choice = choice
        self.path = directory
        self.accept()





_TEST_MANIFEST = [
    # (test_name, circuit_folder, target_json, force_mode)
    # force_mode: 's' strips x-params, 'x' keeps x-params
    ("add_drop_s",          "example_add_drop",          "add_drop.json",          "s"),
    ("add_drop_shunt_s",    "example_add_drop_shunt",    "add_drop_shunt.json",    "s"),
    ("double_pumped_jpa_s", "example_double_pumped_Jpa", "double_pumped_JPA.json", "s"),
    ("double_pumped_jpa_x", "example_double_pumped_Jpa", "double_pumped_JPA.json", "x"),
    ("flux_pumped_s",       "example_flux_pumped_JPA",   "testbench.json",         "s"),
    ("flux_pumped_x",       "example_flux_pumped_JPA",   "testbench.json",         "x"),
    ("full_setup_s",        "example_full",              "full_setup.json",         "s"),
    ("jpa_s",               "example_JPA",               "JPA.json",               "s"),
    ("jpa_x",               "example_JPA",               "JPA.json",               "x"),
    ("snail_s",             "example_snail",             "testbench.json",         "s"),
    ("snail_x",             "example_snail",             "testbench.json",         "x"),
    ("two_twpa_s",          "example_two_twpa",          "two_twpas_series.json",  "s"),
    ("two_twpa_x",          "example_two_twpa",          "two_twpas_series.json",  "x"),
    ("twpa_s",              "example_twpa",              "twpa.json",              "s"),
    ("twpa_x",              "example_twpa",              "twpa.json",              "x"),
]


def _compare_csvs_for_test(path_a: Path, path_b: Path, tol: float = 1e-4) -> dict:
    try:
        import numpy as np
        data_a = np.loadtxt(str(path_a), delimiter=",")
        data_b = np.loadtxt(str(path_b), delimiter=",")
        data_a = np.atleast_2d(data_a)
        data_b = np.atleast_2d(data_b)
        if data_a.shape != data_b.shape:
            return {"status": "fail", "reason": f"shape {data_a.shape} vs {data_b.shape}"}
        compare_a = data_a[:, 1:] if data_a.shape[1] > 1 else data_a
        compare_b = data_b[:, 1:] if data_b.shape[1] > 1 else data_b
        abs_err = np.abs(compare_a - compare_b)
        # Skip entries where both values are below the noise floor — these are
        # numerically zero and their exact value is simulation noise (e.g. S11
        # at -4000 dB vs -6000 dB: both are meaningless and should not fail).
        noise_floor = 1e-6
        significant = (np.abs(compare_a) > noise_floor) | (np.abs(compare_b) > noise_floor)
        significant_err = abs_err[significant]
        max_abs = float(np.max(significant_err)) if significant_err.size else 0.0
        freq_delta = float(np.max(np.abs(data_a[:, 0] - data_b[:, 0]))) if data_a.shape[1] > 1 else 0.0
        ok = max_abs < tol and freq_delta < tol
        return {"status": "ok" if ok else "fail", "max_abs": max_abs, "freq_delta": freq_delta}
    except Exception as exc:
        return {"status": "fail", "reason": str(exc)}


class TestSuiteDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Test Suite")
        self.setMinimumSize(760, 480)
        self._processes: dict[str, tuple[str, int, QProcess]] = {}  # run_id -> (test_name, row, process)

        layout = QVBoxLayout(self)

        self._status_label = QLabel("")

        self._table = QTableWidget(len(_TEST_MANIFEST), 5)
        self._table.setHorizontalHeaderLabels(["Test", "Pipeline", "Status", "Max Error", "Details"])
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        for row, (name, _, _, _) in enumerate(_TEST_MANIFEST):
            self._table.setItem(row, 0, QTableWidgetItem(name))
            self._table.setItem(row, 1, QTableWidgetItem(""))
            self._set_status(row, "pending")
        layout.addWidget(self._table)
        layout.addWidget(self._status_label)

        btn_row = QWidget()
        btn_layout = QHBoxLayout(btn_row)
        btn_layout.setContentsMargins(0, 0, 0, 0)
        self._run_btn = QPushButton("Run All Tests")
        self._run_btn.clicked.connect(self._run_all)
        self._stop_btn = QPushButton("Stop All")
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(self._stop_all)
        btn_layout.addWidget(self._run_btn)
        btn_layout.addWidget(self._stop_btn)
        btn_layout.addStretch()
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btn_layout.addWidget(close_btn)
        layout.addWidget(btn_row)

    def _set_status(self, row: int, status: str, error: str = "", max_abs: float | None = None) -> None:
        colors = {
            "pending": "#888888",
            "running": "#cc9900",
            "pass":    "#22aa44",
            "fail":    "#cc2222",
            "error":   "#cc2222",
        }
        labels = {"pending": "—", "running": "Running…", "pass": "Pass", "fail": "Fail", "error": "Error"}
        item = QTableWidgetItem(labels.get(status, status))
        color = colors.get(status, "#888888")
        item.setForeground(QBrush(QColor(color)))
        self._table.setItem(row, 2, item)
        if max_abs is not None:
            self._table.setItem(row, 3, QTableWidgetItem(f"{max_abs:.3e}"))
        elif status in ("pending", "running"):
            self._table.setItem(row, 3, QTableWidgetItem(""))
        self._table.setItem(row, 4, QTableWidgetItem(error))
        self._update_summary()

    def _update_summary(self) -> None:
        total = self._table.rowCount()
        passed = failed = running = 0
        for row in range(total):
            s = (self._table.item(row, 2) or QTableWidgetItem("")).text()
            if s == "Pass":
                passed += 1
            elif s in ("Fail", "Error"):
                failed += 1
            elif s == "Running…":
                running += 1
        done = passed + failed
        parts = [f"{done}/{total} done", f"{passed} passed"]
        if failed:
            parts.append(f"{failed} failed")
        if running:
            parts.append(f"{running} running")
        self._status_label.setText("  ".join(parts))

    def _run_all(self) -> None:
        self._run_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        for row in range(self._table.rowCount()):
            self._set_status(row, "pending")
        for row, (test_name, circuit_dir, target_json, force_mode) in enumerate(_TEST_MANIFEST):
            self._launch_test(row, test_name, circuit_dir, target_json, force_mode)

    def _stop_all(self) -> None:
        for run_id, (_, row, proc) in list(self._processes.items()):
            if proc.state() != QProcess.ProcessState.NotRunning:
                proc.kill()
                self._set_status(row, "error", "Stopped by user")
        self._processes.clear()
        self._run_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)

    def _launch_test(self, row: int, test_name: str, circuit_dir: str, target_json: str, force_mode: str) -> None:
        circuit_path = ROOT / "reference_circuits" / circuit_dir
        if not circuit_path.exists():
            self._set_status(row, "error", f"Circuit dir not found: {circuit_dir}")
            return
        ref_root = LOGIC_DIR / "references" / test_name
        if not ref_root.exists():
            self._set_status(row, "error", f"Reference dir not found: {test_name}")
            return

        PIPELINE_DATA_DIR.mkdir(parents=True, exist_ok=True)
        temp = Path(tempfile.mkdtemp(prefix="guiV2_pipeline_test_", dir=str(PIPELINE_DATA_DIR)))
        run_id = temp.name

        self._copy_circuit_dir(circuit_path, temp, target_json=target_json, force_mode=force_mode)

        target = temp / target_json
        if not target.exists():
            self._set_status(row, "error", f"Target JSON not found: {target_json}")
            return

        self._table.setItem(row, 1, QTableWidgetItem(run_id))
        rel = target.relative_to(DATA_DIR)
        self._set_status(row, "running")

        proc = QProcess(self)
        proc.setWorkingDirectory(str(LOGIC_DIR))
        proc.finished.connect(
            lambda code, _status, rid=run_id, r=row, tn=test_name, p=proc:
                self._on_test_finished(rid, r, tn, code, p)
        )
        self._processes[run_id] = (test_name, row, proc)
        proc.start("bash", [str(LOGIC_DIR / "run_pipeline.sh"), str(rel)])

    def _copy_circuit_dir(self, circuit_path: Path, dest: Path, *, target_json: str = "", force_mode: str = "", _visited: set | None = None) -> None:
        if _visited is None:
            _visited = set()
        if circuit_path in _visited:
            return
        _visited.add(circuit_path)

        for src in circuit_path.glob("*.json"):
            if src.name.endswith(".bak") or src.name == "project.json":
                continue
            data = json.loads(src.read_text(encoding="utf-8"))
            if src.name == target_json:
                if force_mode == "s":
                    data["x-params"] = False
                    data["x_params"] = False
                elif force_mode == "x":
                    data["x-params"] = True
                    data["x_params"] = True
            (dest / src.name).write_text(json.dumps(data, indent=2), encoding="utf-8")

            for inst in data.get("instances", []):
                type_name = inst.get("type_name", "")
                if "/" not in type_name:
                    continue
                dep_circuit_name = type_name.split("/")[0]
                dep_circuit_path = ROOT / "reference_circuits" / dep_circuit_name
                if dep_circuit_path.exists():
                    dep_dest = dest / dep_circuit_name
                    dep_dest.mkdir(exist_ok=True)
                    self._copy_circuit_dir(dep_circuit_path, dep_dest, _visited=_visited)

    def _on_test_finished(self, run_id: str, row: int, test_name: str, code: int, proc: QProcess) -> None:
        self._processes.pop(run_id, None)
        proc.deleteLater()
        if code != 0:
            self._set_status(row, "error", f"Pipeline exit code {code}")
        else:
            self._compare_results(run_id, row, test_name)
        if not self._processes:
            self._run_btn.setEnabled(True)
            self._stop_btn.setEnabled(False)

    def _compare_results(self, run_id: str, row: int, test_name: str) -> None:
        out_dir = LOGIC_DIR / "outputs" / run_id
        ref_root = LOGIC_DIR / "references" / test_name
        ref_dirs = [d for d in ref_root.iterdir() if d.is_dir() and not d.name.endswith(".log")]
        if not ref_dirs:
            self._set_status(row, "error", "No reference pipeline dir found")
            return

        ref_dir = sorted(ref_dirs)[0]
        all_results: list[dict] = []

        for manifest_name in ("cache_manifest.json", "x_cache_manifest.json"):
            out_manifest_path = out_dir / "cache" / manifest_name
            if not out_manifest_path.exists():
                continue
            ref_manifest_path = ref_dir / "cache" / manifest_name
            if not ref_manifest_path.exists():
                self._set_status(row, "error", f"Reference missing {manifest_name}")
                return
            try:
                out_manifest = json.loads(out_manifest_path.read_text())
                ref_manifest = json.loads(ref_manifest_path.read_text())
            except Exception as exc:
                self._set_status(row, "error", f"Manifest read error: {exc}")
                return

            # Group output entries by base key.
            import itertools as _itertools
            def _base(k):
                return k.split("__p_")[0] if "__p_" in k else k

            # Build a map: base_key -> list of (ref_key, ref_entry) candidates
            ref_by_base: dict = {}
            for rk, re in ref_manifest.items():
                ref_by_base.setdefault(_base(rk), []).append((rk, re))

            # Group output entries by base key
            out_by_base: dict = {}
            for ok, oe in out_manifest.items():
                out_by_base.setdefault(_base(ok), []).append((ok, oe))

            for base_key, out_entries in out_by_base.items():
                ref_candidates = ref_by_base.get(base_key, [])
                if not ref_candidates:
                    continue

                # Exact-key matches pair first; leftovers use best-error matching.
                matched_ref_keys: set = set()
                pairs: list = []

                # Pass 1: exact key matches
                for ok, oe in out_entries:
                    if ok in ref_manifest:
                        pairs.append((ok, oe, ref_manifest[ok]))
                        matched_ref_keys.add(ok)

                # Pass 2: fallback — find best-error ref for each unmatched output
                unmatched_out = [(ok, oe) for ok, oe in out_entries if ok not in ref_manifest]
                unused_refs = [(rk, re) for rk, re in ref_candidates if rk not in matched_ref_keys]

                for ok, oe in unmatched_out:
                    out_csv = Path(str(oe.get("csv", "")))
                    if not out_csv.exists() or not unused_refs:
                        continue
                    best_rk, best_re, best_err = None, None, float("inf")
                    for rk, re in unused_refs:
                        rc = ref_dir / "cache" / Path(str(re.get("csv", ""))).name
                        if not rc.exists():
                            continue
                        r = _compare_csvs_for_test(out_csv, rc)
                        ma = r.get("max_abs")
                        err = ma if ma is not None else float("inf")
                        if r.get("status") == "fail" and r.get("reason"):
                            err = float("inf")
                        if err < best_err:
                            best_err, best_rk, best_re = err, rk, re
                    if best_rk is not None:
                        pairs.append((ok, oe, best_re))
                        unused_refs = [(rk, re) for rk, re in unused_refs if rk != best_rk]

                for ok, oe, re in pairs:
                    out_csv = Path(str(oe.get("csv", "")))
                    ref_csv = ref_dir / "cache" / Path(str(re.get("csv", ""))).name
                    if not out_csv.exists() or not ref_csv.exists():
                        continue
                    result = _compare_csvs_for_test(out_csv, ref_csv)
                    result["key"] = ok
                    all_results.append(result)

        if not all_results:
            self._set_status(row, "error", "No CSVs compared")
            return

        failed = [r for r in all_results if r.get("status") != "ok"]
        max_abs = max((r.get("max_abs", 0.0) or 0.0) for r in all_results)
        if failed:
            worst = max(failed, key=lambda r: r.get("max_abs", 0.0) or 0.0)
            detail = worst.get("reason") or f"{worst['key']}: max_abs={worst.get('max_abs', 0):.3e}"
            self._set_status(row, "fail", detail, max_abs)
        else:
            self._set_status(row, "pass", "", max_abs)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Circuit Project GUI V2")
        self.resize(1400, 900)
        self.project: dict[str, Any] | None = None
        self.project_dir: Path | None = None
        self.active_cell_id: str | None = None
        self.open_cell_ids: list[str] = []
        self.selected: Selection | None = None
        self.mode = "select"
        self.pending_instance: str | None = None
        self.pending_tag: dict[str, str] | None = None
        self.settings = QSettings("CircuitProjectGUI", "guiV2")
        self.builtins = load_builtin_catalog()
        self.process: QProcess | None = None
        self.simulation_processes: dict[str, QProcess] = {}
        self.simulation_targets: dict[str, Path] = {}
        self.simulation_cells: dict[str, str] = {}
        self.undo_stack: list[str] = []
        self.redo_stack: list[str] = []
        self.clipboard: dict[str, Any] | None = None
        self.file_mtimes: dict[str, float] = {}
        self.imported_watch_paths: set[str] = set()
        self.show_grid = True
        self.snap_to_grid = True
        self.grid_size = 20
        self._route_obstacles_cache: dict[tuple[Any, ...], list[QRectF]] = {}
        self.watcher = QFileSystemWatcher(self)
        self.setup_ui()
        self.setup_menus()
        self.setup_shortcuts()
        self.setup_file_safety()
        self.add_message("Info", "Open or create a native project folder.")
        QTimer.singleShot(0, self.startup_checks_then_project)

    def setup_ui(self) -> None:
        self.explorer = QTreeWidget()
        self.explorer.setHeaderHidden(True)
        self.explorer.itemDoubleClicked.connect(self.on_explorer_double_click)
        self.explorer.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.explorer.customContextMenuRequested.connect(self.show_explorer_menu)

        self.canvas = SchematicView(self)
        self.empty = QLabel("No cell open")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.tabs = QTabWidget()
        self.tabs.currentChanged.connect(self.on_tab_changed)
        self.tabs.setTabsClosable(True)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        self.tabs.tabBar().setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tabs.tabBar().customContextMenuRequested.connect(self.show_tab_menu)

        center = QWidget()
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.addWidget(self.tabs)
        self.generated_view = GeneratedCellWidget(self)
        self._force_generated_view_prompt = False
        self._julia_source_view_modes: dict[str, str] = {}
        self.center_stack = QStackedWidget()
        self.center_stack.addWidget(self.canvas)          # index 0
        self.center_stack.addWidget(self.generated_view)  # index 1
        center_layout.addWidget(self.center_stack, 1)

        inspector_content = QWidget()
        self.inspector_layout = QVBoxLayout(inspector_content)
        self.inspector_layout.setContentsMargins(10, 10, 10, 10)
        self.inspector = QScrollArea()
        self.inspector.setWidgetResizable(True)
        self.inspector.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.inspector.setWidget(inspector_content)

        explorer_scroll = QScrollArea()
        explorer_scroll.setWidgetResizable(True)
        explorer_scroll.setWidget(self.explorer)
        self._explorer_scroll = explorer_scroll

        self.main_splitter = QSplitter()
        self.main_splitter.addWidget(self._explorer_scroll)
        self.main_splitter.addWidget(center)
        self.main_splitter.addWidget(self.inspector)
        self.main_splitter.setSizes([280, 820, 330])

        self.messages = QPlainTextEdit()
        self.messages.setReadOnly(True)
        self.results = QTreeWidget()
        self.results.setColumnCount(4)
        self.results.setHeaderLabels(["Name", "Cell", "Rows", "Ref"])
        self.results.itemDoubleClicked.connect(self._on_result_double_clicked)
        results_panel = QWidget()
        results_layout = QVBoxLayout(results_panel)
        results_layout.setContentsMargins(0, 0, 0, 0)
        results_layout.addWidget(self.results)
        results_buttons = QWidget()
        results_buttons_layout = QHBoxLayout(results_buttons)
        open_plot = QPushButton("Open Plot")
        export_data = QPushButton("Export Data")
        clear_results = QPushButton("Clear Results")
        open_plot.clicked.connect(self.open_selected_result_plot)
        export_data.clicked.connect(self.export_selected_result_csv)
        clear_results.clicked.connect(self.clear_results)
        results_buttons_layout.addWidget(open_plot)
        results_buttons_layout.addWidget(export_data)
        results_buttons_layout.addWidget(clear_results)
        results_layout.addWidget(results_buttons)
        self.bottom_tabs = QTabWidget()
        self.bottom_tabs.addTab(self.messages, "Messages")
        self.bottom_tabs.addTab(results_panel, "Results")
        self.results_panel = results_panel

        self.vertical_splitter = QSplitter(Qt.Orientation.Vertical)
        self.vertical_splitter.addWidget(self.main_splitter)
        self.vertical_splitter.addWidget(self.bottom_tabs)
        self.vertical_splitter.setSizes([660, 220])
        self.setCentralWidget(self.vertical_splitter)

        toolbar = QToolBar("Main")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)
        for label, handler in [
            ("Save", self.save_current_cell),
            ("New Cell", self.create_schematic_cell),
            ("Save All", self.save_all),
            ("Update Blocks", self.update_blocks),
            ("Validate", self.validate_current_cell),
            ("Run", self.run_simulation),
            ("Run Tests", self.open_test_suite),
        ]:
            action = QAction(label, self)
            action.triggered.connect(handler)
            toolbar.addAction(action)

    def setup_menus(self) -> None:
        file_menu = self.menuBar().addMenu("File")
        self.add_action(file_menu, "New Project...", self.new_project)
        self.add_action(file_menu, "Open Project Folder...", self.open_project)
        self.recent_menu = file_menu.addMenu("Recent Projects")
        self.add_action(file_menu, "Project Summary", self.show_project_summary)
        self.add_action(file_menu, "Save Current Cell", self.save_current_cell)
        self.add_action(file_menu, "Save All", self.save_all)
        self.add_action(file_menu, "Import Cell JSON...", self.import_cell_json)
        self.add_action(file_menu, "Import Project...", self.reference_project)
        self.add_action(file_menu, "Close Project", self.close_project)
        self.add_action(file_menu, "Quit", self.close)
        self.refresh_recent_menu()

        cell_menu = self.menuBar().addMenu("Cell")
        self.add_action(cell_menu, "Create Schematic Cell...", self.create_schematic_cell)
        self.add_action(cell_menu, "Create Matrix Cell...", self.create_matrix_cell)
        self.add_action(cell_menu, "Create Julia HB Block...", self.create_julia_hb_block)
        self.add_action(cell_menu, "Rename Cell...", self.rename_cell)
        self.add_action(cell_menu, "Duplicate Cell", self.duplicate_cell)
        self.add_action(cell_menu, "Delete Cell", self.delete_cell)
        self.add_action(cell_menu, "Open Cell Properties", self.refresh_inspector)
        self.add_action(cell_menu, "Edit Port Order", self.edit_port_order)
        self.add_action(cell_menu, "Set z0 for Current Cell", self.set_z0)
        self.add_action(cell_menu, "Validate Current Cell", self.validate_current_cell)

        edit_menu = self.menuBar().addMenu("Edit")
        self.add_action(edit_menu, "Undo", self.undo)
        self.add_action(edit_menu, "Redo", self.redo)
        self.add_action(edit_menu, "Copy", self.copy_selection)
        self.add_action(edit_menu, "Paste", self.paste_selection)
        self.add_action(edit_menu, "Delete", self.delete_selected)
        self.add_action(edit_menu, "Select All", self.select_all)

        insert_menu = self.menuBar().addMenu("Insert")
        self.add_action(insert_menu, "Add Instance...", self.show_palette)
        self.add_action(insert_menu, "Wire Mode", lambda: self.set_mode("wire"))
        self.add_action(insert_menu, "Add Exported Pin", lambda: self.set_mode("pin"))
        self.add_action(insert_menu, "Add Text Label", lambda: self.set_mode("label"))

        view_menu = self.menuBar().addMenu("View")
        self.explorer_action = self.add_action(view_menu, "Project Explorer", self._toggle_explorer, checkable=True, checked=True)
        self.inspector_action = self.add_action(view_menu, "Cell Properties", self._toggle_inspector, checkable=True, checked=True)
        self.bottom_action = self.add_action(view_menu, "Debug/Results Panel", self._toggle_bottom, checkable=True, checked=True)
        self.grid_action = self.add_action(view_menu, "Toggle Grid", self.toggle_grid, checkable=True, checked=True)
        self.snap_action = self.add_action(view_menu, "Toggle Snap to Grid", self.toggle_snap, checkable=True, checked=True)
        self.add_action(view_menu, "Fit Canvas", self.fit_canvas)
        self.add_action(view_menu, "Canvas Settings", self.open_canvas_settings)
        self.add_action(view_menu, "Symbol Editor", self.edit_symbol)

        sim_menu = self.menuBar().addMenu("Simulate")
        self.add_action(sim_menu, "Open Simulation Setup", self.edit_simulation_setup)
        self.add_action(sim_menu, "Run Simulation", self.run_simulation)
        self.add_action(sim_menu, "Stop Simulation", self.stop_simulation)
        self.add_action(sim_menu, "Open Latest Results", lambda: self.bottom_tabs.setCurrentWidget(self.results_panel))
        self.add_action(sim_menu, "Clear Results", self.clear_results)
        sim_menu.addSeparator()
        self.add_action(sim_menu, "Run Test Suite", self.open_test_suite)

        help_menu = self.menuBar().addMenu("Help")
        self.add_action(help_menu, "Keyboard Shortcuts", self.show_shortcuts)
        self.add_action(help_menu, "About", self.show_about)

    def update_blocks(self) -> None:
        self.reload_imported_projects(silent=True)
        cell = self.active_cell()
        if not cell or cell.get("type") != "schematic":
            return
        warnings: list[str] = []
        updates: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for inst in cell.get("instances", []):
            ref_name = f"{inst.get('source_project')}/{inst.get('type_name')}" if inst.get("source_project") else inst.get("type_name")
            ref = self.find_library_item(ref_name) or self.find_library_item(inst.get("type_name", ""))
            if not ref:
                warnings.append(f"Missing referenced cell or built-in: {inst.get('type_name')}")
                continue
            ports = ref.get("port_names", [])
            if ports and list(map(str, ports)) != list(map(str, inst.get("port_names", []))):
                warnings.append(f"{inst.get('uid')} port list changed: {', '.join(inst.get('port_names', []))} -> {', '.join(ports)}")
                updates.append((inst, ref))
        if warnings:
            choice = self.popup(
                "Referenced blocks need review",
                "\n".join(warnings) + "\n\nUpdate instance ports and default symbol layouts where possible?",
                "question",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if choice == QMessageBox.StandardButton.Yes:
                self.record_undo()
                for inst, ref in updates:
                    ports = [str(port) for port in ref.get("port_names", [])]
                    inst["port_names"] = ports
                    inst["port_count"] = len(ports)
                    inst["symbol"] = json.loads(json.dumps(ref.get("symbol") or default_symbol(ports)))
                    inst["symbol_port_layout"] = inst["symbol"].get("port_layout", [])
                self.mark_dirty(cell)
                self.refresh_all()
        else:
            self.add_message("Info", "Blocks are current.")

    def add_action(self, menu: QMenu, text: str, slot, checkable: bool = False, checked: bool = False) -> QAction:
        action = QAction(text, self)
        action.setCheckable(checkable)
        if checkable:
            action.setChecked(checked)
            action.toggled.connect(slot)
        else:
            action.triggered.connect(slot)
        menu.addAction(action)
        return action

    def popup(
        self,
        title: str,
        text: str,
        kind: str = "info",
        buttons: QMessageBox.StandardButton = QMessageBox.StandardButton.Ok,
    ) -> QMessageBox.StandardButton:
        box = QMessageBox(self)
        box.setWindowTitle(title)
        box.setText(text)
        box.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        if kind == "warning":
            box.setIcon(QMessageBox.Icon.Warning)
        elif kind == "critical":
            box.setIcon(QMessageBox.Icon.Critical)
        elif kind == "question":
            box.setIcon(QMessageBox.Icon.Question)
        else:
            box.setIcon(QMessageBox.Icon.Information)
        box.setStandardButtons(buttons)
        return box.exec()

    def rebuild_recent_project_menu(self) -> None:
        if not hasattr(self, "recent_menu"):
            return
        self.recent_menu.clear()
        paths = self.recent_project_paths()
        if not paths:
            empty = QAction("No recent projects", self)
            empty.setEnabled(False)
            self.recent_menu.addAction(empty)
            return
        for path in paths:
            action = QAction(path, self)
            action.triggered.connect(lambda _checked=False, p=path: self.load_project_dir(Path(p)))
            self.recent_menu.addAction(action)

    def setup_shortcuts(self) -> None:
        bindings = [
            ("i", self.show_palette),
            ("q", self.refresh_inspector),
            ("o", self.open_selected_block),
            ("p", lambda: self.set_mode("pin")),
            ("l", lambda: self.set_mode("label")),
            ("w", lambda: self.set_mode("wire")),
            ("Esc", self.exit_mode),
            ("Del", self.delete_selected),
            ("Ctrl+S", self.save_current_cell),
            ("Ctrl+Z", self.undo),
            ("Ctrl+Y", self.redo),
            ("Ctrl+Shift+Z", self.redo),
            ("Ctrl+C", self.copy_selection),
            ("Ctrl+V", self.paste_selection),
            ("Ctrl+A", self.select_all),
            ("Ctrl+Tab", self.next_tab),
            ("r", self.rotate_selected),
            ("Ctrl++", lambda: self.zoom_canvas(1.15)),
            ("Ctrl+=", lambda: self.zoom_canvas(1.15)),
            ("Ctrl+-", lambda: self.zoom_canvas(1 / 1.15)),
        ]
        for key, handler in bindings:
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.activated.connect(handler)

    def _toggle_explorer(self, visible: bool) -> None:
        self._explorer_scroll.setVisible(visible)
        if visible:
            sizes = list(self.main_splitter.sizes())
            if sizes[0] < 50:
                total = sum(sizes)
                sizes[0] = 280
                sizes[1] = max(400, total - 280 - sizes[2])
                self.main_splitter.setSizes(sizes)

    def _toggle_inspector(self, visible: bool) -> None:
        self.inspector.setVisible(visible)
        if visible:
            sizes = list(self.main_splitter.sizes())
            if sizes[2] < 50:
                total = sum(sizes)
                sizes[2] = 330
                sizes[1] = max(400, total - sizes[0] - 330)
                self.main_splitter.setSizes(sizes)

    def _toggle_bottom(self, visible: bool) -> None:
        self.bottom_tabs.setVisible(visible)
        if visible:
            vsizes = list(self.vertical_splitter.sizes())
            if vsizes[1] < 50:
                total = sum(vsizes)
                vsizes[1] = 220
                vsizes[0] = max(300, total - 220)
                self.vertical_splitter.setSizes(vsizes)

    def toggle_grid(self, checked: bool) -> None:
        self.show_grid = checked
        self.canvas.draw()

    def toggle_snap(self, checked: bool) -> None:
        self.snap_to_grid = checked

    def snap_point(self, point: QPointF) -> QPointF:
        if not self.snap_to_grid or self.grid_size <= 0:
            return QPointF(point)
        return QPointF(round(point.x() / self.grid_size) * self.grid_size, round(point.y() / self.grid_size) * self.grid_size)

    def open_canvas_settings(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Canvas Settings")
        layout = QFormLayout(dialog)
        grid = QCheckBox()
        grid.setChecked(self.show_grid)
        snap = QCheckBox()
        snap.setChecked(self.snap_to_grid)
        size = QSpinBox()
        size.setRange(5, 200)
        size.setValue(self.grid_size)
        layout.addRow("Show grid", grid)
        layout.addRow("Snap to grid", snap)
        layout.addRow("Grid size", size)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        layout.addRow(buttons)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.show_grid = grid.isChecked()
            self.snap_to_grid = snap.isChecked()
            self.grid_size = size.value()
            if hasattr(self, "grid_action"):
                self.grid_action.setChecked(self.show_grid)
            if hasattr(self, "snap_action"):
                self.snap_action.setChecked(self.snap_to_grid)
            self.canvas.draw()

    def setup_file_safety(self) -> None:
        self.watcher.fileChanged.connect(self.on_file_changed)
        self.autosave_timer = QTimer(self)
        self.autosave_timer.setInterval(30000)
        self.autosave_timer.timeout.connect(self.autosave_dirty_cells)
        self.autosave_timer.start()

    def recent_project_paths(self) -> list[str]:
        return [path for path in self.recent_projects() if Path(path).exists()]

    def remember_project_path(self, path: Path) -> None:
        self.add_recent_project(path)

    def startup_checks_then_project(self) -> None:
        self.prompt_clear_generated_data()
        self.show_project_startup()

    def generated_data_locations(self) -> list[Path]:
        return [PIPELINE_DATA_DIR, LOGIC_DIR / "outputs", LOGIC_DIR / "pipeline_outputs"]

    def generated_entry_count(self, path: Path) -> int:
        if not path.exists():
            return 0
        try:
            return sum(1 for _item in path.rglob("*"))
        except OSError:
            return 0

    def prompt_clear_generated_data(self) -> None:
        oversized = [
            (path, self.generated_entry_count(path))
            for path in self.generated_data_locations()
            if self.generated_entry_count(path) > GENERATED_ENTRY_LIMIT
        ]
        if not oversized:
            return
        details = "\n".join(f"{path}: {count} entries" for path, count in oversized)
        choice = self.popup(
            "Clear generated pipeline data?",
            f"These generated folders have more than {GENERATED_ENTRY_LIMIT} entries:\n\n{details}\n\nClear their contents now?",
            "question",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if choice != QMessageBox.StandardButton.Yes:
            return
        for path, _count in oversized:
            self.clear_generated_location(path)

    def clear_generated_location(self, path: Path) -> None:
        if not path.exists():
            return
        for child in path.iterdir():
            try:
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
            except OSError as exc:
                self.add_message("Warning", f"Could not remove generated item {child}.", str(exc))

    def show_project_startup(self) -> None:
        if self.project:
            return
        dialog = ProjectStartDialog(self.recent_project_paths(), self)
        if dialog.exec() != QDialog.DialogCode.Accepted or not dialog.path:
            return
        path = Path(dialog.path)
        if dialog.choice == "new":
            self.create_project_at(path)
        else:
            self.load_project_dir(path)
            if dialog.choice == "open_and_reference":
                self.reference_project()

    def _confirm_discard_unsaved(self) -> bool:
        if not self.project:
            return True
        dirty = [c for c in self.project.get("cells", {}).values() if c.get("dirty") and not c.get("readOnly")]
        if not dirty:
            return True
        names = ", ".join(c.get("name", "?") for c in dirty[:5])
        if len(dirty) > 5:
            names += f" … and {len(dirty) - 5} more"
        choice = self.popup(
            "Unsaved changes",
            f"The following cells have unsaved changes that will be lost:\n\n{names}\n\nContinue anyway?",
            "question",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        return choice == QMessageBox.StandardButton.Yes

    def create_project_at(self, path: Path) -> None:
        if not self._confirm_discard_unsaved():
            return
        path.mkdir(parents=True, exist_ok=True)
        project = blank_project(clean_name(path.name) or "project", str(path))
        self.project = project
        self.project_dir = path
        self.active_cell_id = None
        self.save_project_metadata()
        self.add_recent_project(path)
        self.refresh_all()
        self.add_message("Info", f"Created project at {path}")

    def exit_mode(self) -> None:
        self.mode = "select"
        self.pending_instance = None
        self.pending_tag = None
        self.canvas.pending_wire = None
        self.canvas.preview_pos = None
        self.canvas.clear_placement_preview()
        self.selected = None
        self.canvas.draw()
        self.refresh_all()
        self.statusBar().showMessage("Select mode")

    def add_message(self, severity: str, text: str, detail: str = "") -> None:
        line = f"[{severity}] {text}"
        if detail:
            line += f"\n{detail}"
        self.messages.appendPlainText(line)
        self.bottom_tabs.setCurrentWidget(self.messages)

    def active_cell(self) -> dict[str, Any] | None:
        if not self.project or not self.active_cell_id:
            return None
        return self.project["cells"].get(self.active_cell_id)

    def find_instance(self, uid: str | None) -> dict[str, Any] | None:
        cell = self.active_cell()
        if not cell:
            return None
        return next((inst for inst in cell.get("instances", []) if inst.get("uid") == uid), None)

    def mark_dirty(self, cell: dict[str, Any] | None = None) -> None:
        cell = cell or self.active_cell()
        if cell and not cell.get("readOnly"):
            cell["dirty"] = True
            self.refresh_tabs()

    def record_undo(self) -> None:
        cell = self.active_cell()
        if not cell or cell.get("readOnly"):
            return
        self.push_undo_snapshot(json.dumps(cell))

    def current_cell_snapshot(self) -> str | None:
        cell = self.active_cell()
        if not cell or cell.get("readOnly"):
            return None
        return json.dumps(cell)

    def push_undo_snapshot(self, snapshot: str | None) -> None:
        if not snapshot:
            return
        if self.undo_stack and self.undo_stack[-1] == snapshot:
            return
        self.undo_stack.append(snapshot)
        if len(self.undo_stack) > 80:
            self.undo_stack.pop(0)
        self.redo_stack.clear()

    def restore_cell_snapshot(self, snapshot: str) -> None:
        current = self.active_cell()
        if not current:
            return
        restored = json.loads(snapshot)
        restored["id"] = current["id"]
        if current.get("fileName"):
            restored["fileName"] = current["fileName"]
        self.project["cells"][current["id"]] = restored
        self.selected = None
        self.refresh_all()

    def undo(self) -> None:
        cell = self.active_cell()
        if not cell or not self.undo_stack:
            return
        self.redo_stack.append(json.dumps(cell))
        self.restore_cell_snapshot(self.undo_stack.pop())

    def redo(self) -> None:
        cell = self.active_cell()
        if not cell or not self.redo_stack:
            return
        self.undo_stack.append(json.dumps(cell))
        self.restore_cell_snapshot(self.redo_stack.pop())

    def new_project(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "Choose New Project Folder")
        if not directory:
            return
        self.create_project_at(Path(directory))

    def open_project(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "Open Project Folder")
        if not directory:
            return
        self.load_project_dir(Path(directory))

    def close_project(self) -> None:
        if not self._confirm_discard_unsaved():
            return
        self.project = None
        self.project_dir = None
        self.active_cell_id = None
        self.open_cell_ids = []
        self.selected = None
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.clipboard = None
        self.file_mtimes.clear()
        if self.watcher.files():
            self.watcher.removePaths(self.watcher.files())
        self.refresh_all()
        self.add_message("Info", "Closed project.")

    def recent_projects(self) -> list[str]:
        value = self.settings.value("recent_projects", [])
        if isinstance(value, str):
            return [value]
        return [str(item) for item in (value or [])]

    def add_recent_project(self, path: Path) -> None:
        items = [str(path)] + [item for item in self.recent_projects() if item != str(path)]
        self.settings.setValue("recent_projects", items[:10])
        self.refresh_recent_menu()

    def refresh_recent_menu(self) -> None:
        if not hasattr(self, "recent_menu"):
            return
        self.recent_menu.clear()
        recents = self.recent_projects()
        if not recents:
            disabled = QAction("No recent projects", self)
            disabled.setEnabled(False)
            self.recent_menu.addAction(disabled)
            return
        for path_text in recents:
            action = QAction(path_text, self)
            action.triggered.connect(lambda _checked=False, p=path_text: self.open_recent_project(p))
            self.recent_menu.addAction(action)

    def open_recent_project(self, path_text: str) -> None:
        path = Path(path_text)
        if not path.exists():
            self.popup("Recent project missing", f"{path} no longer exists.", "warning")
            return
        self.load_project_dir(path)

    def load_project_dir(self, path: Path) -> None:
        if not self._confirm_discard_unsaved():
            return
        project = blank_project(clean_name(path.name) or "project", str(path))
        meta = path / "project.json"
        if meta.exists():
            try:
                project.update(json.loads(meta.read_text(encoding="utf-8")))
                project["path"] = str(path)
            except Exception as exc:
                self.add_message("Warning", "Could not read project.json.", str(exc))
        project["cells"] = {}
        project.setdefault("imports", [])
        project.setdefault("importedCells", {})
        project.setdefault("results", [])
        for file_path in sorted(path.glob("*.json")):
            if file_path.name == "project.json" or file_path.name.endswith(".bak") or ".autosave." in file_path.name:
                continue
            try:
                raw = json.loads(file_path.read_text(encoding="utf-8"))
                if raw.get("type") in {"schematic", "matrix", "generated_hb"}:
                    cell = self.import_pipeline_cell(raw)
                    cell["dirty"] = False
                    cell["fileName"] = file_path.name
                    self.file_mtimes[file_path.name] = file_path.stat().st_mtime
                    project["cells"][cell["id"]] = cell
            except Exception as exc:
                self.add_message("Warning", f"Skipped {file_path.name}.", str(exc))
        self.project = project
        self.project_dir = path
        self.reload_imported_projects(silent=True)
        default = project.get("default_cell")
        first = next((c for c in project["cells"].values() if c.get("name") == default), None) or next(iter(project["cells"].values()), None)
        self.active_cell_id = first.get("id") if first else None
        last_open_names = project.get("gui", {}).get("last_open_tabs", [])
        self.open_cell_ids = [
            cell["id"]
            for name in last_open_names
            for cell in project["cells"].values()
            if cell.get("name") == name
        ]
        if self.active_cell_id and self.active_cell_id not in self.open_cell_ids:
            self.open_cell_ids.insert(0, self.active_cell_id)
        self.refresh_all()
        self.refresh_file_watcher()
        self.add_recent_project(path)
        self.add_message("Info", f"Opened project {project.get('name')} from {path}")

    def save_project_metadata(self) -> None:
        if not self.project or not self.project_dir:
            return
        meta = {
            "name": self.project.get("name"),
            "path": str(self.project_dir),
            "version": 1,
            "default_cell": self.project.get("default_cell", ""),
            "recent_cells": self.project.get("recent_cells", []),
            "imports": self.project.get("imports", []),
            "gui": {
                **self.project.get("gui", {"layout": {}}),
                "last_open_tabs": [
                    self.project["cells"][cell_id].get("name")
                    for cell_id in self.open_cell_ids
                    if cell_id in self.project.get("cells", {}) and not self.project["cells"][cell_id].get("readOnly")
                ],
            },
        }
        (self.project_dir / "project.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    def save_current_cell(self) -> None:
        cell = self.active_cell()
        if not cell:
            return
        if cell.get("readOnly"):
            self.popup("Read-only cell", "Copy imported cells into the project before saving.", "warning")
            return
        if not self.project_dir:
            self.new_project()
            if not self.project_dir:
                return
        issues = self.validate_cell(cell)
        errors = [item for item in issues if item[0] == "Error"]
        for severity, message in issues:
            self.add_message(severity, message)
        if errors:
            choice = self.popup(
                "Validation errors",
                "\n".join(message for _, message in errors) + "\n\nSave anyway?",
                "question",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if choice != QMessageBox.StandardButton.Yes:
                return
        file_name = cell.get("fileName") or f"{clean_name(cell.get('name'))}.json"
        target = self.project_dir / file_name
        if target.exists():
            shutil.copyfile(target, target.with_suffix(target.suffix + ".bak"))
        target.write_text(json.dumps(self.serialize_project_cell(cell), indent=2), encoding="utf-8")
        self.file_mtimes[file_name] = target.stat().st_mtime
        cell["fileName"] = file_name
        cell["dirty"] = False
        self.save_project_metadata()
        self.refresh_all()
        self.refresh_file_watcher()
        self.add_message("Info", f"Saved {target}")

    def save_all(self) -> None:
        if not self.project:
            return
        original = self.active_cell_id
        for cell_id, cell in list(self.project.get("cells", {}).items()):
            if cell.get("readOnly"):
                continue
            self.active_cell_id = cell_id
            self.save_current_cell()
        self.active_cell_id = original
        self.refresh_all()

    def import_cell_json(self) -> None:
        file_name, _ = QFileDialog.getOpenFileName(self, "Import Cell JSON", "", "JSON files (*.json)")
        if not file_name:
            return
        if not self.project:
            self.project = blank_project("imported_cells", "")
        try:
            raw = json.loads(Path(file_name).read_text(encoding="utf-8"))
            cell = self.import_pipeline_cell(raw)
            cell["dirty"] = True
            self.project["cells"][cell["id"]] = cell
            if not self.project.get("default_cell"):
                self.project["default_cell"] = cell["name"]
            self.active_cell_id = cell["id"]
            self.ensure_cell_tab(cell["id"])
            self.refresh_all()
        except Exception as exc:
            self.popup("Import failed", str(exc), "critical")

    def reference_project(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "Reference Project Folder")
        if not directory:
            return
        if not self.project:
            self.new_project()
            if not self.project:
                return
        old_project = self.project
        old_dir = self.project_dir
        old_active = self.active_cell_id
        old_open = list(self.open_cell_ids)
        self.load_project_dir(Path(directory))
        imported_project = self.project
        self.project = old_project
        self.project_dir = old_dir
        self.active_cell_id = old_active
        self.open_cell_ids = old_open
        alias = self.unique_import_alias(clean_name(imported_project.get("name") or Path(directory).name))
        cells = []
        for cell in imported_project.get("cells", {}).values():
            copy = json.loads(json.dumps(cell))
            copy["id"] = str(uuid.uuid4())
            copy["readOnly"] = True
            copy["dirty"] = False
            copy["importAlias"] = alias
            copy["originalName"] = cell.get("name")
            cells.append(copy)
        self.project.setdefault("imports", []).append({"alias": alias, "path": directory, "enabled": True})
        self.project.setdefault("importedCells", {})[alias] = cells
        self.refresh_file_watcher()
        self.refresh_all()
        self.add_message("Info", f"Referenced {alias} with {len(cells)} cells.")

    def load_project_cells_from_dir(self, path: Path) -> list[dict[str, Any]]:
        cells: list[dict[str, Any]] = []
        for file_path in sorted(path.glob("*.json")):
            if file_path.name == "project.json" or file_path.name.endswith(".bak") or ".autosave." in file_path.name:
                continue
            try:
                raw = json.loads(file_path.read_text(encoding="utf-8"))
                if raw.get("type") in {"schematic", "matrix"}:
                    cell = self.import_pipeline_cell(raw)
                    cell["dirty"] = False
                    cell["fileName"] = file_path.name
                    cells.append(cell)
            except Exception as exc:
                self.add_message("Warning", f"Skipped imported cell {file_path.name}.", str(exc))
        return cells

    def data_folder_root(self) -> Path | None:
        if not self.project_dir:
            return None
        # Common layout: data/<current_project>, data/<referenced_project>, ...
        # The resolver intentionally uses the current project's parent folder so it
        # also works when the folder is named something other than "data".
        return self.project_dir.parent

    def referenced_aliases_in_project(self) -> set[str]:
        aliases: set[str] = set()
        if not self.project:
            return aliases
        for cell in self.project.get("cells", {}).values():
            for inst in cell.get("instances", []):
                source_project = str(inst.get("source_project") or "").strip()
                if source_project:
                    aliases.add(source_project.split("/", 1)[0])
                    continue
                type_name = str(inst.get("type_name") or "").strip()
                if "/" in type_name:
                    aliases.add(type_name.split("/", 1)[0])
        return {alias for alias in aliases if alias}

    def resolve_project_reference_dir(self, alias: str) -> Path | None:
        if not alias:
            return None
        existing = next((imp for imp in self.project.get("imports", []) if str(imp.get("alias", "")) == alias), None) if self.project else None
        if existing:
            existing_path = Path(str(existing.get("path", "")))
            if existing_path.exists():
                return existing_path
        root = self.data_folder_root()
        if not root:
            return None
        candidates = [root / alias]
        cleaned = clean_name(alias)
        if cleaned and cleaned != alias:
            candidates.append(root / cleaned)
        for candidate in candidates:
            if candidate.exists() and candidate.is_dir():
                return candidate
        return None

    def import_project_from_data_folder(self, alias: str, silent: bool = False) -> bool:
        if not self.project:
            return False
        path = self.resolve_project_reference_dir(alias)
        if not path:
            return False
        cells = []
        for cell in self.load_project_cells_from_dir(path):
            copy = json.loads(json.dumps(cell))
            copy["id"] = str(uuid.uuid4())
            copy["readOnly"] = True
            copy["dirty"] = False
            copy["importAlias"] = alias
            copy["originalName"] = cell.get("name")
            cells.append(copy)
        if not cells:
            return False
        imports = self.project.setdefault("imports", [])
        existing = next((imp for imp in imports if str(imp.get("alias", "")) == alias), None)
        if existing:
            existing["path"] = str(path)
            existing["enabled"] = True
            existing.setdefault("autoResolved", True)
        else:
            imports.append({"alias": alias, "path": str(path), "enabled": True, "autoResolved": True})
        self.project.setdefault("importedCells", {})[alias] = cells
        if not silent:
            self.add_message("Info", f"Auto-resolved {alias} from {path} with {len(cells)} cells.")
        return True

    def auto_resolve_data_folder_imports(self, silent: bool = False) -> None:
        if not self.project:
            return
        resolved: list[str] = []
        for alias in sorted(self.referenced_aliases_in_project()):
            current_cells = self.project.setdefault("importedCells", {}).get(alias, [])
            if current_cells:
                continue
            if self.import_project_from_data_folder(alias, silent=True):
                resolved.append(alias)
        if resolved and not silent:
            self.add_message("Info", f"Auto-resolved referenced projects from data folder: {', '.join(resolved)}")

    def reload_imported_projects(self, silent: bool = False) -> None:
        if not self.project:
            return
        self.auto_resolve_data_folder_imports(silent=True)
        changed_aliases: list[str] = []
        for imp in self.project.get("imports", []):
            if not imp.get("enabled", True):
                continue
            path = Path(str(imp.get("path", "")))
            alias = str(imp.get("alias", ""))
            if not path.exists() or not alias:
                if not silent:
                    self.add_message("Warning", f"Imported project unavailable: {alias or path}")
                continue
            old_cells = self.project.setdefault("importedCells", {}).get(alias, [])
            old_ports = {cell.get("name"): self.cell_port_names(cell) for cell in old_cells}
            new_cells = []
            for cell in self.load_project_cells_from_dir(path):
                cell["id"] = str(uuid.uuid4())
                cell["readOnly"] = True
                cell["dirty"] = False
                cell["importAlias"] = alias
                cell["originalName"] = cell.get("name")
                new_cells.append(cell)
            new_ports = {cell.get("name"): self.cell_port_names(cell) for cell in new_cells}
            if old_ports != new_ports:
                changed_aliases.append(alias)
            self.project["importedCells"][alias] = new_cells
            # Update any open read-only tabs that are copies of cells from this alias.
            new_by_name = {c.get("name"): c for c in new_cells}
            for tab_id in list(self.project.get("cells", {}).keys()):
                if not tab_id.startswith(f"import:{alias}:"):
                    continue
                tab_cell = self.project["cells"][tab_id]
                orig_name = tab_cell.get("originalName") or tab_cell.get("name", "").split("/", 1)[-1]
                new_source = new_by_name.get(orig_name)
                if new_source:
                    updated = json.loads(json.dumps(new_source))
                    updated["id"] = tab_id
                    updated["name"] = tab_cell.get("name", orig_name)
                    updated["readOnly"] = True
                    updated["dirty"] = False
                    self.project["cells"][tab_id] = updated
        self.refresh_all()
        if changed_aliases:
            self.add_message("Warning", f"Imported project cells changed: {', '.join(changed_aliases)}")
            if not silent:
                self.popup("Referenced cells changed", "Imported project ports or cells changed. Use Update Blocks to review affected instances.", "warning")
        elif not silent:
            self.add_message("Info", "Imported projects reloaded.")

    def remove_imported_project(self, alias: str) -> None:
        if not self.project:
            return
        used_instances: list[tuple[str, str]] = []
        imported_cell_ids = {cell.get("id") for cell in self.project.get("importedCells", {}).get(alias, [])}
        for cell in self.project.get("cells", {}).values():
            for inst in cell.get("instances", []):
                type_name = inst.get("type_name", "")
                ref = self.find_library_item(type_name)
                if ref and ref.get("id") in imported_cell_ids:
                    used_instances.append((cell.get("name", "Unknown"), type_name))
        if used_instances:
            msg = f"This import is used by {len(used_instances)} instance(s):\n\n"
            for cell_name, inst_type in used_instances[:10]:
                msg += f"  • {cell_name} → {inst_type}\n"
            if len(used_instances) > 10:
                msg += f"  ... and {len(used_instances) - 10} more\n"
            msg += "\nAre you sure you want to remove this import?"
            choice = self.popup("Remove Used Import", msg, "warning", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if choice != QMessageBox.StandardButton.Yes:
                return
        imports = self.project.get("imports", [])
        self.project["imports"] = [imp for imp in imports if imp.get("alias") != alias]
        self.project.get("importedCells", {}).pop(alias, None)
        self.mark_dirty()
        self.refresh_all()
        self.add_message("Info", f"Removed import: {alias}")

    def create_schematic_cell(self) -> None:
        self.create_cell("schematic")

    def create_matrix_cell(self) -> None:
        self.create_cell("matrix")

    def julia_import_llm_conversion_prompt(self, source_text: str) -> str:
        source_text = source_text.strip()
        if not source_text:
            source_text = "<paste the source circuit code here>"

        return f"""Convert the circuit code below into explicit Julia code for JosephsonCircuits.jl.

JosephsonCircuits.jl is a Julia package for simulating microwave/Josephson circuits. The target code should be plain, static Julia source where the circuit topology, component values, port mapping, simulation settings, and solver call are visible directly in the code. Do not rely on hidden runtime state, external project files, or unstated helper functions.

What to produce:
1. A complete Julia snippet that starts with `using JosephsonCircuits`.
2. Prefer explicit calls and assignments over metaprogramming or dynamic construction.
3. Preserve all physical values, units, port names/numbers, pump/DC settings, frequency sweep settings, and solver intent.
4. If the circuit is a harmonic-balance circuit, express it as an `hbsolve`-compatible JosephsonCircuits setup. Make the circuit elements, named nodes/ports, pump ports, pump frequencies/currents, DC ports/currents, modulation harmonics, pump harmonics, and mixing settings explicit when known.
5. If the circuit is a linear S-parameter circuit, express it as a `solveS`-compatible network. Make each network element, its S/ABCD matrix expression, the connection list, exported ports, and frequency range explicit.
6. Put derived constants in normal assignments near the top. Keep reusable numeric parameters as named variables when that helps expose them to the GUI.
7. Return the converted Julia code first, in one fenced ```julia block.
8. After the code, add a short "Conversion notes" section listing anything that could not be converted faithfully.

Important limitations to watch for and explain in the notes:
- Code that builds topology dynamically from loops, comprehensions, generated symbols, eval, macros, include files, external data files, random values, user input, or package-specific helper functions may not be recoverable as a static schematic.
- Custom component functions, closures, or non-JosephsonCircuits APIs should be expanded into explicit JosephsonCircuits primitives when possible; otherwise say exactly what information is missing.
- Ambiguous units, implicit globals, undocumented port ordering, hidden frequency sweeps, or solver settings should be called out.
- If nonlinear HB behavior, pump/DC biasing, mode selection, or port mapping cannot be inferred, do not invent it silently. Keep the closest faithful code and explain the uncertainty.
- Do not summarize instead of converting; produce runnable Julia source as directly as possible.

Original code:
```text
{source_text}
```"""

    def create_julia_hb_block(self) -> None:
        if not self.project:
            self.new_project()
            if not self.project:
                return
        if not self.settings.value("julia_import_disclaimer_accepted", False, type=bool):
            disc = QDialog(self)
            disc.setWindowTitle("Experimental Feature")
            disc.setMinimumWidth(480)
            dlayout = QVBoxLayout(disc)
            msg = QLabel(
                "<b>Julia import is an experimental feature.</b><br><br>"
                "The importer statically analyses Julia source code and attempts to "
                "reconstruct a schematic hierarchy. Complex or dynamic patterns may "
                "not be fully resolved, and results should be reviewed before "
                "running a simulation.<br><br>"
                "Some functions may execute trusted Julia code during import."
            )
            msg.setWordWrap(True)
            dlayout.addWidget(msg)
            no_show = QCheckBox("Don't show this again")
            dlayout.addWidget(no_show)
            btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
            btns.accepted.connect(disc.accept)
            btns.rejected.connect(disc.reject)
            dlayout.addWidget(btns)
            if disc.exec() != QDialog.DialogCode.Accepted:
                return
            if no_show.isChecked():
                self.settings.setValue("julia_import_disclaimer_accepted", True)
        dialog = QDialog(self)
        dialog.setWindowTitle("Import Julia Circuit Block")
        layout = QFormLayout(dialog)
        name_field = QLineEdit("generated_hb")
        source = QPlainTextEdit()
        source.setMinimumHeight(360)
        source.setPlaceholderText(
            "Paste trusted JosephsonCircuits Julia code.\n"
            "Supports both hbsolve (HB) and solveS (S-parameter) circuits.\n"
            "The circuit type is detected automatically from the source."
        )
        type_label = QLabel("Type: (paste code to detect)")
        trust = QCheckBox("I trust this Julia code and allow it to execute during import/export")
        trust.setChecked(True)
        type_label.setText("Type: detected during threaded import")
        prompt_row = QWidget()
        prompt_layout = QHBoxLayout(prompt_row)
        prompt_layout.setContentsMargins(0, 0, 0, 0)
        copy_prompt = QPushButton("Copy LLM Conversion Prompt")
        prompt_layout.addWidget(copy_prompt)
        prompt_layout.addStretch()

        def copy_conversion_prompt() -> None:
            QApplication.clipboard().setText(self.julia_import_llm_conversion_prompt(source.toPlainText()))
            self.popup("Prompt copied", "The conversion prompt was copied to the clipboard.")

        copy_prompt.clicked.connect(copy_conversion_prompt)

        layout.addRow("Block name", name_field)
        layout.addRow("Julia source", source)
        layout.addRow("", prompt_row)
        layout.addRow("Detected", type_label)
        layout.addRow("", trust)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        layout.addRow(buttons)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        name = clean_name(name_field.text())
        if not name:
            self.popup("Invalid block", "Block name is required.", "warning")
            return
        if any(c.get("name") == name for c in self.project["cells"].values()):
            self.popup("Duplicate cell", f'Cell "{name}" already exists.', "warning")
            return
        src_text = source.toPlainText()
        if not trust.isChecked():
            self.popup("Trusted execution required", "Julia circuit import may execute trusted Julia for HB fallback. Check the trust box to continue.", "warning")
            return
        progress = QProgressDialog(
            "Parsing Julia simulation hierarchy...",
            None, 0, 0, self,
        )
        progress.setWindowTitle("Importing Julia Circuit")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)
        progress.show()
        loop = QEventLoop()
        import_result: list[dict] = [{}]
        import_error: list[str] = [""]
        worker = _JuliaImportThread(src_text, name)

        def _on_import_done(imported: dict) -> None:
            import_result[0] = imported
            loop.quit()

        def _on_import_failed(msg: str) -> None:
            import_error[0] = msg
            loop.quit()

        worker.import_done.connect(_on_import_done)
        worker.import_failed.connect(_on_import_failed)
        worker.start()
        loop.exec()
        progress.close()
        if import_error[0]:
            self.popup("Julia import failed", import_error[0], "warning")
            return
        imported = import_result[0]
        raw_cells = imported.get("cells", [])
        if not raw_cells:
            self.popup("Julia import failed", "No schematic cells were extracted from the pasted code.", "warning")
            return
        existing_names = {c.get("name") for c in self.project["cells"].values()}
        incoming_names = [clean_name(str(c.get("name", ""))) for c in raw_cells]
        duplicates = sorted({cell_name for cell_name in incoming_names if cell_name in existing_names})
        if duplicates:
            self.popup("Duplicate cell", "These imported cells already exist: " + ", ".join(duplicates), "warning")
            return
        cells = []
        for raw in raw_cells:
            raw["name"] = clean_name(str(raw.get("name", ""))) or name
            cells.append(self.import_pipeline_cell(raw))
            QApplication.processEvents()
        for cell in cells:
            self.project["cells"][cell["id"]] = cell
        if not self.project.get("default_cell"):
            self.project["default_cell"] = cells[-1].get("name", name)
        self.active_cell_id = cells[-1]["id"]
        self.ensure_cell_tab(cells[-1]["id"])
        for cell in cells:
            if not cell.get("readOnly"):
                cell["dirty"] = True
        if self.is_julia_code_generated_cell(self.project["cells"].get(self.active_cell_id)):
            self.request_generated_view_prompt()
        self._show_import_overview(imported, cells)
        self.refresh_all()

    def _show_import_overview(self, imported: dict, cells: list) -> None:
        summary = imported.get("summary", {})
        skipped = imported.get("skipped", [])
        cell_kinds = summary.get("cell_kinds", {})

        lines: list[str] = []
        lines.append(f"Successfully imported {len(cells)} cell(s):\n")
        for cell in cells:
            cname = cell.get("name", "?")
            kind = cell_kinds.get(cname, "")
            lines.append(f"  ✓  {cname}   [{kind}]" if kind else f"  ✓  {cname}")

        if skipped:
            lines.append(f"\nSkipped {len(skipped)} function(s):\n")
            for entry in skipped:
                lines.append(f"  ✗  {entry['name']}  —  {entry['reason']}")

        lines.append(
            "\nNote: “direct S-matrix” cells (ABCD loops) are simulated by running "
            "their Julia function body directly."
        )

        dlg = QDialog(self)
        dlg.setWindowTitle("Julia Import Results")
        dlg.setMinimumWidth(560)
        layout = QVBoxLayout(dlg)
        text = QPlainTextEdit("\n".join(lines))
        text.setReadOnly(True)
        text.setMinimumHeight(260)
        font = text.font()
        font.setFamily("Monospace")
        text.setFont(font)
        layout.addWidget(text)
        btn = QPushButton("OK")
        btn.clicked.connect(dlg.accept)
        layout.addWidget(btn)
        dlg.exec()

    def create_cell(self, cell_type: str) -> None:
        if not self.project:
            self.new_project()
            if not self.project:
                return
        dialog = QDialog(self)
        dialog.setWindowTitle("Create Schematic Cell" if cell_type == "schematic" else "Create Matrix Cell")
        layout = QFormLayout(dialog)
        name_field = QLineEdit("top" if cell_type == "schematic" else "matrix_cell")
        desc = QPlainTextEdit("")
        desc.setMaximumHeight(72)
        z0 = QDoubleSpinBox()
        z0.setRange(0.000001, 1e12)
        z0.setDecimals(6)
        z0.setValue(50.0)
        open_now = QCheckBox()
        open_now.setChecked(True)
        layout.addRow("Cell name", name_field)
        layout.addRow("Description", desc)
        layout.addRow("Default z0", z0)
        if cell_type == "schematic":
            initial_ports = QSpinBox()
            initial_ports.setRange(0, 256)
            initial_ports.setValue(2)
            layout.addRow("Initial number of ports", initial_ports)
        else:
            port_names = QLineEdit("p1, p2")
            matrix_type = QComboBox()
            matrix_type.addItems(["ABCD", "S", "Y", "Z"])
            variables = QPlainTextEdit("")
            variables.setPlaceholderText("C_cross=1e-15\nR=50")
            variables.setMaximumHeight(80)
            defs_edit = QPlainTextEdit("")
            defs_edit.setPlaceholderText(
                "I2 = ComplexF64[1 0; 0 1]\n"
                "Z2 = ComplexF64[0 0; 0 0]\n"
                "Y_c = im * ω * C_cross"
            )
            defs_edit.setMinimumHeight(90)
            matrix_values = QPlainTextEdit("[[1, 0], [0, 1]]")
            matrix_values.setMinimumHeight(92)
            layout.addRow("Port names", port_names)
            layout.addRow("Matrix type", matrix_type)
            layout.addRow("Variables (name=default, exposed to parent)", variables)
            layout.addRow("Definitions (hardcoded constants and derived expressions)", defs_edit)
            layout.addRow("Matrix expression", matrix_values)
        layout.addRow("Open immediately", open_now)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        layout.addRow(buttons)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        name = clean_name(name_field.text())
        if not name:
            self.popup("Invalid cell", "Cell name is required.", "warning")
            return
        if any(c.get("name") == name for c in self.project["cells"].values()):
            self.popup("Duplicate cell", f'Cell "{name}" already exists.', "warning")
            return
        cell = blank_cell(name, cell_type)
        cell["description"] = desc.toPlainText()
        cell.setdefault("simulation", {})["z0"] = z0.value()
        if cell_type == "matrix":
            ports = [clean_name(x) for x in port_names.text().split(",") if clean_name(x)]
            cell["matrix"] = {
                "port_names": ports,
                "matrix_type": matrix_type.currentText(),
                "values": matrix_values.toPlainText(),
                "definitions": defs_edit.toPlainText(),
            }
            cell["variables"] = parse_variables_text(variables.toPlainText())
            cell["symbol"] = default_symbol(ports)
            cell["symbol_port_layout"] = cell["symbol"]["port_layout"]
        else:
            cell.setdefault("gui", {})["initial_port_count"] = initial_ports.value()
        self.project["cells"][cell["id"]] = cell
        if not self.project.get("default_cell"):
            self.project["default_cell"] = name
        if open_now.isChecked():
            self.active_cell_id = cell["id"]
            self.ensure_cell_tab(cell["id"])
        self.refresh_all()

    def duplicate_cell(self) -> None:
        cell = self.active_cell()
        if not cell or not self.project:
            return
        copy = json.loads(json.dumps(cell))
        copy["id"] = str(uuid.uuid4())
        base = f"{cell.get('name', 'cell')}_copy"
        name = base
        i = 2
        while any(c.get("name") == name for c in self.project["cells"].values()):
            name = f"{base}_{i}"
            i += 1
        copy["name"] = name
        copy["dirty"] = True
        copy.pop("fileName", None)
        self.project["cells"][copy["id"]] = copy
        self.active_cell_id = copy["id"]
        self.ensure_cell_tab(copy["id"])
        self.refresh_all()

    def rename_cell(self) -> None:
        cell = self.active_cell()
        if not cell:
            return
        name, ok = QInputDialog.getText(self, "Rename Cell", "New name:", text=cell.get("name", ""))
        name = clean_name(name)
        if ok and name:
            cell["name"] = name
            self.mark_dirty(cell)
            self.refresh_all()

    def delete_cell(self) -> None:
        cell = self.active_cell()
        if not cell or not self.project:
            return
        if self.popup("Delete Cell", f'Delete "{cell.get("name")}" from the project model?', "question", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        del self.project["cells"][cell["id"]]
        self.open_cell_ids = [cell_id for cell_id in self.open_cell_ids if cell_id != cell["id"]]
        self.active_cell_id = next(iter(self.project["cells"]), None)
        if self.active_cell_id:
            self.ensure_cell_tab(self.active_cell_id)
        self.refresh_all()

    def set_mode(self, mode: str) -> None:
        if mode in {"pin", "label"}:
            self.start_tag_placement(mode)
            return
        self.mode = mode
        self.pending_tag = None
        self.pending_instance = None
        self.canvas.clear_placement_preview()
        self.canvas.update_preview_position_from_cursor()
        self.canvas.schedule_wire_preview()
        self.statusBar().showMessage(f"{mode.title()} mode")

    def start_tag_placement(self, kind: str) -> None:
        cell = self.active_cell()
        if not cell:
            self.add_message("Warning", "No cell is active. Select a cell first.")
            return
        default = f"p{len(cell.get('pins', [])) + 1}" if kind == "pin" else f"net_{len(cell.get('labels', [])) + 1}"
        name, ok = QInputDialog.getText(self, "Add Pin" if kind == "pin" else "Add Label", "Name:", text=default)
        name = clean_name(name)
        if not ok or not name:
            self.exit_mode()
            return
        self.pending_tag = {"kind": kind, "name": name}
        self.mode = kind
        self.canvas.update_preview_position_from_cursor()
        self.canvas.update_placement_preview()
        self.canvas.schedule_wire_preview()
        self.statusBar().showMessage(f"{'Pin' if kind == 'pin' else 'Label'} {name}: click a block port, or Esc to cancel")

    def show_palette(self) -> None:
        if not self.project:
            return
        items = self.library_items()
        dialog = InstancePalette(items, self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.selected_name:
            self.pending_instance = dialog.selected_name
            self.pending_tag = None
            self.mode = "place"
            self.canvas.update_preview_position_from_cursor()
            self.canvas.update_placement_preview()
            self.statusBar().showMessage(f"Click the canvas to place {dialog.selected_name}")

    def library_items(self) -> list[dict[str, Any]]:
        if not self.project:
            return self.builtins
        local = [
            {
                "name": c["name"],
                "source": "local",
                "port_names": self.cell_port_names(c),
                "variables": self.public_variable_items(c),
                "symbol": c.get("symbol") or default_symbol(self.cell_port_names(c)),
                "hb": json.loads(json.dumps(c.get("simulation", {}).get("hb") or {})),
            }
            for c in self.project.get("cells", {}).values()
            if c.get("id") != self.active_cell_id and not c.get("readOnly")
        ]
        imported = []
        for alias, cells in self.project.get("importedCells", {}).items():
            for cell in cells:
                ports = self.cell_port_names(cell)
                imported.append(
                    {
                        "name": f"{alias}/{cell['name']}",
                        "type_name": cell["name"],
                        "source": f"imported:{alias}",
                        "source_project": alias,
                        "port_names": ports,
                        "variables": self.public_variable_items(cell),
                        "symbol": cell.get("symbol") or default_symbol(ports),
                        "hb": json.loads(json.dumps(cell.get("simulation", {}).get("hb") or {})),
                    }
                )
        return self.builtins + local + imported

    def cell_port_names(self, cell: dict[str, Any]) -> list[str]:
        if cell.get("type") == "matrix":
            return [str(port) for port in cell.get("matrix", {}).get("port_names", [])]
        pins = [p for p in cell.get("pins", []) if p.get("name")]
        p_matches = [re.fullmatch(r"P([1-9][0-9]*)", str(p.get("name", ""))) for p in pins]
        if pins and all(p_matches):
            return sorted([str(p.get("name")) for p in pins], key=lambda n: int(re.fullmatch(r"P([1-9][0-9]*)", n).group(1)))
        return [str(p.get("name")) for p in sorted(pins, key=lambda p: int(p.get("order", 0) or 0))]

    def find_library_item(self, name: str) -> dict[str, Any] | None:
        for item in self.library_items():
            if item.get("name") == name:
                return item
        if self.project and "/" in str(name):
            alias = str(name).split("/", 1)[0]
            if self.import_project_from_data_folder(alias, silent=True):
                for item in self.library_items():
                    if item.get("name") == name:
                        return item
        return None

    def copy_hb_settings_to_instance(self, inst: dict[str, Any], ref: dict[str, Any]) -> None:
        inst["hb"] = json.loads(json.dumps(ref.get("hb") or {}))

    def instance_hb_settings_summary(self, hb: dict[str, Any]) -> str:
        pump_ports = ", ".join(str(x) for x in hb.get("pump_ports", []) or []) or "none"
        pump_freqs = ", ".join(str(x) for x in hb.get("pump_frequencies", []) or []) or "none"
        mod = ", ".join(str(x) for x in hb.get("modulation_harmonics", []) or []) or "default"
        pump_harm = ", ".join(str(x) for x in hb.get("pump_harmonics", []) or []) or "default"
        return f"Pump ports: {pump_ports}   Pump freqs: {pump_freqs} GHz   Mod harmonics: {mod}   Pump harmonics: {pump_harm}"

    def unique_uid(self, prefix: str) -> str:
        cell = self.active_cell()
        if not cell:
            return prefix + "1"
        i = 1
        while any(inst.get("uid") == f"{prefix}{i}" for inst in cell.get("instances", [])):
            i += 1
        return f"{prefix}{i}"

    def get_instance_bounds(self, inst: dict[str, Any]) -> tuple[float, float, float, float]:
        """Returns (left, top, right, bottom) of the rotated instance AABB."""
        symbol = inst.get("symbol") or default_symbol(inst.get("port_names", []))
        w = float(symbol.get("width", 120)) / 2
        h = float(symbol.get("height", 70)) / 2
        cx, cy = float(inst.get("position", [0, 0])[0]), float(inst.get("position", [0, 0])[1])
        rotation = float(inst.get("rotation_degrees", 0))
        if rotation != 0:
            angle_rad = math.radians(rotation)
            cos_a, sin_a = math.cos(angle_rad), math.sin(angle_rad)
            corners = [(-w, -h), (w, -h), (w, h), (-w, h)]
            xs = [cx + lx * cos_a - ly * sin_a for lx, ly in corners]
            ys = [cy + lx * sin_a + ly * cos_a for lx, ly in corners]
            return min(xs), min(ys), max(xs), max(ys)
        return cx - w, cy - h, cx + w, cy + h

    def check_collision(self, cell: dict[str, Any], test_inst: dict[str, Any], exclude_uids: set[str] | None = None) -> bool:
        """Check if test_inst overlaps (not just touches) any other instance in cell."""
        if exclude_uids is None:
            exclude_uids = set()
        test_bounds = self.get_instance_bounds(test_inst)
        for inst in cell.get("instances", []):
            if inst.get("uid") in exclude_uids:
                continue
            other_bounds = self.get_instance_bounds(inst)
            if not (test_bounds[2] <= other_bounds[0] or test_bounds[0] >= other_bounds[2] or
                    test_bounds[3] <= other_bounds[1] or test_bounds[1] >= other_bounds[3]):
                return True
        return False

    def place_instance(self, name: str, point: list[float]) -> None:
        cell = self.active_cell()
        ref = self.find_library_item(name)
        if not cell or not ref:
            return
        self.record_undo()
        ports = list(ref.get("port_names", []))
        ref_vars = list(ref.get("variables", []))
        vars_ = [v for v in ref_vars if v.get("name") not in RESERVED_VARIABLE_NAMES]
        type_name = ref.get("type_name") or ref.get("name") or name
        prefix = re.sub(r"[^A-Za-z]", "", type_name)[:2].upper() or "U"
        uid = self.unique_uid(prefix)
        inst = {
            "type_name": type_name,
            "uid": uid,
            "source": ref.get("source", "local"),
            "source_project": ref.get("source_project", ""),
            "parameters": {v.get("name"): v.get("default", "") for v in vars_},
            "parameter_order": [v.get("name") for v in vars_],
            "parameter_kinds": {v.get("name"): "positional" for v in vars_},
            "has_frequency_dependency": any(v.get("name") in RESERVED_VARIABLE_NAMES for v in ref_vars),
            "position": point,
            "port_count": len(ports),
            "port_names": ports,
            "rotation_degrees": 0,
            "repeat_count": 1,
            "repeat_connections": [],
            "symbol": json.loads(json.dumps(ref.get("symbol") or default_symbol(ports))),
        }
        self.copy_hb_settings_to_instance(inst, ref)
        inst["symbol_port_layout"] = inst["symbol"].get("port_layout", [])
        if self.check_collision(cell, inst):
            self.add_message("Warning", f"Cannot place {name}: it would overlap with existing block.")
            return
        cell.setdefault("instances", []).append(inst)
        self.selected = Selection("instance", uid)
        self.mark_dirty(cell)
        self.refresh_inspector()
        self._refresh_view()
        self.add_message("Info", f"Placed {name} as {uid}.")

    def _find_net_for_endpoint(self, cell: dict[str, Any], endpoint: dict[str, str]) -> dict[str, Any] | None:
        for net in cell.get("nets", []):
            if any(ep.get("instance_uid") == endpoint.get("instance_uid") and str(ep.get("port")) == str(endpoint.get("port")) for ep in net.get("endpoints", [])):
                return net
        return None

    def _merge_nets(self, cell: dict[str, Any], keep: dict[str, Any], remove: dict[str, Any]) -> None:
        keep_id = keep.get("id")
        remove_id = remove.get("id")
        keep.setdefault("endpoints", []).extend(remove.get("endpoints", []))
        keep.setdefault("route_segments", []).extend(remove.get("route_segments", []))
        keep["pins"] = list(dict.fromkeys(keep.get("pins", []) + remove.get("pins", [])))
        keep["labels"] = list(dict.fromkeys(keep.get("labels", []) + remove.get("labels", [])))
        for pin in cell.get("pins", []):
            if pin.get("net_id") == remove_id:
                pin["net_id"] = keep_id
        for label in cell.get("labels", []):
            if label.get("net_id") == remove_id:
                label["net_id"] = keep_id
        cell["nets"] = [n for n in cell["nets"] if n.get("id") != remove_id]

    def finish_wire(self, source: dict[str, str], target: dict[str, str]) -> None:
        if source == target:
            return
        cell = self.active_cell()
        if not cell:
            return
        self.record_undo()
        source_net = self._find_net_for_endpoint(cell, source)
        target_net = self._find_net_for_endpoint(cell, target)
        source_inst = self.find_instance(source.get("instance_uid"))
        target_inst = self.find_instance(target.get("instance_uid"))
        points = self.auto_route_points(source, target) if source_inst and target_inst else []
        new_segment = {"wire_id": f"wire_{uuid.uuid4().hex[:8]}", "source": source, "target": target, "points": points, "auto": True}
        if source_net is None and target_net is None:
            net = {"id": f"net_{uuid.uuid4().hex[:8]}", "endpoints": [source, target], "pins": [], "labels": [], "route_segments": [new_segment]}
            cell.setdefault("nets", []).append(net)
        elif source_net is not None and target_net is None:
            source_net.setdefault("endpoints", []).append(target)
            source_net.setdefault("route_segments", []).append(new_segment)
        elif source_net is None and target_net is not None:
            target_net.setdefault("endpoints", []).append(source)
            target_net.setdefault("route_segments", []).append(new_segment)
        elif source_net is not target_net:
            source_net.setdefault("route_segments", []).append(new_segment)
            self._merge_nets(cell, source_net, target_net)
        # else: both on same net already; do nothing
        self.mark_dirty(cell)
        self.canvas.pending_wire = None
        self.refresh_inspector()
        self._refresh_view()

    def finish_wire_to_net(self, source: dict[str, str], target_net_id: str, scene_pos: QPointF | None = None) -> None:
        cell = self.active_cell()
        if not cell:
            return
        target_net = next((n for n in cell.get("nets", []) if n.get("id") == target_net_id), None)
        if not target_net:
            return
        self.record_undo()
        source_net = self._find_net_for_endpoint(cell, source)
        closest_ep = target_net["endpoints"][0] if target_net.get("endpoints") else None
        source_inst = self.find_instance(source.get("instance_uid"))
        points = []
        if source_inst and scene_pos is not None:
            start = port_point(source_inst, str(source.get("port")))
            route_obstacles = self.route_obstacles(exclude_uids=set())
            escape_obstacles = self.route_obstacles(exclude_uids={str(source.get("instance_uid", ""))})
            occupied = self.existing_wire_segments()
            start_escape = clear_port_escape_point(source_inst, str(source.get("port")), escape_obstacles)
            target_point = QPointF(scene_pos.x(), scene_pos.y())
            middle = routed_orthogonal_points(start_escape, target_point, route_obstacles, occupied_segments=occupied)
            points = compact_points([[start.x(), start.y()], [start_escape.x(), start_escape.y()]] + middle)
        elif source_inst and closest_ep:
            points = self.auto_route_points(source, closest_ep)
        new_segment = {"wire_id": f"wire_{uuid.uuid4().hex[:8]}", "source": source, "target": closest_ep or source, "points": points, "auto": True}
        if source_net is None:
            target_net.setdefault("endpoints", []).append(source)
            target_net.setdefault("route_segments", []).append(new_segment)
        elif source_net is not target_net:
            source_net.setdefault("route_segments", []).append(new_segment)
            self._merge_nets(cell, target_net, source_net)
        self.mark_dirty(cell)
        self.canvas.pending_wire = None
        self.refresh_inspector()
        self._refresh_view()

    def find_or_create_endpoint_net(self, endpoint: dict[str, str]) -> dict[str, Any]:
        cell = self.active_cell()
        assert cell is not None
        for net in cell.setdefault("nets", []):
            if any(ep.get("instance_uid") == endpoint["instance_uid"] and str(ep.get("port")) == str(endpoint["port"]) for ep in net.get("endpoints", [])):
                return net
        net = {"id": f"net_{uuid.uuid4().hex[:8]}", "endpoints": [endpoint], "pins": [], "labels": [], "route_segments": []}
        cell["nets"].append(net)
        return net

    def add_pin_or_label(self, kind: str, endpoint: dict[str, str], chosen_name: str | None = None) -> None:
        cell = self.active_cell()
        inst = self.find_instance(endpoint.get("instance_uid"))
        if not cell or not inst:
            return
        self.record_undo()
        default = f"p{len(cell.get('pins', [])) + 1}" if kind == "pin" else f"net_{len(cell.get('labels', [])) + 1}"
        if chosen_name is None:
            name, ok = QInputDialog.getText(self, "Add Pin" if kind == "pin" else "Add Label", "Name:", text=default)
            name = clean_name(name)
            if not ok or not name:
                return
        else:
            name = clean_name(chosen_name)
            if not name:
                return
        net = self.find_or_create_endpoint_net(endpoint)
        point = port_point(inst, endpoint["port"])
        item = {
            "id": str(uuid.uuid4()),
            "name": name,
            "net_id": net["id"],
            "position": [point.x() + 14, point.y() - 20],
            "display_visible": True,
        }
        if kind == "pin":
            item["order"] = len(cell.get("pins", [])) + 1
            item["instance_uid"] = endpoint.get("instance_uid", "")
            item["port"] = str(endpoint.get("port", ""))
            cell.setdefault("pins", []).append(item)
            net.setdefault("pins", []).append(item["id"])
        else:
            cell.setdefault("labels", []).append(item)
            net.setdefault("labels", []).append(item["id"])
        self.mark_dirty(cell)
        self.refresh_inspector()
        self._refresh_view()

    def add_pin_or_label_to_net(self, kind: str, net_id: str, point: QPointF, chosen_name: str | None = None) -> None:
        cell = self.active_cell()
        if not cell:
            return
        net = next((item for item in cell.get("nets", []) if item.get("id") == net_id), None)
        if not net:
            return
        self.record_undo()
        if chosen_name is not None:
            name = clean_name(chosen_name)
            if not name:
                return
        else:
            default = f"p{len(cell.get('pins', [])) + 1}" if kind == "pin" else f"net_{len(cell.get('labels', [])) + 1}"
            name, ok = QInputDialog.getText(self, "Add Pin" if kind == "pin" else "Add Label", "Name:", text=default)
            name = clean_name(name)
            if not ok or not name:
                return
        item = {
            "id": str(uuid.uuid4()),
            "name": name,
            "net_id": net_id,
            "position": [point.x() + 8, point.y() - 18],
            "display_visible": True,
        }
        if kind == "pin":
            endpoint = self.closest_endpoint_on_net(net, point)
            item["order"] = len(cell.get("pins", [])) + 1
            item["instance_uid"] = endpoint.get("instance_uid", "")
            item["port"] = str(endpoint.get("port", ""))
            cell.setdefault("pins", []).append(item)
            net.setdefault("pins", []).append(item["id"])
        else:
            cell.setdefault("labels", []).append(item)
            net.setdefault("labels", []).append(item["id"])
        self.mark_dirty(cell)
        self.refresh_inspector()
        self._refresh_view()

    def add_wire_bend(self, net_id: str, wire_id: str, point: QPointF) -> None:
        cell = self.active_cell()
        if not cell:
            return
        net = next((item for item in cell.get("nets", []) if item.get("id") == net_id), None)
        if not net:
            return
        route = next((item for item in net.get("route_segments", []) if item.get("wire_id") == wire_id), None)
        if not route:
            return
        self.record_undo()
        points = route.setdefault("points", [])
        if len(points) >= 2:
            insert_at = max(1, len(points) // 2)
            snapped = self.snap_point(point)
            points.insert(insert_at, [snapped.x(), snapped.y()])
            route["auto"] = False
            self.mark_dirty(cell)
            self.refresh_all()

    def route_obstacles(self, exclude_uids: set[str] | None = None) -> list[QRectF]:
        cell = self.active_cell()
        if not cell:
            return []
        excluded = exclude_uids or set()
        signature: list[tuple[Any, ...]] = []
        for inst in cell.get("instances", []):
            uid = str(inst.get("uid", ""))
            if uid in excluded:
                continue
            symbol = inst.get("symbol") or default_symbol(inst.get("port_names", []))
            pos = inst.get("position", [0, 0])
            signature.append(
                (
                    uid,
                    round(float(pos[0]), 3),
                    round(float(pos[1]), 3),
                    round(float(inst.get("rotation_degrees", 0)), 3),
                    round(float(symbol.get("width", 120)), 3),
                    round(float(symbol.get("height", 70)), 3),
                )
            )
        key = (self.active_cell_id, tuple(sorted(excluded)), tuple(signature))
        cached = self._route_obstacles_cache.get(key)
        if cached is not None:
            return cached
        obstacles = [
            block_rect(inst)
            for inst in cell.get("instances", [])
            if str(inst.get("uid", "")) not in excluded
        ]
        if len(self._route_obstacles_cache) > 32:
            self._route_obstacles_cache.clear()
        self._route_obstacles_cache[key] = obstacles
        return obstacles

    def existing_wire_segments(
        self,
        exclude_wire_id: str | None = None,
        exclude_net_id: str | None = None,
    ) -> list[tuple[list[float], list[float]]]:
        cell = self.active_cell()
        if not cell:
            return []
        segments: list[tuple[list[float], list[float]]] = []
        for net in cell.get("nets", []):
            if exclude_net_id and str(net.get("id", "")) == exclude_net_id:
                continue
            for route in net.get("route_segments", []):
                if exclude_wire_id and str(route.get("wire_id", "")) == exclude_wire_id:
                    continue
                points = compact_points(route.get("points", []))
                for a, b in zip(points, points[1:]):
                    if math.isclose(float(a[0]), float(b[0])) or math.isclose(float(a[1]), float(b[1])):
                        segments.append((a, b))
        return segments

    def auto_route_points(
        self,
        source: dict[str, Any],
        target: dict[str, Any],
        exclude_wire_id: str | None = None,
        exclude_net_id: str | None = None,
    ) -> list[list[float]]:
        source_inst = self.find_instance(source.get("instance_uid"))
        target_inst = self.find_instance(target.get("instance_uid"))
        if not source_inst or not target_inst:
            return []
        start = port_point(source_inst, str(source.get("port")))
        end = port_point(target_inst, str(target.get("port")))
        exclude = {str(source.get("instance_uid", "")), str(target.get("instance_uid", ""))}
        escape_obstacles = self.route_obstacles(exclude_uids=exclude)
        route_obstacles = self.route_obstacles(exclude_uids=set())
        occupied = self.existing_wire_segments(exclude_wire_id=exclude_wire_id, exclude_net_id=exclude_net_id)
        # Keep routing interactive by considering only nearby wire lanes.
        min_x, max_x = sorted([start.x(), end.x()])
        min_y, max_y = sorted([start.y(), end.y()])
        margin = 260.0
        occupied = [
            (a, b)
            for a, b in occupied
            if not (
                max(float(a[0]), float(b[0])) < min_x - margin
                or min(float(a[0]), float(b[0])) > max_x + margin
                or max(float(a[1]), float(b[1])) < min_y - margin
                or min(float(a[1]), float(b[1])) > max_y + margin
            )
        ][:80]

        def escape_candidates(inst: dict[str, Any], port: str, point: QPointF) -> list[QPointF]:
            side = port_side(inst, port)
            candidates: list[QPointF] = []
            for distance in (28.0, 46.0, 64.0):
                if side == "left":
                    candidates.append(QPointF(point.x() - distance, point.y()))
                elif side == "top":
                    candidates.append(QPointF(point.x(), point.y() - distance))
                elif side == "bottom":
                    candidates.append(QPointF(point.x(), point.y() + distance))
                else:
                    candidates.append(QPointF(point.x() + distance, point.y()))
            fallback = clear_port_escape_point(inst, port, escape_obstacles)
            candidates.append(fallback)
            unique: list[QPointF] = []
            seen: set[tuple[float, float]] = set()
            for candidate in candidates:
                key = (round(candidate.x(), 3), round(candidate.y(), 3))
                if key not in seen:
                    seen.add(key)
                    unique.append(candidate)
            return unique

        best_route: list[list[float]] = []
        best_score: tuple[int, float, float, int] | None = None
        for start_escape in escape_candidates(source_inst, str(source.get("port")), start):
            for end_escape in escape_candidates(target_inst, str(target.get("port")), end):
                middle = routed_orthogonal_points(start_escape, end_escape, route_obstacles, occupied_segments=occupied)
                route = compact_points(
                    [[start.x(), start.y()], [start_escape.x(), start_escape.y()]]
                    + middle
                    + [[end_escape.x(), end_escape.y()], [end.x(), end.y()]]
                )
                score = (
                    route_intersection_count(route, escape_obstacles)
                    + route_intersection_count(middle, route_obstacles),
                    route_overlap_length(route, occupied),
                    route_length(route),
                    len(route),
                )
                if best_score is None or score < best_score:
                    best_score = score
                    best_route = route
        return best_route

    def route_crosses_block(self, route: dict[str, Any]) -> str | None:
        points = route.get("points", [])
        if len(points) < 2:
            return None
        source = route.get("source", {})
        target = route.get("target", {})
        endpoint_uids = {str(source.get("instance_uid", "")), str(target.get("instance_uid", ""))}
        cell = self.active_cell()
        if not cell:
            return None
        segments = list(zip(points, points[1:]))
        for inst in cell.get("instances", []):
            uid = str(inst.get("uid", ""))
            rect = block_rect(inst, margin=4.0)
            for idx, (a, b) in enumerate(segments):
                if uid in endpoint_uids and idx in {0, len(segments) - 1}:
                    continue
                if segment_intersects_rect(a, b, rect):
                    return uid
        return None

    def route_overlaps_parallel_wire(self, route: dict[str, Any], exclude_wire_id: str | None = None) -> bool:
        points = compact_points(route.get("points", []))
        if len(points) < 2:
            return False
        occupied = self.existing_wire_segments(exclude_wire_id=exclude_wire_id)
        return route_overlap_length(points, occupied) > 0.5

    def update_routes_for_instance(self, uid: str) -> None:
        cell = self.active_cell()
        if not cell or not uid:
            return
        for net in cell.get("nets", []):
            for route in net.get("route_segments", []):
                points = route.setdefault("points", [])
                source = route.get("source", {})
                target = route.get("target", {})
                source_matches = source.get("instance_uid") == uid
                target_matches = target.get("instance_uid") == uid
                if not source_matches and not target_matches:
                    continue
                source_inst = self.find_instance(source.get("instance_uid"))
                target_inst = self.find_instance(target.get("instance_uid"))
                if source_inst and target_inst:
                    route["points"] = self.auto_route_points(
                        source,
                        target,
                        exclude_wire_id=str(route.get("wire_id", "")),
                    )
                    route["auto"] = True
                    continue
                if source_matches:
                    inst = self.find_instance(uid)
                    if inst:
                        point = port_point(inst, str(source.get("port")))
                        if points:
                            points[0] = [point.x(), point.y()]
                        else:
                            points.append([point.x(), point.y()])
                        align_route_endpoint(points, True, port_side(inst, str(source.get("port"))))
                if target_matches:
                    inst = self.find_instance(uid)
                    if inst:
                        point = port_point(inst, str(target.get("port")))
                        if points:
                            points[-1] = [point.x(), point.y()]
                        else:
                            points.append([point.x(), point.y()])
                        align_route_endpoint(points, False, port_side(inst, str(target.get("port"))))
                route["points"] = orthogonalize_points(points)
        self.update_attached_tags_for_instance(uid)

    def update_route_endpoints_for_instance(self, uid: str) -> None:
        cell = self.active_cell()
        inst = self.find_instance(uid)
        if not cell or not uid or not inst:
            return
        for net in cell.get("nets", []):
            for route in net.get("route_segments", []):
                points = route.setdefault("points", [])
                source = route.get("source", {})
                target = route.get("target", {})
                changed = False
                if source.get("instance_uid") == uid:
                    point = port_point(inst, str(source.get("port")))
                    if points:
                        points[0] = [point.x(), point.y()]
                    else:
                        points.append([point.x(), point.y()])
                    align_route_endpoint(points, True, port_side(inst, str(source.get("port"))))
                    changed = True
                if target.get("instance_uid") == uid:
                    point = port_point(inst, str(target.get("port")))
                    if points:
                        points[-1] = [point.x(), point.y()]
                    else:
                        points.append([point.x(), point.y()])
                    align_route_endpoint(points, False, port_side(inst, str(target.get("port"))))
                    changed = True
                if changed:
                    route["points"] = orthogonalize_points(points)
        self.update_attached_tags_for_instance(uid)

    def update_attached_tags_for_instance(self, uid: str) -> None:
        cell = self.active_cell()
        inst = self.find_instance(uid)
        if not cell or not inst:
            return
        for pin in cell.get("pins", []):
            if pin.get("instance_uid") != uid:
                continue
            point = port_point(inst, str(pin.get("port", "")))
            pin["position"] = [point.x() + 14, point.y() - 20]
        for label in cell.get("labels", []):
            if label.get("instance_uid") != uid:
                continue
            point = port_point(inst, str(label.get("port", "")))
            label["position"] = [point.x() + 14, point.y() - 20]

    def closest_endpoint_on_net(self, net: dict[str, Any], point: QPointF) -> dict[str, str]:
        cell = self.active_cell()
        preferred = self.closest_endpoint_on_net_by_type(cell, net, point, {"P"})
        if preferred:
            return preferred
        best_endpoint: dict[str, str] = {}
        best_distance = float("inf")
        for endpoint in net.get("endpoints", []):
            inst = self.find_instance(endpoint.get("instance_uid"))
            if not inst:
                continue
            port = str(endpoint.get("port", ""))
            port_pos = port_point(inst, port)
            distance = (port_pos.x() - point.x()) ** 2 + (port_pos.y() - point.y()) ** 2
            if distance < best_distance:
                best_endpoint = {"instance_uid": endpoint.get("instance_uid", ""), "port": port}
                best_distance = distance
        if best_endpoint:
            return best_endpoint
        endpoints = net.get("endpoints", [])
        if endpoints:
            return {"instance_uid": endpoints[0].get("instance_uid", ""), "port": str(endpoints[0].get("port", ""))}
        return {"instance_uid": "", "port": ""}

    def closest_endpoint_on_net_by_type(self, cell: dict[str, Any] | None, net: dict[str, Any], point: QPointF, type_names: set[str]) -> dict[str, str]:
        best_endpoint: dict[str, str] = {}
        best_distance = float("inf")
        for endpoint in net.get("endpoints", []):
            inst = next((item for item in (cell or {}).get("instances", []) if item.get("uid") == endpoint.get("instance_uid")), None)
            if not inst or str(inst.get("type_name", "")) not in type_names:
                continue
            port = str(endpoint.get("port", ""))
            port_pos = port_point(inst, port)
            distance = (port_pos.x() - point.x()) ** 2 + (port_pos.y() - point.y()) ** 2
            if distance < best_distance:
                best_endpoint = {"instance_uid": endpoint.get("instance_uid", ""), "port": port}
                best_distance = distance
        return best_endpoint

    def delete_net(self, net_id: str) -> None:
        cell = self.active_cell()
        if not cell:
            return
        self.record_undo()
        cell["nets"] = [net for net in cell.get("nets", []) if net.get("id") != net_id]
        cell["pins"] = [pin for pin in cell.get("pins", []) if pin.get("net_id") != net_id]
        cell["labels"] = [label for label in cell.get("labels", []) if label.get("net_id") != net_id]
        self.selected = None
        self.mark_dirty(cell)
        self.refresh_all()

    def delete_wire(self, net_id: str, wire_id: str) -> None:
        cell = self.active_cell()
        if not cell:
            return
        net = next((n for n in cell.get("nets", []) if n.get("id") == net_id), None)
        if not net:
            return
        self.record_undo()
        net["route_segments"] = [route for route in net.get("route_segments", []) if route.get("wire_id") != wire_id]
        self.selected = None
        self.mark_dirty(cell)
        self.refresh_all()

    def tag_collection(self, cell: dict[str, Any], kind: str) -> list[dict[str, Any]]:
        return cell.setdefault("pins" if kind == "pin" else "labels", [])

    def find_tag(self, kind: str, tag_id: str) -> dict[str, Any] | None:
        cell = self.active_cell()
        if not cell:
            return None
        return next((item for item in self.tag_collection(cell, kind) if item.get("id") == tag_id), None)

    def rename_tag(self, kind: str, tag_id: str) -> None:
        tag = self.find_tag(kind, tag_id)
        if not tag:
            return
        name, ok = QInputDialog.getText(self, "Rename Pin" if kind == "pin" else "Rename Label", "Name:", text=str(tag.get("name", "")))
        name = clean_name(name)
        if not ok or not name:
            return
        self.record_undo()
        tag["name"] = name
        self.mark_dirty()
        self.refresh_all()

    def set_tag_name(self, kind: str, tag_id: str, value: str) -> None:
        tag = self.find_tag(kind, tag_id)
        name = clean_name(value)
        if not tag or not name or tag.get("name") == name:
            return
        tag["name"] = name
        self.mark_dirty()
        self.refresh_all()

    def set_pin_order(self, tag_id: str, value: int) -> None:
        cell = self.active_cell()
        if not cell:
            return
        pins = sorted(cell.get("pins", []), key=lambda p: int(p.get("order", 0) or 0))
        pin = next((item for item in pins if item.get("id") == tag_id), None)
        if not pin:
            return
        pins = [item for item in pins if item.get("id") != tag_id]
        pins.insert(max(0, min(value - 1, len(pins))), pin)
        for i, item in enumerate(pins, start=1):
            item["order"] = i
        cell["pins"] = pins
        self.mark_dirty(cell)
        self.refresh_all()

    def select_net(self, net_id: str) -> None:
        if not net_id:
            return
        self.selected = Selection("net", net_id)
        self.refresh_all()

    def delete_tag(self, kind: str, tag_id: str) -> None:
        cell = self.active_cell()
        if not cell:
            return
        self.record_undo()
        collection_name = "pins" if kind == "pin" else "labels"
        removed = next((item for item in cell.get(collection_name, []) if item.get("id") == tag_id), None)
        cell[collection_name] = [item for item in cell.get(collection_name, []) if item.get("id") != tag_id]
        if removed:
            for net in cell.get("nets", []):
                key = "pins" if kind == "pin" else "labels"
                net[key] = [item for item in net.get(key, []) if item != tag_id]
        if kind == "pin":
            for i, pin in enumerate(sorted(cell.get("pins", []), key=lambda p: int(p.get("order", 0) or 0)), start=1):
                pin["order"] = i
        self.selected = None
        self.mark_dirty(cell)
        self.refresh_all()

    def set_tag_position(self, kind: str, tag_id: str, point: QPointF) -> None:
        tag = self.find_tag(kind, tag_id)
        if not tag:
            return
        snapped = self.snap_point(point)
        tag["position"] = [round(snapped.x(), 3), round(snapped.y(), 3)]

    def constrain_label_to_net(self, label: dict[str, Any]) -> None:
        cell = self.active_cell()
        if not cell or label.get("kind") != "label":
            return
        net_id = label.get("net_id")
        net = next((n for n in cell.get("nets", []) if n.get("id") == net_id), None)
        if not net:
            return
        routes = net.get("route_segments", [])
        if not routes:
            return
        label_pos = (label.get("position", [0, 0])[0], label.get("position", [0, 0])[1])
        min_dist = float('inf')
        closest_pt = label_pos
        for route in routes:
            points = route.get("points", [])
            if len(points) >= 2:
                dist = distance_point_to_segment(label_pos, (points[0][0], points[0][1]), (points[1][0], points[1][1]))
                if dist < min_dist:
                    min_dist = dist
                    closest_pt = closest_point_on_polyline(label_pos, points)
        if min_dist > MAX_LABEL_DISTANCE_PIXELS:
            label["position"] = [round(closest_pt[0], 3), round(closest_pt[1], 3)]

    def _refresh_view(self) -> None:
        cell = self.active_cell()
        if cell and self.is_julia_source_schematic_cell(cell):
            cell_id = str(cell.get("id", ""))
            if self._force_generated_view_prompt or cell_id not in self._julia_source_view_modes:
                self._julia_source_view_modes[cell_id] = self.choose_julia_generated_view(cell)
            self._force_generated_view_prompt = False
            if self._julia_source_view_modes.get(cell_id) == "code":
                self.center_stack.setCurrentIndex(1)
                self.generated_view.load_cell(cell, ask_view=cell.get("id") != self.generated_view._loaded_cell_id)
            else:
                self.invalidate_generated_view()
                self.center_stack.setCurrentIndex(0)
                self.canvas.draw()
        elif cell and cell.get("type") in ("generated_hb", "generated_s"):
            self.center_stack.setCurrentIndex(1)
            ask_view = self._force_generated_view_prompt or cell.get("id") != self.generated_view._loaded_cell_id
            self._force_generated_view_prompt = False
            self.generated_view.load_cell(cell, ask_view=ask_view)
        else:
            self.invalidate_generated_view()
            self.center_stack.setCurrentIndex(0)
            self.canvas.draw()

    def invalidate_generated_view(self) -> None:
        self.generated_view._loaded_cell_id = None
        self._force_generated_view_prompt = False

    def request_generated_view_prompt(self) -> None:
        self.generated_view._loaded_cell_id = None
        self._force_generated_view_prompt = True

    def is_julia_code_generated_cell(self, cell: dict[str, Any] | None) -> bool:
        if not cell:
            return False
        return cell.get("type") in ("generated_hb", "generated_s") or self.is_julia_source_schematic_cell(cell)

    def is_julia_source_schematic_cell(self, cell: dict[str, Any] | None) -> bool:
        if not cell or cell.get("type") != "schematic":
            return False
        return bool(str(cell.get("generated_source", "")).strip())

    def choose_julia_generated_view(self, cell: dict[str, Any]) -> str:
        choice = QMessageBox.question(
            self,
            "Open Julia Cell",
            f"{cell.get('name', 'This cell')} was generated from Julia code.\n"
            f"Which view would you like to open?\n\n"
            f"Yes = Schematic   No = Code & Settings",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        return "schematic" if choice == QMessageBox.StandardButton.Yes else "code"

    def refresh_all(self) -> None:
        self.refresh_explorer()
        self.refresh_tabs()
        self.refresh_inspector()
        self._refresh_view()
        self.refresh_results()

    def refresh_explorer(self) -> None:
        self.explorer.clear()
        if not self.project:
            return
        root = QTreeWidgetItem([f"Project: {self.project.get('name')}"])
        self.explorer.addTopLevelItem(root)
        cells = QTreeWidgetItem(["Cells"])
        root.addChild(cells)
        for cell in self.project.get("cells", {}).values():
            label = cell.get("name", "")
            if cell.get("dirty"):
                label += " *"
            item = QTreeWidgetItem([label])
            item.setData(0, Qt.ItemDataRole.UserRole, ("cell", cell["id"]))
            cells.addChild(item)
        imports = QTreeWidgetItem(["Imported Projects"])
        root.addChild(imports)
        for imp in self.project.get("imports", []):
            group = QTreeWidgetItem([f"{imp.get('alias')} ({imp.get('path')})"])
            group.setData(0, Qt.ItemDataRole.UserRole, ("import_group", imp.get("alias")))
            imports.addChild(group)
            for cell in self.project.get("importedCells", {}).get(imp.get("alias"), []):
                item = QTreeWidgetItem([cell.get("name", "")])
                item.setData(0, Qt.ItemDataRole.UserRole, ("import", imp.get("alias"), cell.get("id")))
                group.addChild(item)
        builtins = QTreeWidgetItem(["Built-ins"])
        root.addChild(builtins)
        by_group: dict[str, QTreeWidgetItem] = {}
        for bi in self.builtins:
            group_name = bi.get("group", "Built-ins")
            group = by_group.get(group_name)
            if group is None:
                group = QTreeWidgetItem([group_name])
                by_group[group_name] = group
                builtins.addChild(group)
            item = QTreeWidgetItem([bi["name"]])
            item.setData(0, Qt.ItemDataRole.UserRole, ("builtin", bi["name"]))
            group.addChild(item)
        recent = QTreeWidgetItem(["Recently Opened Cells"])
        root.addChild(recent)
        recent_names = list(self.project.get("recent_cells", []))
        for name in recent_names:
            cell = next((c for c in self.project.get("cells", {}).values() if c.get("name") == name), None)
            if cell:
                item = QTreeWidgetItem([name])
                item.setData(0, Qt.ItemDataRole.UserRole, ("cell", cell["id"]))
                recent.addChild(item)
        results = QTreeWidgetItem(["Results"])
        root.addChild(results)
        for result in self.project.get("results", []):
            item = QTreeWidgetItem([f"{result.get('cell', '')}/{result.get('name', '')}"])
            item.setData(0, Qt.ItemDataRole.UserRole, ("result", result.get("name", "")))
            results.addChild(item)
        self.explorer.expandAll()

    def show_explorer_menu(self, pos) -> None:
        item = self.explorer.itemAt(pos)
        data = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        menu = QMenu(self.explorer)
        if not data:
            create_schematic = menu.addAction("Create schematic cell")
            create_matrix = menu.addAction("Create matrix cell")
            create_julia = menu.addAction("Create Julia HB block")
            import_project = menu.addAction("Import project")
            chosen = menu.exec(self.explorer.viewport().mapToGlobal(pos))
            if chosen == create_schematic:
                self.create_schematic_cell()
            elif chosen == create_matrix:
                self.create_matrix_cell()
            elif chosen == create_julia:
                self.create_julia_hb_block()
            elif chosen == import_project:
                self.reference_project()
            return
        if data[0] == "cell":
            self.active_cell_id = data[1]
            self.ensure_cell_tab(data[1])
            open_cell = menu.addAction("Open cell")
            rename = menu.addAction("Rename cell")
            duplicate = menu.addAction("Duplicate cell")
            delete = menu.addAction("Delete cell")
            props = menu.addAction("Show cell properties")
            reveal = menu.addAction("Reveal in file browser")
            chosen = menu.exec(self.explorer.viewport().mapToGlobal(pos))
            if chosen == open_cell:
                cell = self.project.get("cells", {}).get(data[1]) if self.project else None
                if self.is_julia_code_generated_cell(cell):
                    self.request_generated_view_prompt()
                self.refresh_all()
            elif chosen == rename:
                self.rename_cell()
            elif chosen == duplicate:
                self.duplicate_cell()
            elif chosen == delete:
                self.delete_cell()
            elif chosen == props:
                self.refresh_inspector()
            elif chosen == reveal:
                self.reveal_active_cell_file()
        elif data[0] == "import":
            alias, cid = data[1], data[2]
            open_cell = menu.addAction("Open read-only")
            copy_local = menu.addAction("Copy into current project")
            chosen = menu.exec(self.explorer.viewport().mapToGlobal(pos))
            if chosen == open_cell:
                self.on_explorer_double_click(item)
            elif chosen == copy_local:
                self.copy_imported_cell(alias, cid)
        elif data[0] == "import_group":
            alias = data[1]
            reload = menu.addAction("Reload")
            remove = menu.addAction("Remove import")
            chosen = menu.exec(self.explorer.viewport().mapToGlobal(pos))
            if chosen == reload:
                self.reload_imported_projects(silent=False)
            elif chosen == remove:
                self.remove_imported_project(alias)
        elif data[0] == "builtin":
            place = menu.addAction("Place instance")
            view = menu.addAction("Open built-in viewer")
            chosen = menu.exec(self.explorer.viewport().mapToGlobal(pos))
            if chosen == place:
                self.pending_instance = data[1]
                self.pending_tag = None
                self.mode = "place"
                self.canvas.update_preview_position_from_cursor()
                self.canvas.update_placement_preview()
            elif chosen == view:
                builtin = next((b for b in self.builtins if b.get("name") == data[1]), None)
                if builtin:
                    self.show_builtin_viewer(builtin)

    def copy_imported_cell(self, alias: str, cell_id: str) -> None:
        if not self.project:
            return
        source = next((c for c in self.project.get("importedCells", {}).get(alias, []) if c.get("id") == cell_id), None)
        if not source:
            return
        copy = json.loads(json.dumps(source))
        copy["id"] = str(uuid.uuid4())
        base = clean_name(str(source.get("name", "imported_cell")))
        name = base
        i = 2
        while any(c.get("name") == name for c in self.project.get("cells", {}).values()):
            name = f"{base}_{i}"
            i += 1
        copy["name"] = name
        copy["readOnly"] = False
        copy["dirty"] = True
        copy.pop("importAlias", None)
        copy.pop("originalName", None)
        copy.pop("fileName", None)
        self.project["cells"][copy["id"]] = copy
        self.active_cell_id = copy["id"]
        if self.is_julia_code_generated_cell(copy):
            self.request_generated_view_prompt()
        self.ensure_cell_tab(copy["id"])
        self.refresh_all()
        self.add_message("Info", f'Copied imported cell "{source.get("name")}" into this project as "{name}".')

    def reveal_active_cell_file(self) -> None:
        cell = self.active_cell()
        if not cell or not self.project_dir:
            return
        file_name = cell.get("fileName")
        if not file_name:
            self.popup("Reveal Cell", "Save this cell before revealing it on disk.")
            return
        self.add_message("Info", f"Cell file: {self.project_dir / file_name}")

    def refresh_tabs(self) -> None:
        self.tabs.blockSignals(True)
        self.tabs.clear()
        if self.project:
            self.open_cell_ids = [cell_id for cell_id in self.open_cell_ids if cell_id in self.project.get("cells", {})]
            for cell_id in self.open_cell_ids:
                cell = self.project["cells"][cell_id]
                label = cell.get("name", "")
                if cell.get("dirty"):
                    label += " *"
                if cell.get("readOnly"):
                    label += " [RO]"
                if any(severity == "Error" for severity, _message in self.validate_cell(cell)):
                    label += " !"
                self.tabs.addTab(QWidget(), label)
                self.tabs.tabBar().setTabData(self.tabs.count() - 1, cell["id"])
                if cell["id"] == self.active_cell_id:
                    self.tabs.setCurrentIndex(self.tabs.count() - 1)
        self.tabs.blockSignals(False)

    def ensure_cell_tab(self, cell_id: str) -> None:
        if cell_id not in self.open_cell_ids:
            self.open_cell_ids.append(cell_id)

    def close_tab(self, index: int) -> None:
        cell_id = self.tabs.tabBar().tabData(index)
        if not self.project or not cell_id:
            return
        cell = self.project.get("cells", {}).get(cell_id)
        if cell and cell.get("dirty") and not cell.get("readOnly"):
            choice = self.popup("Close Tab", f'Close "{cell.get("name")}" with unsaved changes?', "question", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if choice != QMessageBox.StandardButton.Yes:
                return
        self.open_cell_ids = [item for item in self.open_cell_ids if item != cell_id]
        if str(cell_id).startswith("import:") and cell_id in self.project.get("cells", {}):
            self.project["cells"].pop(cell_id, None)
        self.active_cell_id = self.open_cell_ids[-1] if self.open_cell_ids else None
        self.selected = None
        self.refresh_all()

    def show_tab_menu(self, pos) -> None:
        index = self.tabs.tabBar().tabAt(pos)
        if index < 0:
            return
        menu = QMenu(self.tabs)
        close = menu.addAction("Close tab")
        close_others = menu.addAction("Close other tabs")
        chosen = menu.exec(self.tabs.tabBar().mapToGlobal(pos))
        if chosen == close:
            self.close_tab(index)
        elif chosen == close_others:
            keep = self.tabs.tabBar().tabData(index)
            for i in reversed(range(self.tabs.count())):
                if self.tabs.tabBar().tabData(i) != keep:
                    self.close_tab(i)

    def next_tab(self) -> None:
        if self.tabs.count() > 1:
            self.tabs.setCurrentIndex((self.tabs.currentIndex() + 1) % self.tabs.count())

    def refresh_inspector(self) -> None:
        self.clear_layout(self.inspector_layout)
        cell = self.active_cell()
        if not cell:
            self.inspector_layout.addWidget(QLabel("Open or create a cell."))
            self.inspector_layout.addStretch()
            return
        inst = self.find_instance(self.selected.id) if self.selected and self.selected.kind == "instance" else None
        if inst:
            self.populate_instance_inspector(cell, inst)
        elif self.selected and self.selected.kind == "net":
            self.populate_net_inspector(cell, self.selected.id)
        elif self.selected and self.selected.kind in {"pin", "label"}:
            self.populate_tag_inspector(cell, self.selected.kind, self.selected.id)
        else:
            self.populate_cell_inspector(cell)
        self.inspector_layout.addStretch()

    def clear_layout(self, layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            child_layout = item.layout()
            widget = item.widget()
            if child_layout:
                self.clear_layout(child_layout)
                child_layout.deleteLater()
            elif widget:
                widget.deleteLater()

    def populate_net_inspector(self, cell: dict[str, Any], net_id: str) -> None:
        net = next((item for item in cell.get("nets", []) if item.get("id") == net_id), None)
        if not net:
            self.populate_cell_inspector(cell)
            return
        title = QLabel("Net Properties")
        title.setFont(QFont("Sans Serif", 11, QFont.Weight.Bold))
        self.inspector_layout.addWidget(title)
        self.inspector_layout.addWidget(QLabel(f"Net ID: {net.get('id')}"))
        self.inspector_layout.addWidget(QLabel("Endpoints"))
        endpoints = QTableWidget(len(net.get("endpoints", [])), 2)
        endpoints.setHorizontalHeaderLabels(["Instance", "Port"])
        endpoints.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        for row, ep in enumerate(net.get("endpoints", [])):
            endpoints.setItem(row, 0, QTableWidgetItem(str(ep.get("instance_uid", ""))))
            endpoints.setItem(row, 1, QTableWidgetItem(str(ep.get("port", ""))))
        self.inspector_layout.addWidget(endpoints)
        add_pin = QPushButton("Add Pin")
        add_label = QPushButton("Add Label")
        delete = QPushButton("Delete Net")
        add_pin.clicked.connect(lambda: self.add_pin_or_label_to_net("pin", net_id, QPointF(0, 0)))
        add_label.clicked.connect(lambda: self.add_pin_or_label_to_net("label", net_id, QPointF(0, 0)))
        delete.clicked.connect(lambda: self.delete_net(net_id))
        self.inspector_layout.addWidget(add_pin)
        self.inspector_layout.addWidget(add_label)
        self.inspector_layout.addWidget(delete)

    def populate_tag_inspector(self, cell: dict[str, Any], kind: str, tag_id: str) -> None:
        tag = next((item for item in self.tag_collection(cell, kind) if item.get("id") == tag_id), None)
        if not tag:
            self.populate_cell_inspector(cell)
            return
        title = QLabel("Pin Properties" if kind == "pin" else "Label Properties")
        title.setFont(QFont("Sans Serif", 11, QFont.Weight.Bold))
        self.inspector_layout.addWidget(title)
        form = QFormLayout()
        name = QLineEdit(str(tag.get("name", "")))
        net = QLineEdit(str(tag.get("net_id", "")))
        net.setReadOnly(True)
        form.addRow("Name", name)
        if kind == "pin":
            order = QSpinBox()
            order.setRange(1, max(1, len(cell.get("pins", []))))
            order.setValue(int(tag.get("order", 1) or 1))
            form.addRow("Order", order)
        form.addRow("Net", net)
        self.inspector_layout.addLayout(form)
        name.editingFinished.connect(lambda: self.set_tag_name(kind, tag_id, name.text()))
        if kind == "pin":
            order.valueChanged.connect(lambda value: self.set_pin_order(tag_id, value))
        highlight = QPushButton("Highlight Net")
        delete = QPushButton("Delete")
        highlight.clicked.connect(lambda: self.select_net(str(tag.get("net_id", ""))))
        delete.clicked.connect(lambda: self.delete_tag(kind, tag_id))
        self.inspector_layout.addWidget(highlight)
        self.inspector_layout.addWidget(delete)

    def populate_cell_inspector(self, cell: dict[str, Any]) -> None:
        title_text = "Cell Properties"
        if cell.get("readOnly"):
            title_text += " [read-only]"
        title = QLabel(title_text)
        title.setFont(QFont("Sans Serif", 11, QFont.Weight.Bold))
        self.inspector_layout.addWidget(title)
        form = QFormLayout()
        name = QLineEdit(cell.get("name", ""))
        desc = QPlainTextEdit(cell.get("description", ""))
        desc.setMaximumHeight(80)
        z0 = QLineEdit(str(cell.get("simulation", {}).get("z0", 50)))
        form.addRow("Name", name)
        form.addRow("Description", desc)
        form.addRow("z0", z0)
        self.inspector_layout.addLayout(form)
        name.editingFinished.connect(lambda: self.set_cell_prop(cell, "name", clean_name(name.text())))
        desc.textChanged.connect(lambda: self.set_cell_prop(cell, "description", desc.toPlainText()))
        z0.editingFinished.connect(lambda: (cell.setdefault("simulation", {}).__setitem__("z0", self.float_or(z0.text(), 50.0)), self.mark_dirty(cell), self.refresh_tabs()))
        self.inspector_layout.addWidget(QLabel("Ports"))
        ports = QTableWidget(len(cell.get("pins", [])), 3)
        ports.setHorizontalHeaderLabels(["#", "Name", "Net"])
        ports.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        for i, pin in enumerate(cell.get("pins", [])):
            ports.setItem(i, 0, QTableWidgetItem(str(i + 1)))
            ports.setItem(i, 1, QTableWidgetItem(pin.get("name", "")))
            ports.setItem(i, 2, QTableWidgetItem(pin.get("net_id", "")))
        self.inspector_layout.addWidget(ports)
        sim = cell.get("simulation", {})
        input_ports = ", ".join(map(str, sim.get("input_ports", []))) or "none"
        output_ports = ", ".join(map(str, sim.get("output_ports", []))) or "none"
        mode = str(sim.get("mode", "s"))
        self.inspector_layout.addWidget(QLabel(f"Simulation: mode={mode}   input={input_ports}   output={output_ports}"))
        sim_button = QPushButton("Simulation Setup")
        sim_button.clicked.connect(self.edit_simulation_setup)
        self.inspector_layout.addWidget(sim_button)
        _cell_hb = cell.setdefault("simulation", {}).setdefault("hb", {})
        hb_top_cb = QCheckBox("HB top block")
        hb_top_cb.setChecked(bool(_cell_hb.get("top_block", False)))
        self.inspector_layout.addWidget(hb_top_cb)
        hb_disable_cb = QCheckBox("Skip nested HB block check")
        hb_disable_cb.setChecked(bool(cell.get("skip_hb_top_block_check", False)))
        hb_disable_cb.setVisible(cell.get("type") == "schematic")
        self.inspector_layout.addWidget(hb_disable_cb)
        hb_settings_btn = QPushButton("HB Simulation Settings")
        hb_settings_btn.setVisible(bool(_cell_hb.get("top_block", False)))
        self.inspector_layout.addWidget(hb_settings_btn)

        def _on_hb_top_toggled(checked: bool) -> None:
            cell.setdefault("simulation", {}).setdefault("hb", {})["top_block"] = checked
            hb_settings_btn.setVisible(checked)
            self.mark_dirty(cell)

        hb_top_cb.toggled.connect(_on_hb_top_toggled)
        hb_disable_cb.toggled.connect(lambda checked: (
            cell.update({"skip_hb_top_block_check": checked}),  # type: ignore[func-returns-value]
            self.mark_dirty(cell),
        ))
        hb_settings_btn.clicked.connect(lambda _checked=False, c=cell: self.edit_hb_settings(c.setdefault("simulation", {}).setdefault("hb", {}), c))
        port_order = QPushButton("Edit Port Order")
        port_order.clicked.connect(self.edit_port_order)
        self.inspector_layout.addWidget(port_order)
        if cell.get("type") == "matrix":
            matrix = cell.get("matrix", {})
            self.inspector_layout.addWidget(QLabel(f"Matrix: {matrix.get('matrix_type', 'ABCD')}   Ports: {', '.join(matrix.get('port_names', [])) or 'none'}"))
            edit_matrix = QPushButton("Edit Matrix Definition")
            edit_matrix.clicked.connect(self.edit_matrix_definition)
            self.inspector_layout.addWidget(edit_matrix)
        if cell.get("type") == "generated_hb":
            summary = cell.get("generated_summary", {}) or {}
            counts = summary.get("primitive_counts", {}) or {}
            counts_text = ", ".join(f"{key}:{value}" for key, value in sorted(counts.items())) or "none"
            self.inspector_layout.addWidget(QLabel(f"Generated HB: {summary.get('component_count', 0)} components   {summary.get('node_count', 0)} nodes"))
            self.inspector_layout.addWidget(QLabel(f"Primitive counts: {counts_text}"))
            edit_source = QPushButton("Edit Julia Source")
            edit_source.clicked.connect(self.edit_generated_hb_source)
            self.inspector_layout.addWidget(edit_source)
            refresh_generated = QPushButton("Regenerate Summary")
            refresh_generated.clicked.connect(self.refresh_generated_hb_summary)
            self.inspector_layout.addWidget(refresh_generated)
        if cell.get("type") in ("matrix", "generated_hb"):
            self.inspector_layout.addWidget(QLabel("Variables"))
            variables = QTableWidget(len(cell.get("variables", [])), 4)
            variables.setHorizontalHeaderLabels(["Name", "Value", "Default", "Status"])
            variables.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
            for row, var in enumerate(cell.get("variables", [])):
                name_text = str(var.get("name", ""))
                value_text = str(var.get("value", var.get("default", "")))
                default_text = str(var.get("default", ""))
                status = "Expression" if any(ch in value_text for ch in "+-*/()") and not self.float_like(value_text) else "Valid"
                if not name_text:
                    status = "Missing"
                variables.setItem(row, 0, QTableWidgetItem(name_text))
                variables.setItem(row, 1, QTableWidgetItem(value_text))
                variables.setItem(row, 2, QTableWidgetItem(default_text))
                variables.setItem(row, 3, QTableWidgetItem(status))
            self.inspector_layout.addWidget(variables)
            variable_buttons = QWidget()
            variable_layout = QHBoxLayout(variable_buttons)
            add_var = QPushButton("Add Variable")
            del_var = QPushButton("Delete Variable")
            variable_layout.addWidget(add_var)
            variable_layout.addWidget(del_var)
            self.inspector_layout.addWidget(variable_buttons)
            add_var.clicked.connect(self.add_cell_variable)
            del_var.clicked.connect(lambda: self.delete_cell_variable(variables.currentRow()))

    def _collect_propagated_vars(self, cell: dict[str, Any]) -> list[dict[str, Any]]:
        blocked_names = {
            str(v.get("name", "")).strip()
            for v in cell.get("variables", [])
            if (
                str(v.get("name", "")).strip()
                and v.get("kind") != "uid"
                and self.numeric_expression(self.variable_default_text(v))
            )
        }
        seen: dict[str, list[str]] = {}

        def exposed_names_for_ident(ident: str, aliases: dict[str, set[str]], blocked: set[str]) -> set[str]:
            names = aliases.get(ident, {ident})
            return {
                name
                for name in names
                if name and name not in VARIABLE_GLOBAL_NAMES and name not in blocked
            }

        def visit(
            parent_cell: dict[str, Any],
            path: list[str],
            blocked: set[str],
            aliases: dict[str, set[str]],
            depth: int = 0,
        ) -> None:
            if depth > 32:
                return
            for inst in parent_cell.get("instances", []) or []:
                uid = str(inst.get("uid", "?")).strip() or "?"
                full_path = path + [uid]
                path_text = ".".join(full_path)
                nested_cell = self.cell_definition_for_instance(inst)
                values = dict(inst.get("parameters", {}) or {})
                if nested_cell:
                    for var in nested_cell.get("variables", []) or []:
                        if var.get("kind") == "uid":
                            continue
                        var_name = str(var.get("name", "")).strip()
                        if not var_name or var_name in values:
                            continue
                        var_value = var.get("default", var.get("value", ""))
                        if var_value not in ["", None]:
                            values[var_name] = var_value
                child_blocked = set(blocked)
                child_aliases = {name: set(targets) for name, targets in aliases.items()}
                for key, val in values.items():
                    if self.is_uid_parameter(inst, key):
                        continue
                    key = str(key).strip()
                    val_str = str(val).strip()
                    if not key or not val_str:
                        continue
                    if self.numeric_expression(val_str):
                        child_blocked.add(key)
                        child_aliases.pop(key, None)
                        continue
                    names: set[str] = set()
                    for ident in self.expression_variable_names(val_str):
                        names.update(exposed_names_for_ident(ident, aliases, blocked))
                    if names:
                        child_aliases[key] = names
                for key, val in values.items():
                    if self.is_uid_parameter(inst, key):
                        continue
                    val_str = str(val).strip()
                    if not val_str or self.numeric_expression(val_str):
                        continue
                    for ident in self.expression_variable_names(val_str):
                        for exposed_name in exposed_names_for_ident(ident, aliases, blocked):
                            label = f"{path_text}.{key}"
                            if label not in seen.get(exposed_name, []):
                                seen.setdefault(exposed_name, []).append(label)
                if nested_cell:
                    visit(nested_cell, full_path, child_blocked, child_aliases, depth + 1)

        root_aliases = {
            str(v.get("name", "")).strip(): {str(v.get("name", "")).strip()}
            for v in cell.get("variables", [])
            if (
                str(v.get("name", "")).strip()
                and v.get("kind") != "uid"
                and str(v.get("name", "")).strip() not in blocked_names
            )
        }
        visit(cell, [], blocked_names, root_aliases)
        return [{"name": name, "used_by": refs} for name, refs in sorted(seen.items())]

    def is_uid_parameter(self, inst: dict[str, Any], key: str) -> bool:
        if inst.get("parameter_kinds", {}).get(key) == "uid":
            return True
        return inst.get("type_name") == "K" and key in {"inductor_a", "inductor_b"}

    def cell_definition_for_instance(self, inst: dict[str, Any]) -> dict[str, Any] | None:
        type_name = str(inst.get("type_name", ""))
        source_project = str(inst.get("source_project", ""))
        if not type_name or not self.project:
            return None
        if not source_project and "/" in type_name:
            source_project, type_name = type_name.split("/", 1)
        if source_project:
            for cell in self.project.get("importedCells", {}).get(source_project, []):
                if cell.get("name") == type_name:
                    return cell
        for cell in self.project.get("cells", {}).values():
            if cell.get("name") == type_name:
                return cell
        for cells in self.project.get("importedCells", {}).values():
            for cell in cells:
                if cell.get("name") == type_name:
                    return cell
        return None

    def manual_override_variables(self, cell_def: dict[str, Any] | None) -> list[dict[str, Any]]:
        if not cell_def:
            return []
        variables: list[dict[str, Any]] = []
        seen: set[str] = set()
        function_names = self.cell_function_names(cell_def)
        for var in cell_def.get("variables", []) or []:
            name = str(var.get("name", "")).strip()
            if not name or name in seen or name in VARIABLE_GLOBAL_NAMES or name in function_names or var.get("kind") == "uid":
                continue
            if not self.numeric_expression(self.variable_default_text(var)):
                continue
            variables.append(var)
            seen.add(name)
        return variables

    def variable_default_text(self, var: dict[str, Any]) -> str:
        return str(var.get("default", var.get("value", ""))).strip()

    def expression_variable_names(self, value: Any) -> set[str]:
        text = str(value).strip()
        if not text:
            return set()
        names = set(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", text))
        return names - self.expression_function_names(text) - VARIABLE_GLOBAL_NAMES

    def expression_function_names(self, value: Any) -> set[str]:
        return set(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b(?=\s*\()", str(value)))

    def cell_function_names(self, cell: dict[str, Any]) -> set[str]:
        names: set[str] = set()
        for var in cell.get("variables", []) or []:
            names.update(self.expression_function_names(var.get("default", var.get("value", ""))))
        for var in cell.get("simulation_variables", []) or []:
            names.update(self.expression_function_names(var.get("default", var.get("value", ""))))
        for inst in cell.get("instances", []) or []:
            for value in (inst.get("parameters", {}) or {}).values():
                names.update(self.expression_function_names(value))
        return names

    def numeric_expression(self, value: Any) -> bool:
        text = str(value).strip()
        if not text:
            return False
        return not self.expression_variable_names(text)

    def symbolic_value(self, value: Any) -> bool:
        return bool(self.expression_variable_names(value))

    def public_variable_items(self, cell_def: dict[str, Any] | None) -> list[dict[str, Any]]:
        if not cell_def:
            return []
        return self.inferred_cell_variables(cell_def)

    def internal_parameter_override_items(self, cell_def: dict[str, Any] | None) -> list[dict[str, Any]]:
        if not cell_def:
            return []
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        visible_override_keys: set[str] = {
            str(var.get("name", "")).strip()
            for var in cell_def.get("variables", []) or []
            if (
                str(var.get("name", "")).strip()
                and var.get("kind") != "uid"
                and self.numeric_expression(self.variable_default_text(var))
            )
        }

        def simple_identifier(value: Any) -> str:
            text = str(value).strip()
            return text if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", text) else ""

        def child_default_values(child_cell: dict[str, Any] | None) -> dict[str, Any]:
            defaults: dict[str, Any] = {}
            if not child_cell:
                return defaults
            for var in child_cell.get("variables", []) or []:
                if var.get("kind") == "uid":
                    continue
                name = str(var.get("name", "")).strip()
                if not name:
                    continue
                value = var.get("default", var.get("value", ""))
                if value not in ["", None]:
                    defaults[name] = value
            return defaults

        def visit(parent_cell: dict[str, Any], path: list[str], aliases: dict[str, set[str]], depth: int = 0) -> None:
            if depth > 32:
                return
            for child_inst in parent_cell.get("instances", []) or []:
                uid = str(child_inst.get("uid", "")).strip()
                if not uid:
                    continue
                full_path = path + [uid]
                path_text = ".".join(full_path)
                nested_cell = self.cell_definition_for_instance(child_inst)
                params = child_inst.get("parameters", {}) or {}
                defaults: dict[str, Any] = {}
                for var_name, var_default in child_default_values(nested_cell).items():
                    defaults.setdefault(var_name, var_default)

                ordered_keys = list(child_inst.get("parameter_order", []) or []) + sorted(set(params) | set(defaults))
                child_aliases = dict(aliases)
                local_links: dict[str, set[str]] = {}
                for key in ordered_keys:
                    key = str(key)
                    if not key or key in RESERVED_VARIABLE_NAMES or self.is_uid_parameter(child_inst, key):
                        continue
                    value = params.get(key, defaults.get(key, ""))
                    override_key = f"{path_text}.{key}"
                    linked = set(aliases.get(key, set()))
                    ident = simple_identifier(value)
                    if ident:
                        linked.update(aliases.get(ident, {ident}))
                    local_links[override_key] = linked
                    child_aliases[key] = linked | {override_key}

                for key in ordered_keys:
                    key = str(key)
                    if not key or key in RESERVED_VARIABLE_NAMES or self.is_uid_parameter(child_inst, key):
                        continue
                    value = params.get(key, defaults.get(key, ""))
                    if value in ["", None] or not self.numeric_expression(value):
                        continue
                    override_key = f"{path_text}.{key}"
                    if override_key in seen:
                        continue
                    seen.add(override_key)
                    linked_keys = local_links.get(override_key, set())
                    if any(linked_key in visible_override_keys for linked_key in linked_keys):
                        continue
                    visible_override_keys.add(override_key)
                    items.append(
                        {
                            "key": override_key,
                            "uid": path_text,
                            "parameter": key,
                            "default": str(value),
                            "type": str(child_inst.get("type_name", "")),
                            "linked_keys": sorted(linked_keys),
                        }
                    )

                if nested_cell:
                    visit(nested_cell, full_path, child_aliases, depth + 1)

        root_aliases = {
            str(var.get("name", "")).strip(): {str(var.get("name", "")).strip()}
            for var in cell_def.get("variables", []) or []
            if str(var.get("name", "")).strip() and var.get("kind") != "uid"
        }
        for var in self._collect_propagated_vars(cell_def):
            name = str(var.get("name", "")).strip()
            if name:
                root_aliases.setdefault(name, {name})
        visit(cell_def, [], root_aliases)
        return items

    def inferred_cell_variables(self, cell: dict[str, Any]) -> list[dict[str, Any]]:
        variables = [
            {key: value for key, value in {
                "name": v.get("name"),
                "default": v.get("default") or v.get("value"),
                "value": v.get("value") or v.get("default"),
                "scope": v.get("scope"),
            }.items() if value is not None}
            for v in cell.get("variables", [])
            if v.get("name") and str(v.get("name")) not in VARIABLE_GLOBAL_NAMES and str(v.get("name")) not in self.cell_function_names(cell)
        ]
        known = {str(v.get("name")) for v in variables}
        known.update(VARIABLE_GLOBAL_NAMES)
        known.update(self.cell_function_names(cell))
        for inst in cell.get("instances", []):
            params = inst.get("parameters", {}) or {}
            for key, value in params.items():
                if self.is_uid_parameter(inst, key):
                    continue
                text = str(value).strip()
                if not text or self.numeric_expression(text):
                    continue
                for ident in self.expression_variable_names(text):
                    if ident in known:
                        continue
                    default = str(value) if ident == key else ""
                    variables.append({"name": ident, "default": default})
                    known.add(ident)
        return variables

    def populate_instance_inspector(self, cell: dict[str, Any], inst: dict[str, Any]) -> None:
        title = QLabel("Block Properties")
        title.setFont(QFont("Sans Serif", 11, QFont.Weight.Bold))
        self.inspector_layout.addWidget(title)
        form = QFormLayout()
        uid = QLineEdit(inst.get("uid", ""))
        typ = QLineEdit(inst.get("type_name", ""))
        typ.setReadOnly(True)
        source = QLineEdit(str(inst.get("source_project") or inst.get("source") or "local"))
        source.setReadOnly(True)
        status = QLineEdit("Imported" if inst.get("source_project") else ("Built-in" if inst.get("source") == "built-in" else "Local"))
        status.setReadOnly(True)
        form.addRow("UID", uid)
        form.addRow("Type", typ)
        form.addRow("Source", source)
        form.addRow("Status", status)
        self.inspector_layout.addLayout(form)
        uid.editingFinished.connect(lambda: self.set_instance_uid(inst, uid.text()))
        # Check if the cell definition is marked as hb_top_block
        type_name = inst.get("type_name", "")
        cell_def = self.cell_definition_for_instance(inst)
        if type_name and self.project:
            # Show indicator and editable settings when the referenced block is an HB simulation root.
            _def_hb = (cell_def or {}).get("simulation", {}).get("hb", {})
            if _def_hb.get("top_block") or inst.get("hb", {}).get("top_block"):
                hb_info = QLabel("⚠️ Cell definition is marked as HB top block")
                hb_info.setStyleSheet("color: #8e4b32; font-weight: bold;")
                self.inspector_layout.addWidget(hb_info)
                if cell_def and not inst.get("hb"):
                    self.copy_hb_settings_to_instance(inst, cell_def)
                hb_summary = QLabel(self.instance_hb_settings_summary(inst.get("hb") or {}))
                hb_summary.setWordWrap(True)
                self.inspector_layout.addWidget(hb_summary)
                hb_settings_btn = QPushButton("HB Instance Simulation Settings")
                hb_settings_btn.clicked.connect(lambda _checked=False, i=inst, owner=cell: self.edit_hb_settings(i.setdefault("hb", {}), owner))
                self.inspector_layout.addWidget(hb_settings_btn)
        params = inst.setdefault("parameters", {})
        has_frequency_dependency = bool(inst.get("has_frequency_dependency")) or "w" in params
        public_vars = self.public_variable_items(cell_def)
        if cell_def:
            visible_params = {
                str(var.get("name")): params.get(str(var.get("name")), var.get("default", ""))
                for var in public_vars
                if str(var.get("name", "")).strip()
            }
        else:
            visible_params = {key: value for key, value in params.items() if key not in RESERVED_VARIABLE_NAMES}
        if has_frequency_dependency:
            self.inspector_layout.addWidget(QLabel("Frequency dependent: uses reserved simulation variable w."))
        if visible_params:
            self.inspector_layout.addWidget(QLabel("Parameters"))
            for key in list(visible_params):
                default = next(
                    (var.get("default", "") for var in public_vars if str(var.get("name", "")) == key),
                    "",
                )
                kind = inst.get("parameter_kinds", {}).get(key, "")
                field = QLineEdit(str(visible_params[key]))
                self.inspector_layout.addWidget(QLabel(f"{key}   default={default}   kind={kind}"))
                self.inspector_layout.addWidget(field)
                field.editingFinished.connect(lambda k=key, f=field: self.set_param(inst, k, f.text()))
        manual_vars = self.manual_override_variables(cell_def)
        internal_overrides = self.internal_parameter_override_items(cell_def)
        if manual_vars or internal_overrides:
            override_count = len(inst.get("internal_parameter_overrides", {}) or {}) + sum(
                1
                for var in manual_vars
                if str(var.get("name", "")) in params
            )
            label = "Manual Variable Overrides..."
            if override_count:
                label = f"Manual Variable Overrides ({override_count})..."
            overrides_btn = QPushButton(label)
            overrides_btn.clicked.connect(lambda _checked=False, i=inst, c=cell_def: self.edit_manual_overrides(i, c))
            self.inspector_layout.addWidget(overrides_btn)
        repeat = QPushButton("Repeat Settings")
        repeat.clicked.connect(self.edit_repeat_settings)
        self.inspector_layout.addWidget(repeat)
        symbol = QPushButton("Edit Symbol")
        symbol.clicked.connect(self.edit_symbol)
        self.inspector_layout.addWidget(symbol)
        open_block = QPushButton("Open Block")
        open_block.clicked.connect(self.open_selected_block)
        self.inspector_layout.addWidget(open_block)
        replace = QPushButton("Replace Block")
        replace.clicked.connect(self.replace_selected_block)
        self.inspector_layout.addWidget(replace)
        reset_params = QPushButton("Reset Parameters")
        reset_params.clicked.connect(self.reset_selected_parameters)
        self.inspector_layout.addWidget(reset_params)
        reset_symbol = QPushButton("Reset Symbol")
        reset_symbol.clicked.connect(lambda: self.reset_selected_symbol_dialogless())
        self.inspector_layout.addWidget(reset_symbol)
        delete = QPushButton("Delete Block")
        delete.clicked.connect(self.delete_selected)
        self.inspector_layout.addWidget(delete)

    def set_cell_prop(self, cell: dict[str, Any], key: str, value: Any) -> None:
        cell[key] = value
        self.mark_dirty(cell)
        self.refresh_tabs()

    def set_instance_uid(self, inst: dict[str, Any], value: str) -> None:
        inst["uid"] = clean_name(value)
        self.selected = Selection("instance", inst["uid"])
        self.mark_dirty()
        self.refresh_all()

    def set_param(self, inst: dict[str, Any], key: str, value: str) -> None:
        inst.setdefault("parameters", {})[key] = value
        self.mark_dirty()

    def set_optional_param(self, inst: dict[str, Any], key: str, value: str) -> None:
        params = inst.setdefault("parameters", {})
        value = value.strip()
        if value:
            params[key] = value
        else:
            params.pop(key, None)
        self.mark_dirty()

    def set_internal_override(self, inst: dict[str, Any], key: str, value: str) -> None:
        overrides = inst.setdefault("internal_parameter_overrides", {})
        value = value.strip()
        if value:
            overrides[key] = value
        else:
            overrides.pop(key, None)
        if not overrides:
            inst.pop("internal_parameter_overrides", None)
        self.mark_dirty()

    def edit_manual_overrides(self, inst: dict[str, Any], cell_def: dict[str, Any] | None) -> None:
        manual_vars = self.manual_override_variables(cell_def)
        internal_items = self.internal_parameter_override_items(cell_def)
        if not manual_vars and not internal_items:
            self.popup("Manual Overrides", "This block has no variables to override.")
            return

        dialog = QDialog(self)
        dialog.setWindowTitle(f"Manual Overrides: {inst.get('uid', '')}")
        dialog.resize(620, 560)
        root = QVBoxLayout(dialog)
        hint = QLabel("Overrides are saved only on this block instance. The referenced block JSON is not changed.")
        hint.setWordWrap(True)
        root.addWidget(hint)
        search = QLineEdit()
        search.setPlaceholderText("Search variables, instance paths, or defaults")
        root.addWidget(search)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(8, 8, 8, 8)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)

        param_fields: dict[str, QLineEdit] = {}
        internal_fields: dict[str, QLineEdit] = {}
        params = inst.get("parameters", {}) or {}
        internal_values = inst.get("internal_parameter_overrides", {}) or {}
        searchable_rows: list[tuple[QWidget, str]] = []

        def add_section(title_text: str) -> None:
            title = QLabel(title_text)
            title.setFont(QFont("Sans Serif", 10, QFont.Weight.Bold))
            title.setStyleSheet("margin-top: 8px;")
            body_layout.addWidget(title)

        def add_override_row(
            label_text: str,
            default: str,
            current: str,
            tooltip: str,
            target: dict[str, QLineEdit],
            key: str,
            search_text: str = "",
        ) -> None:
            row = QWidget()
            row_l = QHBoxLayout(row)
            row_l.setContentsMargins(0, 0, 0, 0)
            label = QLabel(label_text)
            label.setToolTip(tooltip)
            field = QLineEdit(current)
            field.setPlaceholderText(default)
            clear = QPushButton("Clear")
            clear.setMaximumWidth(58)
            clear.clicked.connect(field.clear)
            row_l.addWidget(label, 1)
            row_l.addWidget(field, 2)
            row_l.addWidget(clear)
            body_layout.addWidget(row)
            target[key] = field
            searchable_rows.append((row, " ".join([label_text, default, current, tooltip, key, search_text]).lower()))

        def apply_search(text: str) -> None:
            query = text.strip().lower()
            for row, haystack in searchable_rows:
                row.setVisible(not query or query in haystack)

        search.textChanged.connect(apply_search)

        if manual_vars:
            add_section("Block Variables")
            for var in manual_vars:
                key = str(var.get("name", ""))
                default = str(var.get("default", var.get("value", "")))
                add_override_row(key, default, str(params.get(key, "")), f"Child default: {default}", param_fields, key)

        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for item in internal_items:
            grouped.setdefault((item["uid"], item["type"]), []).append(item)
        for (uid, type_name), items in sorted(grouped.items(), key=lambda pair: (pair[0][0], pair[0][1])):
            add_section(f"{uid} ({type_name})")
            for item in items:
                linked = ", ".join(item.get("linked_keys", []) or [])
                tooltip = f"{item['key']} child default: {item['default']}"
                if linked:
                    tooltip += f"\nLinked override path(s): {linked}"
                add_override_row(
                    item["parameter"],
                    item["default"],
                    str(internal_values.get(item["key"], "")),
                    tooltip,
                    internal_fields,
                    item["key"],
                    linked,
                )

        body_layout.addStretch()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        root.addWidget(buttons)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        def equivalent_override_value(left: str, right: str) -> bool:
            left = left.strip()
            right = right.strip()
            if self.float_like(left) and self.float_like(right):
                return math.isclose(float(left), float(right), rel_tol=1e-12, abs_tol=0.0)
            return left == right

        field_by_key: dict[str, QLineEdit] = {}
        field_by_key.update(param_fields)
        field_by_key.update(internal_fields)
        conflicts: list[str] = []
        for item in internal_items:
            key = str(item.get("key", ""))
            field = internal_fields.get(key)
            if not field:
                continue
            value = field.text().strip()
            if not value:
                continue
            for linked_key in item.get("linked_keys", []) or []:
                linked_field = field_by_key.get(str(linked_key))
                if not linked_field:
                    continue
                linked_value = linked_field.text().strip()
                if linked_value and not equivalent_override_value(value, linked_value):
                    conflicts.append(f"{key}={value} conflicts with {linked_key}={linked_value}")
        if conflicts:
            self.popup(
                "Manual Overrides",
                "These overrides refer to the same hierarchical value and must match:\n\n"
                + "\n".join(conflicts[:8]),
                "warning",
            )
            return

        self.record_undo()
        params = inst.setdefault("parameters", {})
        for key, field in param_fields.items():
            value = field.text().strip()
            if value:
                params[key] = value
            else:
                params.pop(key, None)

        new_internal = {
            key: field.text().strip()
            for key, field in internal_fields.items()
            if field.text().strip()
        }
        if new_internal:
            inst["internal_parameter_overrides"] = new_internal
        else:
            inst.pop("internal_parameter_overrides", None)
        self.mark_dirty()
        self.refresh_inspector()

    def float_or(self, text: str, fallback: float) -> float:
        try:
            return float(text)
        except ValueError:
            return fallback

    def float_like(self, text: str) -> bool:
        try:
            float(text)
            return True
        except ValueError:
            return False

    def add_cell_variable(self) -> None:
        cell = self.active_cell()
        if not cell:
            return
        name, ok = QInputDialog.getText(self, "Add Variable", "Variable name:")
        name = clean_name(name)
        if not ok or not name:
            return
        if name in RESERVED_VARIABLE_NAMES:
            self.popup("Reserved variable", "\"w\" is reserved for simulation frequency and cannot be set manually.", "warning")
            return
        value, ok = QInputDialog.getText(self, "Variable Default", "Default value:", text="0")
        if not ok:
            return
        self.record_undo()
        cell.setdefault("variables", []).append({"name": name, "value": value, "default": value, "scope": "cell"})
        self.mark_dirty(cell)
        self.refresh_inspector()

    def delete_cell_variable(self, row: int) -> None:
        cell = self.active_cell()
        if not cell or row < 0 or row >= len(cell.get("variables", [])):
            return
        self.record_undo()
        cell["variables"].pop(row)
        self.mark_dirty(cell)
        self.refresh_inspector()

    def set_instance_sim_flag(self, inst: dict[str, Any], key: str, checked: bool) -> None:
        inst.setdefault("simulation", {})[key] = checked
        self.mark_dirty()

    def edit_hb_settings(self, hb: dict[str, Any], owner: dict[str, Any]) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("HB Simulation Settings")
        layout = QFormLayout(dialog)

        def list_to_str(lst: list) -> str:
            return ", ".join(str(x) for x in lst) if lst else ""

        pump_ports = QLineEdit(list_to_str(hb.get("pump_ports", [])))
        pump_freqs = QLineEdit(list_to_str(hb.get("pump_frequencies", [])))
        pump_currents = QLineEdit(list_to_str(hb.get("pump_currents", [])))
        dc_ports = QLineEdit(list_to_str(hb.get("dc_ports", [])))
        dc_currents = QLineEdit(list_to_str(hb.get("dc_currents", [])))
        mod_harmonics_val = hb.get("modulation_harmonics", 10)
        pump_harmonics_val = hb.get("pump_harmonics", 20)
        mod_harm = QLineEdit(list_to_str(mod_harmonics_val if isinstance(mod_harmonics_val, list) else [mod_harmonics_val or 10]))
        pump_harm = QLineEdit(list_to_str(pump_harmonics_val if isinstance(pump_harmonics_val, list) else [pump_harmonics_val or 20]))
        threewave = QCheckBox()
        threewave.setChecked(bool(hb.get("threewave_mixing", True)))
        fourwave = QCheckBox()
        fourwave.setChecked(bool(hb.get("fourwave_mixing", True)))

        layout.addRow("Pump ports (comma-separated)", pump_ports)
        layout.addRow("Pump frequencies GHz (comma-separated)", pump_freqs)
        layout.addRow("Pump currents (comma-separated)", pump_currents)
        layout.addRow("DC ports (comma-separated)", dc_ports)
        layout.addRow("DC currents (comma-separated)", dc_currents)
        layout.addRow("Modulation harmonics (comma-separated, one per pump)", mod_harm)
        layout.addRow("Pump harmonics (comma-separated, one per pump)", pump_harm)
        layout.addRow("Three-wave mixing", threewave)
        layout.addRow("Four-wave mixing", fourwave)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        layout.addRow(buttons)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        def parse_list(text: str) -> list[str]:
            return [x.strip() for x in text.split(",") if x.strip()]

        self.record_undo()
        hb["pump_ports"] = parse_list(pump_ports.text())
        hb["pump_frequencies"] = [float(x) for x in parse_list(pump_freqs.text()) if x]
        hb["pump_currents"] = parse_list(pump_currents.text())
        hb["dc_ports"] = parse_list(dc_ports.text())
        hb["dc_currents"] = parse_list(dc_currents.text())
        hb["modulation_harmonics"] = [int(x) for x in parse_list(mod_harm.text()) if x]
        hb["pump_harmonics"] = [int(x) for x in parse_list(pump_harm.text()) if x]
        hb["threewave_mixing"] = threewave.isChecked()
        hb["fourwave_mixing"] = fourwave.isChecked()
        self.mark_dirty()
        self.refresh_inspector()

    def edit_repeat_settings(self) -> None:
        inst = self.find_instance(self.selected.id if self.selected else None)
        if not inst:
            self.popup("Repeat Settings", "Select a block first.")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Repeat Settings: {inst.get('uid')}")
        layout = QVBoxLayout(dialog)
        form = QFormLayout()
        count = QSpinBox()
        count.setRange(1, 1_000_000)
        count.setValue(max(1, repeat_count_value(inst.get("repeat_count", 1))))
        form.addRow("Repeat count", count)
        layout.addLayout(form)
        table = QTableWidget(max(1, len(inst.get("repeat_connections", []))), 2)
        table.setHorizontalHeaderLabels(["Out port expression", "In port expression"])
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        connections = list(inst.get("repeat_connections", [])) or [{"out": "", "in": ""}]
        for row, connection in enumerate(connections):
            table.setItem(row, 0, QTableWidgetItem(str(connection.get("out", ""))))
            table.setItem(row, 1, QTableWidgetItem(str(connection.get("in", ""))))
        layout.addWidget(table)
        row_buttons = QWidget()
        row_layout = QHBoxLayout(row_buttons)
        add_row = QPushButton("Add Connection")
        remove_row = QPushButton("Remove Selected")
        row_layout.addWidget(add_row)
        row_layout.addWidget(remove_row)
        layout.addWidget(row_buttons)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        layout.addWidget(buttons)

        def append_row() -> None:
            row = table.rowCount()
            table.insertRow(row)
            table.setItem(row, 0, QTableWidgetItem(""))
            table.setItem(row, 1, QTableWidgetItem(""))

        def remove_selected_row() -> None:
            row = table.currentRow()
            if row >= 0 and table.rowCount() > 1:
                table.removeRow(row)

        add_row.clicked.connect(append_row)
        remove_row.clicked.connect(remove_selected_row)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        dialog.resize(520, 340)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        if count.value() < 1:
            self.popup("Invalid Repeat Count", "Repeat count must be at least 1.", "warning")
            return
        repeat_connections = []
        for row in range(table.rowCount()):
            out_item = table.item(row, 0)
            in_item = table.item(row, 1)
            out_text = out_item.text().strip() if out_item else ""
            in_text = in_item.text().strip() if in_item else ""
            if out_text or in_text:
                if not out_text or not in_text:
                    self.popup("Incomplete Repeat Connection", "Each repeat connection needs both an out and an in expression.", "warning")
                    return
                repeat_connections.append({"out": out_text, "in": in_text})
        self.record_undo()
        inst["repeat_count"] = count.value()
        inst["repeat_connections"] = repeat_connections
        self.mark_dirty()
        self.refresh_all()

    def reset_selected_parameters(self) -> None:
        inst = self.find_instance(self.selected.id if self.selected else None)
        if not inst:
            return
        self.record_undo()
        inst["parameters"] = {}
        inst.pop("internal_parameter_overrides", None)
        self.mark_dirty()
        self.refresh_inspector()

    def reset_selected_symbol_dialogless(self) -> None:
        inst = self.find_instance(self.selected.id if self.selected else None)
        if not inst:
            return
        self.record_undo()
        inst["symbol"] = default_symbol(inst.get("port_names", []))
        inst["symbol_port_layout"] = inst["symbol"]["port_layout"]
        self.mark_dirty()
        self.refresh_all()

    def edit_simulation_setup(self) -> None:
        cell = self.active_cell()
        if not cell:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Simulation Setup")
        layout = QFormLayout(dialog)

        def ordered_pins() -> list[dict[str, Any]]:
            return [
                pin
                for _idx, pin in sorted(
                    enumerate(cell.get("pins", [])),
                    key=lambda item: (int(item[1].get("order", 0) or 0), item[0]),
                )
            ]

        def list_to_str(value: Any) -> str:
            values = as_list(value)
            return ", ".join(str(x) for x in values) if values else ""

        def parse_list(text: str) -> list[str]:
            return [x.strip() for x in text.split(",") if x.strip()]

        def float_list(text: str) -> list[float]:
            return [float(x) for x in parse_list(text)]

        def int_list(text: str, fallback: int) -> list[int]:
            values = [int(x) for x in parse_list(text)]
            return values or [fallback]

        ports = [pin.get("name", "") for pin in ordered_pins() if pin.get("name")]
        saved_ports = [
            str(port)
            for port in cell.get("simulation", {}).get("input_ports", []) + cell.get("simulation", {}).get("output_ports", [])
            if port
        ]
        x_saved = cell.get("simulation", {}).get("x", {})
        saved_ports.extend(str(x_saved.get(key, "")) for key in ["input_port", "output_port", "pump_port", "dc_port"] if x_saved.get(key))
        saved_ports.extend(str(port) for port in as_list(x_saved.get("pump_ports", [])) if port)
        saved_ports.extend(str(port) for port in as_list(x_saved.get("dc_ports", [])) if port)
        port_choices = [""]
        for port in ports + saved_ports:
            if port and port not in port_choices:
                port_choices.append(port)

        def port_combo(current: str) -> QComboBox:
            box = QComboBox()
            box.addItems(port_choices)
            if current and current not in port_choices:
                box.addItem(current)
            box.setCurrentText(current if current else "")
            return box

        mode = QComboBox()
        mode.addItems(["s", "x"])
        mode.setCurrentText(cell["simulation"].get("mode", "s"))
        input_port = port_combo(next(iter(cell["simulation"].get("input_ports", [])), ""))
        output_port = port_combo(next(iter(cell["simulation"].get("output_ports", [])), ""))
        start = QDoubleSpinBox()
        start.setRange(0.0, 1e9)
        start.setDecimals(9)
        start.setValue(float(cell["simulation"].get("freq_start", 2.0)))
        stop = QDoubleSpinBox()
        stop.setRange(0.0, 1e9)
        stop.setDecimals(9)
        stop.setValue(float(cell["simulation"].get("freq_stop", 20.0)))
        points = QSpinBox()
        points.setRange(1, 10000000)
        points.setValue(int(cell["simulation"].get("freq_points", 200) or 200))
        sweep = QComboBox()
        sweep.addItems(["linear", "log"])
        sweep.setCurrentText(cell["simulation"].get("sweep_type", "linear"))
        title = QLineEdit(cell["simulation"].get("figure_title") or cell.get("name", ""))
        z0 = QDoubleSpinBox()
        z0.setRange(0.000001, 1e12)
        z0.setDecimals(6)
        z0.setValue(float(cell.get("simulation", {}).get("z0", 50.0)))
        x = cell["simulation"].setdefault("x", {})
        x_input = port_combo(str(x.get("input_port", "")))
        x_output = port_combo(str(x.get("output_port", "")))
        x_pump_ports = [str(port) for port in as_list(x.get("pump_ports", x.get("pump_port", ""))) if str(port)]
        x_pump = QLineEdit(list_to_str(x_pump_ports))
        x_pump.setPlaceholderText("e.g. P_in, P_in")
        x_dc_ports = [str(port) for port in as_list(x.get("dc_ports", x.get("dc_port", ""))) if str(port)]
        x_dc = QLineEdit(list_to_str(x_dc_ports))
        x_dc.setPlaceholderText("e.g. P_dc1, P_dc2")
        x_pump_freqs = QLineEdit(list_to_str(x.get("pump_frequencies", x.get("pump_frequency", 7.12))))
        x_pump_current = QLineEdit(list_to_str(x.get("pump_currents", x.get("pump_current", ""))))
        x_dc_current = QLineEdit(list_to_str(x.get("dc_currents", x.get("dc_current", ""))))
        x_mod = QLineEdit(list_to_str(x.get("modulation_harmonics", [10]) or [10]))
        x_pump_harm = QLineEdit(list_to_str(x.get("pump_harmonics", [20]) or [20]))
        threewave = QCheckBox()
        threewave.setChecked(bool(x.get("threewave_mixing", True)))
        fourwave = QCheckBox()
        fourwave.setChecked(bool(x.get("fourwave_mixing", True)))
        from PyQt6.QtWidgets import QGroupBox
        x_group = QGroupBox("X-parameter setup")
        x_form = QFormLayout(x_group)
        x_form.addRow("X input port", x_input)
        x_form.addRow("X output port", x_output)
        x_form.addRow("Pump ports", x_pump)
        x_form.addRow("Pump frequencies GHz (comma-separated)", x_pump_freqs)
        x_form.addRow("Pump currents (comma-separated)", x_pump_current)
        x_form.addRow("DC ports (comma-separated)", x_dc)
        x_form.addRow("DC currents (comma-separated)", x_dc_current)
        x_form.addRow("Modulation harmonics (comma-separated)", x_mod)
        x_form.addRow("Pump harmonics (comma-separated)", x_pump_harm)
        x_form.addRow("Three-wave mixing", threewave)
        x_form.addRow("Four-wave mixing", fourwave)
        s_group = QGroupBox("S-parameter ports")
        s_form = QFormLayout(s_group)
        s_form.addRow("Input port", input_port)
        s_form.addRow("Output port", output_port)
        hb_group = QGroupBox("HB top-block settings")
        hb_layout = QVBoxLayout(hb_group)
        _sim_hb = cell.get("simulation", {}).get("hb", {})
        if _sim_hb.get("top_block"):
            hb_layout.addWidget(QLabel(self.instance_hb_settings_summary(_sim_hb)))
            hb_settings = QPushButton("Edit HB Settings")
            hb_settings.clicked.connect(lambda _checked=False, c=cell: self.edit_hb_settings(c.setdefault("simulation", {}).setdefault("hb", {}), c))
            hb_layout.addWidget(hb_settings)
        else:
            top_names = sorted(
                str(inst.get("uid", ""))
                for inst in cell.get("instances", [])
                if inst.get("hb", {}).get("top_block")
            )
            msg = "HB top block: " + (", ".join(top_names) if top_names else "none marked")
            hb_layout.addWidget(QLabel(msg))
        layout.addRow("Mode", mode)
        layout.addRow(s_group)
        layout.addRow("Start GHz", start)
        layout.addRow("Stop GHz", stop)
        layout.addRow("Points", points)
        layout.addRow("Sweep type", sweep)
        layout.addRow("Figure title", title)
        layout.addRow("z0", z0)
        layout.addRow(x_group)
        layout.addRow(hb_group)

        def _update_visibility(m: str) -> None:
            is_x_mode = m == "x"
            s_group.setVisible(not is_x_mode)
            hb_group.setVisible(not is_x_mode)
            x_group.setVisible(is_x_mode)
            dialog.adjustSize()

        _update_visibility(mode.currentText())
        mode.currentTextChanged.connect(_update_visibility)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        layout.addRow(buttons)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            sim = cell["simulation"]
            sim["mode"] = mode.currentText()
            sim["input_ports"] = [input_port.currentText()] if input_port.currentText() else []
            sim["output_ports"] = [output_port.currentText()] if output_port.currentText() else []
            sim["freq_start"] = start.value()
            sim["freq_stop"] = stop.value()
            sim["freq_points"] = points.value()
            sim["sweep_type"] = sweep.currentText()
            sim["figure_title"] = title.text().strip() or cell.get("name")
            sim["z0"] = z0.value()
            x = sim.setdefault("x", {})
            x["input_port"] = x_input.currentText()
            x["output_port"] = x_output.currentText()
            pump_ports = parse_list(x_pump.text())
            x["pump_ports"] = pump_ports
            x["pump_port"] = pump_ports[0] if pump_ports else ""
            pump_frequencies = float_list(x_pump_freqs.text()) or [7.12]
            pump_currents = parse_list(x_pump_current.text())
            x["pump_frequencies"] = pump_frequencies
            x["pump_frequency"] = pump_frequencies[0]
            x["pump_currents"] = pump_currents
            x["pump_current"] = pump_currents[0] if pump_currents else ""
            dc_ports = parse_list(x_dc.text())
            dc_currents = parse_list(x_dc_current.text())
            x["dc_ports"] = dc_ports
            x["dc_port"] = dc_ports[0] if dc_ports else ""
            x["dc_currents"] = dc_currents
            x["dc_current"] = dc_currents[0] if dc_currents else ""
            x["modulation_harmonics"] = int_list(x_mod.text(), 10)
            x["pump_harmonics"] = int_list(x_pump_harm.text(), 20)
            x["threewave_mixing"] = threewave.isChecked()
            x["fourwave_mixing"] = fourwave.isChecked()
            self.mark_dirty(cell)

    def edit_port_order(self) -> None:
        cell = self.active_cell()
        if not cell:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Cell Ports")
        layout = QVBoxLayout(dialog)
        table = QTableWidget(len(cell.get("pins", [])), 3)
        table.setHorizontalHeaderLabels(["Order", "Name", "Net"])
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        for row, pin in enumerate(cell.get("pins", [])):
            table.setItem(row, 0, QTableWidgetItem(str(row + 1)))
            table.setItem(row, 1, QTableWidgetItem(pin.get("name", "")))
            table.setItem(row, 2, QTableWidgetItem(pin.get("net_id", "")))
        layout.addWidget(table)
        buttons_row = QWidget()
        buttons_layout = QVBoxLayout(buttons_row)
        up = QPushButton("Move Up")
        down = QPushButton("Move Down")
        delete = QPushButton("Delete")
        buttons_layout.addWidget(up)
        buttons_layout.addWidget(down)
        buttons_layout.addWidget(delete)
        layout.addWidget(buttons_row)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        layout.addWidget(buttons)

        def selected_row() -> int:
            return table.currentRow()

        def swap_rows(a: int, b: int) -> None:
            pins = cell.get("pins", [])
            if a < 0 or b < 0 or a >= len(pins) or b >= len(pins):
                return
            pins[a], pins[b] = pins[b], pins[a]
            dialog.accept()
            self.edit_port_order()

        up.clicked.connect(lambda: swap_rows(selected_row(), selected_row() - 1))
        down.clicked.connect(lambda: swap_rows(selected_row(), selected_row() + 1))
        delete.clicked.connect(lambda: self.delete_pin_from_dialog(cell, selected_row(), dialog))
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.record_undo()
            for row, pin in enumerate(cell.get("pins", [])):
                item = table.item(row, 1)
                if item:
                    pin["name"] = clean_name(item.text())
                pin["order"] = row + 1
            self.mark_dirty(cell)
            self.refresh_all()

    def edit_matrix_definition(self) -> None:
        cell = self.active_cell()
        if not cell or cell.get("type") != "matrix":
            return
        matrix = cell.setdefault("matrix", {"port_names": ["p1", "p2"], "matrix_type": "ABCD", "values": "[[1, 0], [0, 1]]", "definitions": ""})
        dialog = QDialog(self)
        dialog.setWindowTitle("Matrix Cell Definition")
        layout = QFormLayout(dialog)
        ports = QLineEdit(", ".join(matrix.get("port_names", [])))
        matrix_type = QComboBox()
        matrix_type.addItems(["ABCD", "S", "Y", "Z"])
        matrix_type.setCurrentText(matrix.get("matrix_type", "ABCD"))
        vars_text = QPlainTextEdit("\n".join(
            f"{v.get('name')}={v.get('default', v.get('value', ''))}"
            for v in cell.get("variables", [])
        ))
        vars_text.setMaximumHeight(90)
        vars_text.setPlaceholderText("C_cross=1e-15\nR=50")
        defs_edit = QPlainTextEdit(str(matrix.get("definitions", "")))
        defs_edit.setMinimumHeight(110)
        defs_edit.setPlaceholderText(
            "I2 = ComplexF64[1 0; 0 1]\n"
            "Z2 = ComplexF64[0 0; 0 0]\n"
            "Y_c = im * ω * C_cross"
        )
        values = QPlainTextEdit(str(matrix.get("values", "")))
        values.setMinimumHeight(120)
        layout.addRow("Port names", ports)
        layout.addRow("Matrix type", matrix_type)
        layout.addRow("Variables (name=default, exposed to parent)", vars_text)
        layout.addRow("Definitions (hardcoded constants and derived expressions)", defs_edit)
        layout.addRow("Matrix expression", values)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        layout.addRow(buttons)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.record_undo()
        port_names = [clean_name(part) for part in ports.text().split(",") if clean_name(part)]
        matrix["port_names"] = port_names
        matrix["matrix_type"] = matrix_type.currentText()
        matrix["definitions"] = defs_edit.toPlainText()
        matrix["values"] = values.toPlainText()
        cell["variables"] = parse_variables_text(vars_text.toPlainText())
        cell["symbol"] = default_symbol(port_names)
        cell["symbol_port_layout"] = cell["symbol"]["port_layout"]
        self.mark_dirty(cell)
        self.refresh_all()

    def edit_generated_hb_source(self) -> None:
        cell = self.active_cell()
        if not cell or cell.get("type") != "generated_hb":
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Edit Julia HB Source")
        layout = QFormLayout(dialog)
        source = QPlainTextEdit(str(cell.get("generated_source", "")))
        source.setMinimumHeight(380)
        trust = QCheckBox("I trust this Julia code and allow it to execute")
        trust.setChecked(True)
        layout.addRow("Julia source", source)
        layout.addRow("", trust)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        layout.addRow(buttons)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        if not trust.isChecked():
            self.popup("Trusted execution required", "Julia HB source execution must be explicitly trusted.", "warning")
            return
        src_text = source.toPlainText()
        progress = QProgressDialog("Running Julia circuit probe...", None, 0, 0, self)
        progress.setWindowTitle("Updating HB Source")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)
        progress.show()
        loop = QEventLoop()
        probe_result: list[dict] = [{}]
        probe_error: list[str] = [""]
        worker = _JuliaProbeThread(src_text, timeout=120)
        def _done(probe: dict) -> None:
            probe_result[0] = probe
            loop.quit()
        def _fail(msg: str) -> None:
            probe_error[0] = msg
            loop.quit()
        worker.probe_done.connect(_done)
        worker.probe_failed.connect(_fail)
        worker.start()
        loop.exec()
        progress.close()
        if probe_error[0]:
            self.popup("Julia import failed", probe_error[0], "warning")
            return
        try:
            updated = build_generated_cell(cell.get("name", "generated_hb"), src_text, probe_result[0])
        except Exception as exc:
            self.popup("Julia import failed", str(exc), "warning")
            return
        self.record_undo()
        keep = {"id": cell.get("id"), "name": cell.get("name"), "fileName": cell.get("fileName"), "dirty": True}
        cell.update(updated)
        cell.update({key: value for key, value in keep.items() if value is not None})
        self.mark_dirty(cell)
        self.invalidate_generated_view()
        self.refresh_all()

    def refresh_generated_hb_summary(self) -> None:
        cell = self.active_cell()
        if not cell or cell.get("type") != "generated_hb":
            return
        src_text = str(cell.get("generated_source", ""))
        progress = QProgressDialog("Running Julia circuit probe...", None, 0, 0, self)
        progress.setWindowTitle("Regenerating Summary")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)
        progress.show()
        loop = QEventLoop()
        probe_result: list[dict] = [{}]
        probe_error: list[str] = [""]
        worker = _JuliaProbeThread(src_text, timeout=120)
        def _done(probe: dict) -> None:
            probe_result[0] = probe
            loop.quit()
        def _fail(msg: str) -> None:
            probe_error[0] = msg
            loop.quit()
        worker.probe_done.connect(_done)
        worker.probe_failed.connect(_fail)
        worker.start()
        loop.exec()
        progress.close()
        if probe_error[0]:
            self.popup("Julia summary failed", probe_error[0], "warning")
            return
        try:
            updated = build_generated_cell(cell.get("name", "generated_hb"), src_text, probe_result[0])
        except Exception as exc:
            self.popup("Julia summary failed", str(exc), "warning")
            return
        cell["generated_summary"] = updated.get("generated_summary", {})
        cell["generated_display_components"] = updated.get("generated_display_components", [])
        current_names = [pin.get("name") for pin in cell.get("pins", []) if pin.get("name")]
        new_names = [pin.get("name") for pin in updated.get("pins", []) if pin.get("name")]
        if not current_names or len(current_names) != len(new_names):
            cell["pins"] = updated.get("pins", [])
            cell["symbol"] = updated.get("symbol")
            cell["symbol_port_layout"] = updated.get("symbol_port_layout")
        self.mark_dirty(cell)
        self.invalidate_generated_view()
        self.refresh_all()

    def delete_pin_from_dialog(self, cell: dict[str, Any], row: int, dialog: QDialog) -> None:
        if 0 <= row < len(cell.get("pins", [])):
            self.record_undo()
            cell["pins"].pop(row)
            for i, pin in enumerate(cell.get("pins", [])):
                pin["order"] = i + 1
            self.mark_dirty(cell)
            dialog.accept()
            self.refresh_all()

    def set_z0(self) -> None:
        cell = self.active_cell()
        if not cell:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Set z0")
        layout = QFormLayout(dialog)
        value = QDoubleSpinBox()
        value.setRange(0.000001, 1e12)
        value.setDecimals(6)
        value.setValue(float(cell.get("simulation", {}).get("z0", 50)))
        current_only = QRadioButton("Current cell only")
        project_default = QRadioButton("Current project default")
        selected_cells = QRadioButton("Selected cells")
        current_only.setChecked(True)
        layout.addRow("Reference impedance z0", value)
        layout.addRow(current_only)
        layout.addRow(project_default)
        layout.addRow(selected_cells)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        layout.addRow(buttons)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.record_undo()
        if project_default.isChecked() and self.project:
            for item in self.project.get("cells", {}).values():
                if not item.get("readOnly"):
                    item.setdefault("simulation", {})["z0"] = value.value()
                    item["dirty"] = True
        elif selected_cells.isChecked() and self.project:
            for item in self.project.get("cells", {}).values():
                if not item.get("readOnly") and item.get("id") == self.active_cell_id:
                    item.setdefault("simulation", {})["z0"] = value.value()
                    item["dirty"] = True
        else:
            cell.setdefault("simulation", {})["z0"] = value.value()
            self.mark_dirty(cell)
        self.refresh_all()

    def on_tab_changed(self, index: int) -> None:
        cell_id = self.tabs.tabBar().tabData(index)
        if cell_id:
            self.active_cell_id = cell_id
            cell = self.project.get("cells", {}).get(cell_id) if self.project else None
            if self.is_julia_code_generated_cell(cell):
                self.request_generated_view_prompt()
            self.selected = None
            self.remember_open_cell()
            self.refresh_inspector()
            self._refresh_view()

    def remember_open_cell(self) -> None:
        cell = self.active_cell()
        if not self.project or not cell or cell.get("readOnly"):
            return
        name = str(cell.get("name", ""))
        if not name:
            return
        recent = [name] + [item for item in self.project.get("recent_cells", []) if item != name]
        self.project["recent_cells"] = recent[:12]

    def on_explorer_double_click(self, item: QTreeWidgetItem) -> None:
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if not data:
            return
        if data[0] == "cell":
            self.active_cell_id = data[1]
            cell = self.project.get("cells", {}).get(data[1]) if self.project else None
            if self.is_julia_code_generated_cell(cell):
                self.request_generated_view_prompt()
            self.ensure_cell_tab(data[1])
            self.selected = None
            self.remember_open_cell()
            self.refresh_all()
        elif data[0] == "builtin":
            self.pending_instance = data[1]
            self.pending_tag = None
            self.mode = "place"
            self.canvas.update_preview_position_from_cursor()
            self.canvas.update_placement_preview()
            self.statusBar().showMessage(f"Click the canvas to place {data[1]}")
        elif data[0] == "import":
            alias, cid = data[1], data[2]
            source = next((c for c in self.project.get("importedCells", {}).get(alias, []) if c.get("id") == cid), None)
            if source:
                copy = json.loads(json.dumps(source))
                tab_id = f"import:{alias}:{cid}"
                copy["id"] = tab_id
                copy["name"] = f"{alias}/{source.get('name')}"
                copy["readOnly"] = True
                copy["dirty"] = False
                self.project["cells"][tab_id] = copy
                self.active_cell_id = tab_id
                if self.is_julia_code_generated_cell(copy):
                    self.request_generated_view_prompt()
                self.ensure_cell_tab(tab_id)
                self.refresh_all()

    def delete_selected(self) -> None:
        cell = self.active_cell()
        if not cell:
            return
        self.record_undo()
        selected_uids = {item.inst.get("uid") for item in self.canvas.scene().selectedItems() if isinstance(item, BlockItem)}
        selected_nets = {item.net_id for item in self.canvas.scene().selectedItems() if isinstance(item, WireItem)}
        selected_pins = {item.tag_id for item in self.canvas.scene().selectedItems() if isinstance(item, TagItem) and item.kind == "pin"}
        selected_labels = {item.tag_id for item in self.canvas.scene().selectedItems() if isinstance(item, TagItem) and item.kind == "label"}
        if self.selected and self.selected.kind == "instance":
            selected_uids.add(self.selected.id)
        elif self.selected and self.selected.kind == "net":
            selected_nets.add(self.selected.id)
        elif self.selected and self.selected.kind == "pin":
            selected_pins.add(self.selected.id)
        elif self.selected and self.selected.kind == "label":
            selected_labels.add(self.selected.id)
        if selected_uids:
            cell["instances"] = [inst for inst in cell.get("instances", []) if inst.get("uid") not in selected_uids]
            cell["nets"] = [net for net in cell.get("nets", []) if not any(ep.get("instance_uid") in selected_uids for ep in net.get("endpoints", []))]
        if selected_nets:
            cell["nets"] = [net for net in cell.get("nets", []) if net.get("id") not in selected_nets]
            cell["pins"] = [pin for pin in cell.get("pins", []) if pin.get("net_id") not in selected_nets]
            cell["labels"] = [label for label in cell.get("labels", []) if label.get("net_id") not in selected_nets]
        if selected_pins:
            cell["pins"] = [pin for pin in cell.get("pins", []) if pin.get("id") not in selected_pins]
            for i, pin in enumerate(sorted(cell.get("pins", []), key=lambda p: int(p.get("order", 0) or 0)), start=1):
                pin["order"] = i
        if selected_labels:
            cell["labels"] = [label for label in cell.get("labels", []) if label.get("id") not in selected_labels]
        if self.selected and self.selected.kind == "net" and not selected_nets:
            self.delete_net(self.selected.id)
            return
        self.selected = None
        self.mark_dirty(cell)
        self.refresh_all()

    def select_all(self) -> None:
        cell = self.active_cell()
        if not cell or not cell.get("instances"):
            return
        self.selected = Selection("instance", cell["instances"][0]["uid"])
        for item in self.canvas.scene().items():
            if isinstance(item, (BlockItem, WireItem, TagItem)):
                item.setSelected(True)
        self.refresh_inspector()

    def copy_selection(self) -> None:
        cell = self.active_cell()
        if not cell:
            return
        selected_uids = [item.inst.get("uid") for item in self.canvas.scene().selectedItems() if isinstance(item, BlockItem)]
        if self.selected and self.selected.kind == "instance" and self.selected.id not in selected_uids:
            selected_uids.append(self.selected.id)
        instances = [json.loads(json.dumps(inst)) for inst in cell.get("instances", []) if inst.get("uid") in selected_uids]
        if not instances:
            self.add_message("Warning", "No block selection to copy.")
            return
        self.clipboard = {"kind": "instances", "instances": instances}
        self.add_message("Info", f"Copied {len(instances)} block(s).")

    def paste_selection(self) -> None:
        cell = self.active_cell()
        if not cell or not self.clipboard or self.clipboard.get("kind") != "instances":
            return
        self.record_undo()
        new_ids = []
        for source in self.clipboard.get("instances", []):
            copy = json.loads(json.dumps(source))
            prefix = re.sub(r"\d+$", "", copy.get("uid", "")) or "U"
            copy["uid"] = self.unique_uid(prefix)
            pos = copy.get("position", [0, 0])
            copy["position"] = [pos[0] + 30, pos[1] + 30]
            cell.setdefault("instances", []).append(copy)
            new_ids.append(copy["uid"])
        if new_ids:
            self.selected = Selection("instance", new_ids[-1])
        self.mark_dirty(cell)
        self.refresh_all()

    def duplicate_selected(self) -> None:
        self.copy_selection()
        self.paste_selection()

    def rotate_selected(self) -> None:
        inst = self.find_instance(self.selected.id if self.selected else None)
        if not inst:
            return
        self.record_undo()
        inst["rotation_degrees"] = (float(inst.get("rotation_degrees", 0)) + 90) % 360
        self.update_routes_for_instance(str(inst.get("uid", "")))
        self.mark_dirty()
        self.refresh_all()

    def replace_selected_block(self) -> None:
        inst = self.find_instance(self.selected.id if self.selected else None)
        if not inst or not self.project:
            return
        dialog = InstancePalette(self.library_items(), self)
        if dialog.exec() != QDialog.DialogCode.Accepted or not dialog.selected_name:
            return
        ref = self.find_library_item(dialog.selected_name)
        if not ref:
            return
        self.record_undo()
        ports = [str(port) for port in ref.get("port_names", [])]
        ref_vars = list(ref.get("variables", []))
        vars_ = [v for v in ref_vars if v.get("name") not in RESERVED_VARIABLE_NAMES]
        inst["type_name"] = ref.get("type_name") or ref.get("name") or dialog.selected_name
        inst["source"] = ref.get("source", "local")
        inst["source_project"] = ref.get("source_project", "")
        inst["port_names"] = ports
        inst["port_count"] = len(ports)
        inst["parameters"] = {v.get("name"): v.get("default", "") for v in vars_}
        inst["parameter_order"] = [v.get("name") for v in vars_]
        inst["parameter_kinds"] = {v.get("name"): "positional" for v in vars_}
        inst.pop("internal_parameter_overrides", None)
        inst["has_frequency_dependency"] = any(v.get("name") in RESERVED_VARIABLE_NAMES for v in ref_vars)
        inst["symbol"] = json.loads(json.dumps(ref.get("symbol") or default_symbol(ports)))
        self.copy_hb_settings_to_instance(inst, ref)
        inst["symbol_port_layout"] = inst["symbol"].get("port_layout", [])
        self.mark_dirty()
        self.refresh_all()

    def edit_symbol(self) -> None:
        inst = self.find_instance(self.selected.id if self.selected else None)
        if not inst:
            self.popup("Symbol Editor", "Select a block first.")
            return
        symbol = inst.setdefault("symbol", default_symbol(inst.get("port_names", [])))
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Symbol Editor: {inst.get('uid')}")
        layout = QVBoxLayout(dialog)
        form = QFormLayout()
        width = QLineEdit(str(symbol.get("width", 120)))
        height = QLineEdit(str(symbol.get("height", 70)))
        label_pos = QComboBox()
        label_pos.addItems(["center", "top", "bottom"])
        label_pos.setCurrentText(symbol.get("label_position", "center"))
        form.addRow("Width", width)
        form.addRow("Height", height)
        form.addRow("Label position", label_pos)
        layout.addLayout(form)
        table = QTableWidget(len(symbol.get("port_layout", [])), 4)
        table.setHorizontalHeaderLabels(["Port", "Side", "Position", "Label visible"])
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        for row, port in enumerate(symbol.get("port_layout", [])):
            table.setItem(row, 0, QTableWidgetItem(str(port.get("port"))))
            side = QComboBox()
            side.addItems(["left", "right", "top", "bottom"])
            side.setCurrentText(port.get("side", "right"))
            table.setCellWidget(row, 1, side)
            table.setItem(row, 2, QTableWidgetItem(str(port.get("position", 0.5))))
            visible = QComboBox()
            visible.addItems(["true", "false"])
            visible.setCurrentText("true" if port.get("label_visible", True) else "false")
            table.setCellWidget(row, 3, visible)
        layout.addWidget(table)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Reset)
        layout.addWidget(buttons)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        buttons.button(QDialogButtonBox.StandardButton.Reset).clicked.connect(lambda: self.reset_selected_symbol(dialog))
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.record_undo()
            symbol["width"] = self.float_or(width.text(), 120)
            symbol["height"] = self.float_or(height.text(), 70)
            symbol["label_position"] = label_pos.currentText()
            for row, port in enumerate(symbol.get("port_layout", [])):
                side = table.cellWidget(row, 1)
                visible = table.cellWidget(row, 3)
                port["side"] = side.currentText() if isinstance(side, QComboBox) else port.get("side", "right")
                port["position"] = max(0.0, min(1.0, self.float_or(table.item(row, 2).text() if table.item(row, 2) else "0.5", 0.5)))
                port["label_visible"] = visible.currentText() == "true" if isinstance(visible, QComboBox) else True
            inst["symbol_port_layout"] = [{"port": p.get("port"), "side": p.get("side"), "position": p.get("position")} for p in symbol.get("port_layout", [])]
            self.update_routes_for_instance(str(inst.get("uid", "")))
            self.mark_dirty()
            self.refresh_all()

    def reset_selected_symbol(self, dialog: QDialog) -> None:
        inst = self.find_instance(self.selected.id if self.selected else None)
        if not inst:
            return
        self.record_undo()
        inst["symbol"] = default_symbol(inst.get("port_names", []))
        inst["symbol_port_layout"] = inst["symbol"]["port_layout"]
        self.update_routes_for_instance(str(inst.get("uid", "")))
        self.mark_dirty()
        dialog.accept()
        self.refresh_all()

    def open_selected_block(self) -> None:
        inst = self.find_instance(self.selected.id if self.selected else None)
        if not inst or not self.project:
            return
        local = next((c for c in self.project.get("cells", {}).values() if c.get("name") == inst.get("type_name") and not c.get("readOnly")), None)
        if local:
            self.active_cell_id = local["id"]
            if self.is_julia_code_generated_cell(local):
                self.request_generated_view_prompt()
            self.ensure_cell_tab(local["id"])
            self.selected = None
            self.refresh_all()
            return
        alias = inst.get("source_project") or str(inst.get("source", "")).replace("imported:", "")
        imported = next((c for c in self.project.get("importedCells", {}).get(alias, []) if c.get("name") == inst.get("type_name")), None)
        if imported:
            copy = json.loads(json.dumps(imported))
            tab_id = f"import:{alias}:{imported.get('id')}"
            copy["id"] = tab_id
            copy["name"] = f"{alias}/{imported.get('name')}"
            copy["readOnly"] = True
            copy["dirty"] = False
            self.project["cells"][tab_id] = copy
            self.active_cell_id = tab_id
            if self.is_julia_code_generated_cell(copy):
                self.request_generated_view_prompt()
            self.ensure_cell_tab(tab_id)
            self.selected = None
            self.refresh_all()
            return
        builtin = next((b for b in self.builtins if b.get("name") == inst.get("type_name")), None)
        if builtin:
            self.show_builtin_viewer(builtin)
            return
        self.add_message("Error", f"Cannot open missing block reference {inst.get('type_name')}.")

    def show_builtin_viewer(self, builtin: dict[str, Any]) -> None:
        path = Path(str(builtin.get("actual_path") or (LOGIC_DIR / builtin.get("path", ""))))
        raw = ""
        if path.exists():
            raw = path.read_text(encoding="utf-8")
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Built-in: {builtin.get('name')}")
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel(f"Group: {builtin.get('group')}"))
        layout.addWidget(QLabel(f"Ports: {', '.join(builtin.get('port_names', [])) or 'none'}"))
        layout.addWidget(QLabel("Variables:"))
        vars_text = QPlainTextEdit("\n".join(f"{v.get('name')} = {v.get('default', '')}" for v in builtin.get("variables", [])) or "none")
        vars_text.setReadOnly(True)
        vars_text.setMaximumHeight(100)
        layout.addWidget(vars_text)
        raw_text = QPlainTextEdit(raw)
        raw_text.setReadOnly(True)
        layout.addWidget(raw_text)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.resize(720, 520)
        dialog.exec()

    def fit_canvas(self) -> None:
        self.canvas.fitInView(self.canvas.scene().itemsBoundingRect().adjusted(-80, -80, 80, 80), Qt.AspectRatioMode.KeepAspectRatio)

    def zoom_canvas(self, factor: float) -> None:
        self.canvas.scale(factor, factor)

    def validate_cell(self, cell: dict[str, Any]) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", cell.get("name", "")):
            out.append(("Error", "Cell name is invalid."))
        if self.project:
            matching = [c for c in self.project.get("cells", {}).values() if c.get("name") == cell.get("name") and not c.get("readOnly")]
            if len(matching) > 1:
                out.append(("Error", f'Duplicate cell name "{cell.get("name")}".'))
        if cell.get("type") == "generated_hb":
            if not str(cell.get("generated_source", "")).strip():
                out.append(("Error", "Generated HB block has no Julia source."))
            if not cell.get("pins"):
                out.append(("Error", "Generated HB block has no exported ports."))
            if float(cell.get("simulation", {}).get("z0", 0) or 0) <= 0:
                out.append(("Error", "z0 must be positive."))
            return out
        seen: set[str] = set()
        instances_by_uid = {inst.get("uid"): inst for inst in cell.get("instances", [])}
        for inst in cell.get("instances", []):
            uid = inst.get("uid")
            if uid in seen:
                out.append(("Error", f"Duplicate block UID {uid}."))
            seen.add(uid)
            if int(inst.get("port_count", 0)) != len(inst.get("port_names", [])):
                out.append(("Error", f"{uid} has mismatched port count."))
            if repeat_count_value(inst.get("repeat_count", 1)) < 1:
                out.append(("Error", f"{uid} repeat count must be at least 1."))
            ref_name = f"{inst.get('source_project')}/{inst.get('type_name')}" if inst.get("source_project") else inst.get("type_name")
            if not self.find_library_item(str(ref_name)) and not self.find_library_item(str(inst.get("type_name", ""))):
                out.append(("Error", f"Missing referenced cell or built-in: {inst.get('type_name')}."))
            symbol_ports = {str(port.get("port")) for port in inst.get("symbol", {}).get("port_layout", [])}
            actual_ports = {str(port) for port in inst.get("port_names", [])}
            if symbol_ports and symbol_ports != actual_ports:
                out.append(("Warning", f"{uid} symbol ports do not match actual ports."))
            for key, value in inst.get("parameters", {}).items():
                if key in RESERVED_VARIABLE_NAMES:
                    continue
                if value in {None, ""}:
                    out.append(("Error", f"{uid} parameter {key} is missing."))
                elif isinstance(value, str) and value.count("(") != value.count(")"):
                    out.append(("Error", f"{uid} parameter {key} has an invalid expression."))
        valid_net_ids = {net.get("id") for net in cell.get("nets", [])}
        connected_port_keys: set[tuple[str, str]] = set()
        all_pins = cell.get("pins", [])
        all_labels = cell.get("labels", [])
        for net in cell.get("nets", []):
            endpoints = net.get("endpoints", [])
            net_id = net.get("id")
            net_has_pin = any(pin.get("net_id") == net_id for pin in all_pins)
            net_has_label = any(label.get("net_id") == net_id for label in all_labels)
            net_has_external_tag = net_has_pin or net_has_label
            if len(endpoints) < 2 and not net_has_external_tag:
                out.append(("Error", f"{net.get('id')} is dangling; connect it to at least two block ports."))
            endpoint_keys: set[tuple[str, str]] = set()
            for ep in net.get("endpoints", []):
                key = (str(ep.get("instance_uid", "")), str(ep.get("port", "")))
                if key in endpoint_keys:
                    out.append(("Warning", f"{net.get('id')} contains duplicate endpoint {key[0]}.{key[1]}."))
                endpoint_keys.add(key)
                if len(endpoints) >= 2 or net_has_external_tag:
                    connected_port_keys.add(key)
                inst = instances_by_uid.get(ep.get("instance_uid"))
                if not inst:
                    out.append(("Error", f"Wire endpoint references missing block {ep.get('instance_uid')}."))
                    continue
                if str(ep.get("port")) not in {str(port) for port in inst.get("port_names", [])}:
                    out.append(("Error", f"Wire endpoint {ep.get('instance_uid')}.{ep.get('port')} references a missing port."))
            for route in net.get("route_segments", []):
                if len(route.get("points", [])) < 2 and not route.get("auto", True):
                    out.append(("Error", f"{net.get('id')} route {route.get('wire_id', '')} has no drawable path."))
                for role in ["source", "target"]:
                    ep = route.get(role, {})
                    key = (str(ep.get("instance_uid", "")), str(ep.get("port", "")))
                    if key not in endpoint_keys:
                        out.append(("Error", f"{net.get('id')} route {route.get('wire_id', '')} has a dangling {role} endpoint {key[0]}.{key[1]}."))
                crossed_uid = self.route_crosses_block(route)
                if crossed_uid:
                    out.append(("Warning", f"{net.get('id')} route {route.get('wire_id', '')} crosses through block {crossed_uid}."))
        for inst in cell.get("instances", []):
            uid = str(inst.get("uid", ""))
            for port in inst.get("port_names", []):
                key = (uid, str(port))
                if key not in connected_port_keys:
                    out.append(("Error", f"Port {uid}.{port} is dangling; add a wire, pin, or label."))
        pin_names = [pin.get("name") for pin in cell.get("pins", [])]
        pins = {name for name in pin_names if name}
        if any(not name for name in pin_names):
            out.append(("Error", "Pin names may not be empty."))
        if len(pins) != len(pin_names):
            out.append(("Error", "Pin names must be unique."))
        orders = [pin.get("order") for pin in cell.get("pins", [])]
        if sorted(orders) != list(range(1, len(orders) + 1)):
            out.append(("Error", "Pin order must be defined and contiguous."))
        valid_net_ids_set = {net.get("id") for net in cell.get("nets", [])}
        for item in cell.get("pins", []):
            if not item.get("name"):
                out.append(("Error", "Pin name may not be empty."))
            net = next((n for n in cell.get("nets", []) if n.get("id") == item.get("net_id")), None)
            if not net:
                out.append(("Error", f"Pin {item.get('name')} is attached to an invalid net."))
                continue
            key = (str(item.get("instance_uid", "")), str(item.get("port", "")))
            if key not in {(str(ep.get("instance_uid", "")), str(ep.get("port", ""))) for ep in net.get("endpoints", [])}:
                out.append(("Error", f"Pin {item.get('name')} is attached to dangling endpoint {key[0]}.{key[1]}."))
        for item in cell.get("labels", []):
            if not item.get("name"):
                out.append(("Error", "Label name may not be empty."))
            if item.get("net_id") not in valid_net_ids_set:
                out.append(("Error", f"Label {item.get('name')} is attached to an invalid net."))
        p_uids_with_labels = {
            pin.get("instance_uid")
            for pin in cell.get("pins", [])
            if pin.get("instance_uid")
        } & {
            inst.get("uid")
            for inst in cell.get("instances", [])
            if inst.get("type_name") == "P"
        }
        for inst in cell.get("instances", []):
            if inst.get("type_name") == "P":
                inst_uid = inst.get("uid")
                for net in cell.get("nets", []):
                    if any(ep.get("instance_uid") == inst_uid for ep in net.get("endpoints", [])):
                        if any(label.get("net_id") == net.get("id") for label in cell.get("labels", [])):
                            p_uids_with_labels.add(inst_uid)
                            break
        for port in cell.get("simulation", {}).get("input_ports", []) + cell.get("simulation", {}).get("output_ports", []):
            if port not in pins and port not in p_uids_with_labels:
                out.append(("Error", f"Simulation port {port} is not an exported pin."))
        sim = cell.get("simulation", {})
        if float(sim.get("z0", 0) or 0) <= 0:
            out.append(("Error", "z0 must be positive."))
        if float(sim.get("freq_start", 0) or 0) >= float(sim.get("freq_stop", 0) or 0):
            out.append(("Error", "Frequency stop must be greater than frequency start."))
        if int(sim.get("freq_points", 0) or 0) <= 0:
            out.append(("Error", "Frequency points must be positive."))
        if sim.get("mode") == "x":
            x = sim.get("x", {})
            for label, port in [("X input", x.get("input_port")), ("X output", x.get("output_port"))]:
                if not port or port not in pins:
                    out.append(("Error", f"{label} port must be one of the current cell pins."))
            pump_ports = [str(port) for port in as_list(x.get("pump_ports", x.get("pump_port", ""))) if str(port)]
            if not pump_ports:
                out.append(("Error", "At least one X pump port must be selected."))
            for port in pump_ports:
                if port not in pins:
                    out.append(("Error", f"Pump port {port} must be one of the current cell pins."))
            dc_ports = [str(port) for port in as_list(x.get("dc_ports", x.get("dc_port", ""))) if str(port)]
            dc_currents = [str(current) for current in as_list(x.get("dc_currents", x.get("dc_current", ""))) if str(current)]
            if len(dc_ports) != len(dc_currents):
                out.append(("Error", "X DC ports and X DC currents must have the same count."))
            for port in dc_ports:
                if port not in pins:
                    out.append(("Error", f"DC port {port} must be one of the current cell pins."))
        if cell.get("type") == "schematic" and not cell.get("instances"):
            out.append(("Warning", "Empty schematic."))
        return out

    def validate_current_cell(self) -> None:
        cell = self.active_cell()
        if not cell:
            return
        issues = self.validate_cell(cell)
        if not issues:
            self.add_message("Info", f'{cell.get("name")} passed validation.')
            self.popup("Validation", "No validation issues found.")
            return
        for severity, message in issues:
            self.add_message(severity, message)
        errors = [message for severity, message in issues if severity == "Error"]
        title = "Validation failed" if errors else "Validation warnings"
        self.popup(title, "\n".join(message for _severity, message in issues), "warning")
        self.canvas.draw()

    def export_pipeline_cell(self, cell: dict[str, Any], *, for_simulation: bool = False) -> dict[str, Any]:
        if cell.get("type") == "generated_hb":
            return materialize_generated_hb_cell(cell)
        if cell.get("type") != "matrix" and cell.get("generated_from") == "julia_direct_s" and not cell.get("instances"):
            coerced = self.coerce_direct_s_matrix_cell(cell)
            if coerced.get("type") == "matrix":
                return self.export_pipeline_cell(self.import_pipeline_cell(coerced), for_simulation=for_simulation)
        if cell.get("type") == "matrix":
            matrix = cell.get("matrix", {})
            ports = [str(p) for p in matrix.get("port_names", [])]
            return {
                "name": cell.get("name"),
                "type": "matrix",
                "port_count": len(ports),
                "port_names": ports,
                "matrix_type": matrix.get("matrix_type", "ABCD"),
                "matrix_values": matrix.get("values", ""),
                "matrix_definitions": matrix.get("definitions", ""),
                "variables": [{"name": v.get("name"), "default": v.get("default") or v.get("value")} for v in cell.get("variables", [])],
                "simulation": {"z0": float(cell.get("simulation", {}).get("z0", 50))},
                "symbol": cell.get("symbol") or default_symbol(ports),
                "symbol_port_layout": cell.get("symbol_port_layout") or (cell.get("symbol") or default_symbol(ports)).get("port_layout", []),
                "gui": cell.get("gui", {}),
                "reference": bool(cell.get("reference", False)),
            }
        wires: list[dict[str, Any]] = []
        for net in cell.get("nets", []):
            endpoints = net.get("endpoints", [])
            if len(endpoints) < 2:
                continue
            routes = net.get("route_segments", [])
            if routes:
                route_items = [(route.get("source", endpoints[0]), route.get("target", endpoints[-1]), route.get("wire_id", f"{net.get('id')}_{idx}")) for idx, route in enumerate(routes, start=1)]
            else:
                hub = endpoints[0]
                route_items = [(hub, ep, f"{net.get('id')}_{idx}") for idx, ep in enumerate(endpoints[1:], start=1)]
            for source, target, wire_id in route_items:
                wires.append(
                    {
                        "source_instance_uid": source.get("instance_uid"),
                        "source_port": str(source.get("port")),
                        "target_instance_uid": target.get("instance_uid"),
                        "target_port": str(target.get("port")),
                        "name": net.get("name", ""),
                        "gui_wire_id": wire_id,
                    }
                )
        raw_pins = cell.get("pins", [])
        ordered_export_pins = [
            pin
            for _idx, pin in sorted(
                enumerate(raw_pins),
                key=lambda item: (int(item[1].get("order", 0) or 0), item[0]),
            )
        ]
        pins = []
        for pin in ordered_export_pins:
            net = next((n for n in cell.get("nets", []) if n.get("id") == pin.get("net_id")), None)
            ep = self.export_endpoint_for_tag(cell, pin, net)
            pins.append({"name": pin.get("name"), "instance_uid": ep.get("instance_uid", ""), "port": str(ep.get("port", ""))})
        labels = []
        for label in cell.get("labels", []):
            net = next((n for n in cell.get("nets", []) if n.get("id") == label.get("net_id")), None)
            ep = (net.get("endpoints") or [None])[0] if net else None
            entry: dict[str, Any] = {"name": label.get("name"), "position": label.get("position", [0, 0])}
            if ep:
                entry["net_source_uid"] = ep.get("instance_uid", "")
                entry["net_source_port"] = str(ep.get("port", ""))
            labels.append(entry)
        sim = cell.get("simulation", {})
        raw_sim_settings = dict(sim.get("raw_settings", {}))
        gui_meta = json.loads(json.dumps(cell.get("gui", {})))
        gui_meta["wire_routes"] = [
            {"net_id": net.get("id"), **route}
            for net in cell.get("nets", [])
            for route in net.get("route_segments", [])
        ]
        base = {
            "name": cell.get("name"),
            "type": cell.get("type"),
            "instances": [],
            "wires": wires,
            "pins": pins,
            "labels": labels,
            "variables": self.inferred_cell_variables(cell),
            "simulation": {
                "z0": float(cell.get("simulation", {}).get("z0", 50)),
                "hb": json.loads(json.dumps(cell.get("simulation", {}).get("hb") or {})),
            },
            "simulation_input_ports": sim.get("input_ports", []),
            "simulation_output_ports": sim.get("output_ports", []),
            "simulation_freq_start": float(sim.get("freq_start", 2.0)),
            "simulation_freq_stop": float(sim.get("freq_stop", 20.0)),
            "simulation_freq_points": int(sim.get("freq_points", 200)),
            "simulation_sweep_type": sim.get("sweep_type", "linear"),
            "simulation_figure_title": sim.get("figure_title") or cell.get("name"),
            "gui": gui_meta,
        }
        for inst in cell.get("instances", []):
            type_name = f"{inst.get('source_project')}/{inst.get('type_name')}" if inst.get("source_project") else inst.get("type_name")
            rewrote_to_override_cell = bool(for_simulation and inst.get("internal_parameter_overrides"))
            if rewrote_to_override_cell:
                type_name = self.override_instance_type_name(inst, parent_name=str(cell.get("name", "")))
            inst_cell_def = self.cell_definition_for_instance(inst)
            public_vars = self.public_variable_items(inst_cell_def)
            public_names = {str(var.get("name", "")).strip() for var in public_vars}
            if inst_cell_def:
                keep_names = {
                    name
                    for name in set(inst.get("parameters", {}) or {})
                    if (
                        name in public_names
                        or name in RESERVED_VARIABLE_NAMES
                        or self.is_uid_parameter(inst, name)
                    )
                }
                export_params = {
                    name: value
                    for name, value in (inst.get("parameters", {}) or {}).items()
                    if name in keep_names
                }
                export_order = [name for name in (inst.get("parameter_order", []) or []) if name in keep_names]
                export_kinds = {
                    name: value
                    for name, value in (inst.get("parameter_kinds", {}) or {}).items()
                    if name in keep_names
                }
            else:
                export_params = dict(inst.get("parameters", {}) or {})
                export_order = list(inst.get("parameter_order", []) or [])
                export_kinds = dict(inst.get("parameter_kinds", {}) or {})
            for var in public_vars:
                name = str(var.get("name", "")).strip()
                if not name:
                    continue
                if name not in export_order:
                    export_order.append(name)
                export_kinds.setdefault(name, "positional")
            inst_export = {
                    "type_name": type_name,
                    "uid": inst.get("uid"),
                    "parameters": export_params,
                    "parameter_order": export_order,
                    "parameter_kinds": export_kinds,
                    "position": inst.get("position", [0, 0]),
                    "port_count": len(inst.get("port_names", [])),
                    "port_names": [str(p) for p in inst.get("port_names", [])],
                    "rotation_degrees": inst.get("rotation_degrees", 0),
                    "repeat_count": max(1, repeat_count_value(inst.get("repeat_count", 1))),
                    "repeat_connections": [
                        {"out": str(connection.get("out", "")), "in": str(connection.get("in", ""))}
                        for connection in inst.get("repeat_connections", [])
                        if connection.get("out") and connection.get("in")
                    ],
                    "symbol_port_layout": [
                        {"port": p.get("port"), "side": p.get("side"), "position": p.get("position")}
                        for p in inst.get("symbol", {}).get("port_layout", [])
                    ],
                    "symbol": inst.get("symbol", default_symbol(inst.get("port_names", []))),
                }
            if inst.get("internal_parameter_overrides") and not rewrote_to_override_cell:
                inst_export["internal_parameter_overrides"] = dict(inst.get("internal_parameter_overrides", {}))
            if inst.get("hb"):
                inst_export["hb"] = json.loads(json.dumps(inst["hb"]))
            base["instances"].append(inst_export)
        if cell.get("skip_hb_top_block_check"):
            base["skip_hb_top_block_check"] = True
        base.update({key: value for key, value in raw_sim_settings.items() if key in SIMULATION_RAW_KEYS})
        base["reference"] = bool(cell.get("reference", False))
        if cell.get("simulation_variables"):
            base["simulation_variables"] = cell.get("simulation_variables")
        if sim.get("mode") == "x":
            x = sim.get("x", {})
            pump_frequencies = [float(v) for v in as_list(x.get("pump_frequencies", x.get("pump_frequency", 0))) if str(v) != ""]
            pump_currents = [str(v) for v in as_list(x.get("pump_currents", x.get("pump_current", ""))) if str(v)]
            pump_ports = [str(v) for v in as_list(x.get("pump_ports", x.get("pump_port", ""))) if str(v)]
            dc_ports = [str(v) for v in as_list(x.get("dc_ports", x.get("dc_port", ""))) if str(v)]
            dc_currents = [str(v) for v in as_list(x.get("dc_currents", x.get("dc_current", ""))) if str(v)]
            modulation_harmonics = [int(v) for v in as_list(x.get("modulation_harmonics", [10]) or [10])]
            pump_harmonics = [int(v) for v in as_list(x.get("pump_harmonics", [20]) or [20])]
            base.update(
                {
                    "x-params": True,
                    "x_input_ports": [x.get("input_port")] if x.get("input_port") else [],
                    "x_output_ports": [x.get("output_port")] if x.get("output_port") else [],
                    "x_pump_ports": pump_ports,
                    "x_pump_frequencies": pump_frequencies or [7.12],
                    "x_pump_currents": pump_currents,
                    "x_dc_ports": dc_ports,
                    "x_dc_currents": dc_currents,
                    "x_modulation_harmonics": modulation_harmonics,
                    "x_pump_harmonics": pump_harmonics,
                    "x_threewave_mixing": bool(x.get("threewave_mixing", True)),
                    "x_fourwave_mixing": bool(x.get("fourwave_mixing", True)),
                }
            )
        if cell.get("generated_source"):
            base["generated_source"] = cell.get("generated_source", "")
            base["generated_from"] = cell.get("generated_from", "julia_reverse_import")
            base["generated_language"] = cell.get("generated_language", "julia")
        return base

    def override_instance_type_name(self, inst: dict[str, Any], parent_name: str = "") -> str:
        uid = clean_name(str(inst.get("uid", "inst"))) or "inst"
        raw_type = str(inst.get("type_name", "cell"))
        source_project = str(inst.get("source_project", ""))
        if not source_project and "/" in raw_type:
            source_project, raw_type = raw_type.split("/", 1)
        base = clean_name(raw_type) or "cell"
        parent = clean_name(parent_name)
        suffix = f"{parent}_{uid}" if parent else uid
        override_name = f"{base}__override_{suffix}"
        if source_project:
            return f"{source_project}/{override_name}"
        return override_name

    def apply_internal_overrides_to_cell(self, cell_data: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
        out = json.loads(json.dumps(cell_data))
        if not overrides:
            return out
        for key, value in overrides.items():
            parts = [part for part in str(key).split(".") if part]
            if len(parts) < 2:
                continue
            uid = parts[0]
            param_path = parts[1:]
            child_inst = next((item for item in out.get("instances", []) if str(item.get("uid", "")) == uid), None)
            if not child_inst:
                continue
            if len(param_path) > 1:
                nested_key = ".".join(param_path)
                child_inst.setdefault("internal_parameter_overrides", {})[nested_key] = str(value)
                continue
            param = param_path[0]
            child_inst.setdefault("parameters", {})[param] = str(value)
            child_inst.setdefault("raw_parameters", {})[param] = str(value)
            child_inst.setdefault("resolved_parameters", {})[param] = str(value)
            order = child_inst.setdefault("parameter_order", [])
            if param not in order:
                order.append(param)
            child_inst.setdefault("parameter_kinds", {}).setdefault(param, "positional")
        return out

    def serialize_project_cell(self, cell: dict[str, Any]) -> dict[str, Any]:
        if cell.get("type") != "generated_hb":
            return self.export_pipeline_cell(cell)
        return {
            "name": cell.get("name"),
            "type": "generated_hb",
            "description": cell.get("description", ""),
            "variables": cell.get("variables", []),
            "pins": cell.get("pins", []),
            "labels": [],
            "instances": [],
            "nets": [],
            "generated_language": cell.get("generated_language", "julia"),
            "generator_kind": cell.get("generator_kind", "josephsoncircuits_circuit"),
            "generated_source": cell.get("generated_source", ""),
            "generated_summary": cell.get("generated_summary", {}),
            "simulation": cell.get("simulation", {}),
            "symbol": cell.get("symbol") or default_symbol(self.cell_port_names(cell)),
            "symbol_port_layout": cell.get("symbol_port_layout") or (cell.get("symbol") or default_symbol(self.cell_port_names(cell))).get("port_layout", []),
            "gui": cell.get("gui", {}),
            "reference": bool(cell.get("reference", False)),
        }

    def export_endpoint_for_tag(self, cell: dict[str, Any], tag: dict[str, Any], net: dict[str, Any] | None) -> dict[str, str]:
        endpoints = (net or {}).get("endpoints", [])
        tag_uid = tag.get("instance_uid")
        tag_port = str(tag.get("port", ""))

        try:
            tag_pos = tag.get("position", [0, 0])
            point = QPointF(float(tag_pos[0]), float(tag_pos[1]))
        except (TypeError, ValueError, IndexError):
            point = QPointF(0, 0)

        p_endpoint = self.closest_endpoint_on_net_by_type(cell, net or {}, point, {"P"})
        if p_endpoint:
            return p_endpoint

        if tag_uid and tag_port:
            for endpoint in endpoints:
                if endpoint.get("instance_uid") == tag_uid and str(endpoint.get("port", "")) == tag_port:
                    return {"instance_uid": tag_uid, "port": tag_port}

        best_endpoint: dict[str, str] = {}
        best_distance = float("inf")
        for endpoint in endpoints:
            inst = next((item for item in cell.get("instances", []) if item.get("uid") == endpoint.get("instance_uid")), None)
            if not inst:
                continue
            port = str(endpoint.get("port", ""))
            port_pos = port_point(inst, port)
            distance = (port_pos.x() - point.x()) ** 2 + (port_pos.y() - point.y()) ** 2
            if distance < best_distance:
                best_endpoint = {"instance_uid": endpoint.get("instance_uid", ""), "port": port}
                best_distance = distance
        if best_endpoint:
            return best_endpoint
        if endpoints:
            return {"instance_uid": endpoints[0].get("instance_uid", ""), "port": str(endpoints[0].get("port", ""))}
        return {"instance_uid": "", "port": ""}

    def coerce_direct_s_matrix_cell(self, raw: dict[str, Any]) -> dict[str, Any]:
        if raw.get("type") == "matrix":
            return raw
        if raw.get("generated_from") != "julia_direct_s":
            return raw
        if raw.get("instances"):
            return raw

        out = json.loads(json.dumps(raw))
        name = clean_name(str(out.get("name", "direct_s_matrix"))) or "direct_s_matrix"
        ports = [str(port) for port in out.get("port_names", []) if str(port)]
        if not ports:
            ports = [str(pin.get("name")) for pin in out.get("pins", []) if str(pin.get("name", ""))]
        if not ports:
            port_count = int(out.get("port_count", 0) or 0)
            ports = [f"p{idx}" for idx in range(1, max(port_count, 2) + 1)]

        variables = out.get("variables", []) or []
        param_names = [
            clean_name(str(var.get("name", "")))
            for var in variables
            if clean_name(str(var.get("name", ""))) and var.get("kind") != "uid"
        ]
        source = str(out.get("matrix_definitions") or out.get("generated_source") or "").strip()
        if re.match(r"^\s*function\b", source):
            definitions = source
        else:
            keyword_defaults = []
            for var in variables:
                var_name = clean_name(str(var.get("name", "")))
                if not var_name or var.get("kind") == "uid":
                    continue
                default = var.get("default", var.get("value", "1.0"))
                keyword_defaults.append(f"{var_name} = {default}")
            signature = f"function {name}(w"
            if keyword_defaults:
                signature += "; " + ", ".join(keyword_defaults)
            signature += ")"
            definitions = f"{signature}\n{source}\nend"

        keyword_args = ", ".join(f"{param}={param}" for param in param_names)
        call_suffix = f"; {keyword_args}" if keyword_args else ""
        out["name"] = name
        out["type"] = "matrix"
        out["port_count"] = len(ports)
        out["port_names"] = ports
        out["matrix_type"] = out.get("matrix_type", "S")
        out["matrix_definitions"] = definitions
        out["matrix_values"] = out.get("matrix_values") or f"{name}([w]{call_suffix})[:, :, 1]"
        out["pins"] = []
        out["labels"] = []
        out["instances"] = []
        out["wires"] = []
        out["symbol"] = out.get("symbol") or default_symbol(ports)
        out["symbol_port_layout"] = out.get("symbol_port_layout") or out["symbol"].get("port_layout", [])
        return out

    def import_pipeline_cell(self, raw: dict[str, Any]) -> dict[str, Any]:
        raw = self.coerce_direct_s_matrix_cell(raw)
        cell = blank_cell(raw.get("name", "imported_cell"), raw.get("type", "schematic"))
        cell["dirty"] = False
        # Migrate z0: prefer simulation.z0, fall back to legacy top-level z0
        _raw_sim = raw.get("simulation") or {}
        cell.setdefault("simulation", {})["z0"] = _raw_sim.get("z0") or raw.get("z0", 50)
        if raw.get("generated_source"):
            cell["generated_source"] = raw.get("generated_source", "")
            cell["generated_from"] = raw.get("generated_from", "julia_reverse_import")
            cell["generated_language"] = raw.get("generated_language", "julia")
        if cell["type"] == "generated_hb":
            cell["description"] = raw.get("description", "")
            cell["variables"] = raw.get("variables", [])
            cell["pins"] = raw.get("pins", [])
            cell["labels"] = []
            cell["instances"] = []
            cell["nets"] = []
            cell["generated_language"] = raw.get("generated_language", "julia")
            cell["generator_kind"] = raw.get("generator_kind", "josephsoncircuits_circuit")
            cell["generated_source"] = raw.get("generated_source", "")
            cell["generated_summary"] = raw.get("generated_summary", {})
            _sim = dict(raw.get("simulation", cell.get("simulation", {})))
            if "z0" not in _sim:
                _sim["z0"] = raw.get("z0", 50)
            _raw_hb = _sim.get("hb") or {
                "top_block": truthy(raw.get("hb_top_block", True)),
                "pump_ports": as_list(raw.get("hb_pump_ports", [])),
                "pump_frequencies": as_list(raw.get("hb_pump_frequencies", [])),
                "pump_currents": as_list(raw.get("hb_pump_currents", [])),
                "dc_ports": as_list(raw.get("hb_dc_ports", [])),
                "dc_currents": as_list(raw.get("hb_dc_currents", [])),
                "modulation_harmonics": raw.get("hb_modulation_harmonics", [10]),
                "pump_harmonics": raw.get("hb_pump_harmonics", [20]),
                "threewave_mixing": truthy(raw.get("hb_threewave_mixing", True)),
                "fourwave_mixing": truthy(raw.get("hb_fourwave_mixing", True)),
            }
            _sim["hb"] = _raw_hb
            cell["simulation"] = _sim
            cell["symbol"] = raw.get("symbol") or default_symbol(self.cell_port_names(cell))
            cell["symbol_port_layout"] = raw.get("symbol_port_layout") or cell["symbol"].get("port_layout", [])
            cell["reference"] = bool(raw.get("reference", False))
            return cell
        if cell["type"] == "matrix":
            cell["matrix"] = {
                "port_names": [str(p) for p in raw.get("port_names", [])],
                "matrix_type": raw.get("matrix_type", "ABCD"),
                "values": raw.get("matrix_values", ""),
                "definitions": raw.get("matrix_definitions", ""),
            }
            ports = cell["matrix"]["port_names"]
            cell["variables"] = raw.get("variables", [])
            cell["symbol"] = raw.get("symbol") or default_symbol(ports)
            cell["symbol_port_layout"] = raw.get("symbol_port_layout") or cell["symbol"].get("port_layout", [])
        if cell["type"] == "schematic":
            cell["variables"] = []
        cell["instances"] = []
        for inst in raw.get("instances", []):
            copy = json.loads(json.dumps(inst))
            ports = [str(p) for p in copy.get("port_names", [])]
            copy.setdefault("source", "local")
            # Parse type_name to extract source_project prefix if present (e.g., "example_JPA/JPA" -> source_project="example_JPA", type_name="JPA")
            type_name = copy.get("type_name", "")
            if "/" in type_name:
                source_proj, comp = type_name.split("/", 1)
                copy["source_project"] = source_proj
                copy["type_name"] = comp
                copy["source"] = f"imported:{source_proj}"
            copy["repeat_count"] = max(1, repeat_count_value(copy.get("repeat_count", 1)))
            copy["repeat_connections"] = [
                {"out": str(connection.get("out", "")), "in": str(connection.get("in", ""))}
                for connection in copy.get("repeat_connections", [])
                if connection.get("out") and connection.get("in")
            ]
            copy["symbol"] = copy.get("symbol") or default_symbol(ports)
            if not copy["symbol"].get("port_layout"):
                copy["symbol"]["port_layout"] = copy.get("symbol_port_layout") or default_symbol(ports).get("port_layout", [])
            repair_common_port_symbol(copy)
            copy.setdefault("simulation", {})
            cell["instances"].append(copy)
        cell["nets"] = []
        route_lookup = {
            str(route.get("wire_id")): route
            for route in raw.get("gui", {}).get("wire_routes", [])
            if route.get("wire_id")
        }
        parent: dict[tuple[str, str], tuple[str, str]] = {}

        def key_for_endpoint(endpoint: dict[str, Any]) -> tuple[str, str]:
            return (str(endpoint.get("instance_uid", "")), str(endpoint.get("port", "")))

        def find_key(key: tuple[str, str]) -> tuple[str, str]:
            parent.setdefault(key, key)
            if parent[key] != key:
                parent[key] = find_key(parent[key])
            return parent[key]

        def union_keys(a: tuple[str, str], b: tuple[str, str]) -> None:
            parent[find_key(b)] = find_key(a)

        route_records: list[dict[str, Any]] = []
        named_wire_roots: dict[str, tuple[str, str]] = {}

        for i, wire in enumerate(raw.get("wires", []), start=1):
            source = {"instance_uid": wire.get("source_instance_uid"), "port": str(wire.get("source_port"))}
            target = {"instance_uid": wire.get("target_instance_uid"), "port": str(wire.get("target_port"))}
            wire_id = str(wire.get("gui_wire_id") or f"wire_{i}")
            route = route_lookup.get(wire_id, {})
            source_key = key_for_endpoint(source)
            target_key = key_for_endpoint(target)
            union_keys(source_key, target_key)
            wire_name = str(wire.get("name", "")).strip()
            if wire_name:
                if wire_name in named_wire_roots:
                    union_keys(source_key, named_wire_roots[wire_name])
                else:
                    named_wire_roots[wire_name] = source_key
            route_records.append(
                {
                    "wire_id": wire_id,
                    "source": source,
                    "target": target,
                    "points": route.get("points", []),
                    "auto": bool(route.get("auto", len(route.get("points", [])) <= 6)),
                }
            )

        grouped_endpoints: dict[tuple[str, str], dict[tuple[str, str], dict[str, str]]] = {}
        grouped_routes: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for route in route_records:
            root = find_key(key_for_endpoint(route["source"]))
            grouped_routes.setdefault(root, []).append(route)
            grouped_endpoints.setdefault(root, {})[key_for_endpoint(route["source"])] = route["source"]
            grouped_endpoints.setdefault(root, {})[key_for_endpoint(route["target"])] = route["target"]

        for idx, root in enumerate(grouped_routes, start=1):
            cell["nets"].append(
                {
                    "id": f"net_{idx}",
                    "endpoints": list(grouped_endpoints.get(root, {}).values()),
                    "pins": [],
                    "labels": [],
                    "route_segments": grouped_routes[root],
                }
            )
        for i, pin in enumerate(raw.get("pins", []), start=1):
            endpoint = {"instance_uid": pin.get("instance_uid"), "port": str(pin.get("port"))}
            net = self.find_or_create_endpoint_net_for_cell(cell, endpoint)
            inst = next((item for item in cell["instances"] if item.get("uid") == pin.get("instance_uid")), None)
            point = port_point(inst, str(pin.get("port"))) if inst else QPointF(-260, -80 + i * 34)
            cell["pins"].append(
                {
                    "id": str(uuid.uuid4()),
                    "name": pin.get("name"),
                    "order": i,
                    "net_id": net["id"],
                    "instance_uid": endpoint["instance_uid"],
                    "port": endpoint["port"],
                    "position": [point.x() + 14, point.y() - 20],
                    "display_visible": True,
                }
            )
            net.setdefault("pins", []).append(cell["pins"][-1]["id"])
        for i, label in enumerate(raw.get("labels", []), start=1):
            anchor_uid = label.get("net_source_uid") or label.get("instance_uid")
            anchor_port = label.get("net_source_port") or label.get("port")
            if anchor_uid and anchor_port:
                endpoint = {"instance_uid": anchor_uid, "port": str(anchor_port)}
                net = self.find_or_create_endpoint_net_for_cell(cell, endpoint)
            elif cell.get("nets"):
                net = cell["nets"][0]
            else:
                net = {"id": f"net_{uuid.uuid4().hex[:8]}", "endpoints": [], "pins": [], "labels": [], "route_segments": []}
                cell.setdefault("nets", []).append(net)
            raw_position = label.get("position", [260, -80 + i * 34])
            if anchor_uid and anchor_port and raw_position in ([0, 0], [0.0, 0.0], None):
                inst = next((item for item in cell["instances"] if item.get("uid") == anchor_uid), None)
                point = port_point(inst, str(anchor_port)) if inst else QPointF(260, -80 + i * 34)
                raw_position = [point.x() + 14, point.y() - 20]
            cell["labels"].append(
                {
                    "id": str(uuid.uuid4()),
                    "name": label.get("name"),
                    "net_id": net["id"],
                    "position": raw_position,
                    "display_visible": True,
                }
            )
            net.setdefault("labels", []).append(cell["labels"][-1]["id"])
        sim = cell["simulation"]
        sim["raw_settings"] = {key: raw.get(key) for key in SIMULATION_RAW_KEYS if key in raw}
        cell["simulation_variables"] = raw.get("simulation_variables", [])
        sim["input_ports"] = as_list(raw.get("simulation_input_ports", []))
        sim["output_ports"] = as_list(raw.get("simulation_output_ports", []))
        sim["freq_start"] = raw.get("simulation_freq_start", 2.0)
        sim["freq_stop"] = raw.get("simulation_freq_stop", 20.0)
        sim["freq_points"] = raw.get("simulation_freq_points", 200)
        sim["sweep_type"] = raw.get("simulation_sweep_type", "linear")
        sim["figure_title"] = raw.get("simulation_figure_title", cell["name"])
        cell["skip_hb_top_block_check"] = truthy(raw.get("skip_hb_top_block_check", False))
        _raw_sim_hb = (raw.get("simulation") or {}).get("hb") or {}
        _cell_sim_hb = cell.setdefault("simulation", {}).setdefault("hb", {})
        _cell_sim_hb["top_block"] = truthy(_raw_sim_hb.get("top_block", raw.get("hb_top_block", False)))
        _cell_sim_hb["pump_ports"] = as_list(_raw_sim_hb.get("pump_ports", raw.get("hb_pump_ports", [])))
        _cell_sim_hb["pump_frequencies"] = as_list(_raw_sim_hb.get("pump_frequencies", raw.get("hb_pump_frequencies", [])))
        _cell_sim_hb["pump_currents"] = as_list(_raw_sim_hb.get("pump_currents", raw.get("hb_pump_currents", [])))
        _cell_sim_hb["dc_ports"] = as_list(_raw_sim_hb.get("dc_ports", raw.get("hb_dc_ports", [])))
        _cell_sim_hb["dc_currents"] = as_list(_raw_sim_hb.get("dc_currents", raw.get("hb_dc_currents", [])))
        _cell_sim_hb["modulation_harmonics"] = _raw_sim_hb.get("modulation_harmonics", raw.get("hb_modulation_harmonics", [10]))
        _cell_sim_hb["pump_harmonics"] = _raw_sim_hb.get("pump_harmonics", raw.get("hb_pump_harmonics", [20]))
        _cell_sim_hb["threewave_mixing"] = truthy(_raw_sim_hb.get("threewave_mixing", raw.get("hb_threewave_mixing", True)))
        _cell_sim_hb["fourwave_mixing"] = truthy(_raw_sim_hb.get("fourwave_mixing", raw.get("hb_fourwave_mixing", True)))
        if truthy(raw.get("x-params", raw.get("x_params", False))):
            sim["mode"] = "x"
        x = sim.setdefault("x", {})
        x["input_port"] = str(first_value(raw.get("x_input_ports", ""), ""))
        x["output_port"] = str(first_value(raw.get("x_output_ports", ""), ""))
        x["pump_ports"] = [str(value) for value in as_list(raw.get("x_pump_ports", [])) if str(value)]
        x["pump_port"] = str(first_value(x["pump_ports"], ""))
        x["pump_frequencies"] = as_list(raw.get("x_pump_frequencies", [7.12])) or [7.12]
        x["pump_frequency"] = first_value(x["pump_frequencies"], 7.12)
        x["pump_currents"] = [str(value) for value in as_list(raw.get("x_pump_currents", [])) if str(value)]
        x["pump_current"] = str(first_value(x["pump_currents"], ""))
        x["dc_ports"] = [str(value) for value in as_list(raw.get("x_dc_ports", [])) if str(value)]
        x["dc_port"] = str(first_value(x["dc_ports"], ""))
        x["dc_currents"] = [str(value) for value in as_list(raw.get("x_dc_currents", [])) if str(value)]
        x["dc_current"] = str(first_value(x["dc_currents"], ""))
        x["modulation_harmonics"] = [int(value) for value in as_list(raw.get("x_modulation_harmonics", [10]) or [10])]
        x["pump_harmonics"] = [int(value) for value in as_list(raw.get("x_pump_harmonics", [20]) or [20])]
        x["threewave_mixing"] = truthy(raw.get("x_threewave_mixing", True))
        x["fourwave_mixing"] = truthy(raw.get("x_fourwave_mixing", True))
        cell["reference"] = bool(raw.get("reference", False))
        return cell

    def find_or_create_endpoint_net_for_cell(self, cell: dict[str, Any], endpoint: dict[str, str]) -> dict[str, Any]:
        for net in cell.setdefault("nets", []):
            if any(ep.get("instance_uid") == endpoint.get("instance_uid") and str(ep.get("port")) == str(endpoint.get("port")) for ep in net.get("endpoints", [])):
                return net
        net = {"id": f"net_{uuid.uuid4().hex[:8]}", "endpoints": [endpoint], "pins": [], "labels": [], "route_segments": []}
        cell["nets"].append(net)
        return net

    def unique_import_alias(self, base: str) -> str:
        alias = base or "imported_project"
        i = 2
        while any(item.get("alias") == alias for item in self.project.get("imports", [])):
            alias = f"{base}_{i}"
            i += 1
        return alias

    def run_simulation(self) -> None:
        cell = self.active_cell()
        if not cell:
            return
        issues = self.validate_cell(cell)
        errors = [message for severity, message in issues if severity == "Error"]
        for severity, message in issues:
            self.add_message(severity, message)
        if errors:
            self.popup("Invalid simulation setup", "\n".join(errors), "warning")
            return
        PIPELINE_DATA_DIR.mkdir(parents=True, exist_ok=True)
        temp = Path(tempfile.mkdtemp(prefix="guiV2_pipeline_", dir=str(PIPELINE_DATA_DIR)))
        items = self.export_all_local_cells()
        item_by_name: dict[str, dict] = {item.get("name", ""): item for item in items}
        # Include readOnly cells so skip_hb_top_block_check can patch imported cells too
        if self.project:
            for c in self.project.get("cells", {}).values():
                if c.get("readOnly") and c.get("name") not in item_by_name:
                    exported = self.export_pipeline_cell(c, for_simulation=True)
                    item_by_name[exported.get("name", "")] = exported
        # Apply skip_hb_top_block_check: recursively disable hb_top_block in all imported cells
        if self.project:
            active_cell_for_exp = self.active_cell()
            if active_cell_for_exp and active_cell_for_exp.get("skip_hb_top_block_check"):
                def patch_imported_cells_recursive(cell_name: str) -> None:
                    item = item_by_name.get(cell_name)
                    if item is None:
                        return
                    item.setdefault("simulation", {}).setdefault("hb", {})["top_block"] = False
                    # Recursively patch all instances in this cell
                    for inst in item.get("instances", []):
                        inst_type = inst.get("type_name", "")
                        patch_imported_cells_recursive(inst_type)

                # Start patching from all instances in the active cell
                for inst in active_cell_for_exp.get("instances", []):
                    patch_imported_cells_recursive(inst.get("type_name", ""))
        self.write_simulation_dependency_cells(temp)
        self.write_internal_override_cells(temp)
        for item in item_by_name.values():
            (temp / f"{clean_name(item.get('name', 'cell'))}.json").write_text(json.dumps(item, indent=2), encoding="utf-8")
        target = temp / f"{clean_name(cell.get('name', 'cell'))}.json"
        rel = target.relative_to(DATA_DIR)
        run_id = temp.name
        process = QProcess(self)
        process.setWorkingDirectory(str(LOGIC_DIR))
        process.readyReadStandardOutput.connect(
            lambda p=process, rid=run_id: self.append_simulation_output(rid, p, stderr=False)
        )
        process.readyReadStandardError.connect(
            lambda p=process, rid=run_id: self.append_simulation_output(rid, p, stderr=True)
        )
        process.finished.connect(
            lambda code, status, rid=run_id, t=target, p=process: self.simulation_finished(code, t, rid, p)
        )
        self.simulation_processes[run_id] = process
        self.simulation_targets[run_id] = target
        self.simulation_cells[run_id] = str(cell.get("name", "cell"))
        self.process = process
        self.add_message("Info", f"Running pipeline {run_id} for {cell.get('name')}...")
        process.start("bash", [str(LOGIC_DIR / "run_pipeline.sh"), str(rel)])

    def write_simulation_dependency_cells(self, temp: Path) -> None:
        if not self.project:
            return
        for alias, cells in self.project.get("importedCells", {}).items():
            alias_name = clean_name(str(alias))
            if not alias_name:
                continue
            alias_dir = temp / alias_name
            alias_dir.mkdir(parents=True, exist_ok=True)
            for cell in cells:
                name = clean_name(str(cell.get("name", "cell"))) or "cell"
                exported = self.export_pipeline_cell(cell, for_simulation=True)
                (alias_dir / f"{name}.json").write_text(json.dumps(exported, indent=2), encoding="utf-8")

    def write_internal_override_cells(self, temp: Path) -> None:
        if not self.project:
            return

        parent_cells: list[tuple[dict[str, Any], str]] = [
            (cell, "")
            for cell in self.project.get("cells", {}).values()
        ]
        for alias, cells in self.project.get("importedCells", {}).items():
            alias_name = clean_name(str(alias))
            parent_cells.extend((cell, alias_name) for cell in cells if alias_name)

        for parent, parent_alias in parent_cells:
            for inst in parent.get("instances", []) or []:
                overrides = inst.get("internal_parameter_overrides", {}) or {}
                if not overrides:
                    continue
                cell_def = self.cell_definition_for_instance(inst)
                if not cell_def:
                    continue
                exported = self.export_pipeline_cell(cell_def, for_simulation=True)
                exported = self.apply_internal_overrides_to_cell(exported, overrides)
                override_type = self.override_instance_type_name(inst, parent_name=str(parent.get("name", "")))
                rel = Path(str(override_type).replace("\\", "/"))
                if parent_alias and len(rel.parts) == 1:
                    rel = Path(parent_alias) / rel
                rel = rel.with_suffix(".json")
                out_path = temp / rel
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(json.dumps(exported, indent=2), encoding="utf-8")

    def append_simulation_output(self, run_id: str, process: QProcess, stderr: bool = False) -> None:
        payload = process.readAllStandardError() if stderr else process.readAllStandardOutput()
        text = bytes(payload).decode(errors="replace")
        if not text:
            return
        prefix = f"[{run_id}] "
        self.messages.appendPlainText("".join(prefix + line if line else line for line in text.splitlines(True)))

    def open_test_suite(self) -> None:
        dlg = TestSuiteDialog(self)
        dlg.exec()

    def stop_simulation(self) -> None:
        running = [
            (run_id, process)
            for run_id, process in self.simulation_processes.items()
            if process.state() != QProcess.ProcessState.NotRunning
        ]
        if not running:
            self.add_message("Info", "No simulations are running.")
            return
        for run_id, process in running:
            process.kill()
            self.add_message("Warning", f"Simulation {run_id} stopped.")

    def simulation_finished(self, code: int, target: Path, run_id: str = "", process: QProcess | None = None) -> None:
        if run_id:
            self.simulation_processes.pop(run_id, None)
            self.simulation_targets.pop(run_id, None)
            cell_name = self.simulation_cells.pop(run_id, target.stem)
        else:
            cell_name = target.stem
        if process is not None:
            process.deleteLater()
        self.process = next(
            (p for p in self.simulation_processes.values() if p.state() != QProcess.ProcessState.NotRunning),
            None,
        )
        if code == 0:
            label = f"{run_id} for {cell_name}" if run_id else cell_name
            self.add_message("Info", f"Simulation completed: {label}.")
            self.collect_results(target)
        else:
            label = f"{run_id} for {cell_name}" if run_id else cell_name
            self.add_message("Error", f"Simulation failed: {label} exited with code {code}.")

    def export_all_local_cells(self) -> list[dict[str, Any]]:
        if not self.project:
            return []
        return [self.export_pipeline_cell(cell, for_simulation=True) for cell in self.project.get("cells", {}).values() if not cell.get("readOnly")]

    def collect_results(self, target: Path) -> None:
        if not self.project:
            return
        out_root = LOGIC_DIR / "outputs" / target.parent.name
        if not out_root.exists():
            return

        cache_dir = out_root / "cache"

        def load_manifest(name: str) -> dict[str, Any]:
            path = cache_dir / name
            if not path.exists():
                return {}
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                return {}

        x_manifest = load_manifest("x_cache_manifest.json")
        base_manifest = load_manifest("cache_manifest.json")
        seen_paths: set[Path] = set()
        current_cells = {
            path.stem
            for path in (out_root / "specialized").glob("*.json")
            if not path.name.endswith("_json_port_order.json")
        }

        result_paths: list[tuple[str, Path, str, str]] = []
        for cell_name, entry in sorted(x_manifest.items()):
            if current_cells and cell_name not in current_cells:
                continue
            csv_path = Path(str(entry.get("csv", "")))
            if csv_path.is_file() and csv_path not in seen_paths:
                result_paths.append((f"{cell_name} X parameters", csv_path, "x", cell_name))
                seen_paths.add(csv_path)

        for cell_name, entry in sorted(base_manifest.items()):
            if current_cells and cell_name not in current_cells:
                continue
            csv_path = Path(str(entry.get("csv", "")))
            if csv_path.is_file() and csv_path not in seen_paths:
                result_paths.append((cell_name, csv_path, "s", cell_name))
                seen_paths.add(csv_path)

        if not result_paths:
            for csv_path in sorted(cache_dir.glob("*.csv")):
                if "_x_" in csv_path.name or csv_path.name.endswith("_nodeflux.csv"):
                    continue
                result_paths.append((csv_path.stem, csv_path, "s", csv_path.stem))

        cell_data = next((c for c in self.project.get("cells", {}).values() if c.get("name") == target.stem), None)
        has_reference = bool((cell_data or {}).get("reference", False))

        run_id = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.project["results"] = [
            result
            for result in self.project.get("results", [])
            if result.get("cell") != target.stem
        ]
        for display_name, csv_path, result_kind, manifest_key in result_paths:
            try:
                text = self.normalize_csv(csv_path.read_text(encoding="utf-8"))
                rows = max(0, sum(1 for _ in csv.reader(text.splitlines())) - 1)
                ref_status: str | None = None
                ref_details: dict[str, Any] = {}
                if has_reference:
                    ref_csv = self._find_reference_csv(target.stem, manifest_key, result_kind)
                    if ref_csv:
                        ref_details = self._compare_csvs(csv_path, ref_csv)
                        ref_status = str(ref_details.get("status", "fail"))
                    else:
                        ref_status = "no_ref"
                self.project.setdefault("results", []).append(
                    {
                        "name": display_name,
                        "cell": target.stem,
                        "rows": rows,
                        "csv": text,
                        "source_path": str(csv_path.relative_to(LOGIC_DIR)),
                        "absolute_path": str(csv_path),
                        "kind": result_kind,
                        "manifest_key": manifest_key,
                        "port_names": self.result_port_names(manifest_key),
                        "ref_status": ref_status,
                        "ref_details": ref_details,
                        "run_id": run_id,
                    }
                )
            except Exception:
                continue
        self.refresh_results()
        self.bottom_tabs.setCurrentWidget(self.results_panel)

    def normalize_csv(self, text: str) -> str:
        rows = list(csv.reader(text.splitlines()))
        if not rows:
            return text
        try:
            [float(value) for value in rows[0]]
        except ValueError:
            return text
        headers = ["frequency"] + [f"col_{i}" for i in range(1, len(rows[0]))]
        out = [",".join(headers)]
        out.extend(",".join(row) for row in rows)
        return "\n".join(out) + "\n"

    def result_port_names(self, cell_name: str) -> list[str]:
        if not self.project:
            return []
        candidates = [str(cell_name or "")]
        base = candidates[0]
        if "__p_" in base:
            candidates.append(base.split("__p_", 1)[0])
        if "__override_" in base:
            candidates.append(base.split("__override_", 1)[0])
        for name in candidates:
            if not name:
                continue
            for cell in self.project.get("cells", {}).values():
                if cell.get("name") == name:
                    return self.cell_port_names(cell)
            for cells in self.project.get("importedCells", {}).values():
                for cell in cells:
                    if cell.get("name") == name:
                        return self.cell_port_names(cell)
        return []

    def refresh_results(self) -> None:
        if self.project:
            self.project["results"] = [
                result
                for result in self.project.get("results", [])
                if not self.is_x_sidecar_result(result)
            ]
        results = (self.project or {}).get("results", [])
        self.results.clear()
        groups: dict[str, list[tuple[int, dict[str, Any]]]] = {}
        for idx, result in enumerate(results):
            if self.is_x_sidecar_result(result):
                continue
            run_id = result.get("run_id", "Unknown")
            groups.setdefault(run_id, []).append((idx, result))
        for run_id in sorted(groups.keys(), reverse=True):
            items = groups[run_id]
            s = "s" if len(items) != 1 else ""
            pass_count = sum(1 for _, r in items if r.get("ref_status") == "ok")
            fail_count = sum(1 for _, r in items if r.get("ref_status") not in (None, "ok", "no_ref"))
            status_str = f" — {pass_count}✓" if pass_count > 0 else ""
            if fail_count > 0:
                status_str += f" {fail_count}✗"
            group_item = QTreeWidgetItem([f"Run: {run_id}  ({len(items)} result{s}){status_str}", "", "", ""])
            group_item.setExpanded(True)
            self.results.addTopLevelItem(group_item)
            for idx, result in items:
                ref_status = result.get("ref_status")
                if ref_status is None:
                    ref_text = "-"
                elif ref_status == "ok":
                    ref_text = "✓ OK"
                elif ref_status == "no_ref":
                    ref_text = "no ref"
                else:
                    details = result.get("ref_details", {}) or {}
                    max_abs = details.get("max_abs")
                    ref_text = f"✗ Δ {self._format_deviation(max_abs)}" if max_abs is not None else "✗ FAIL"
                child = QTreeWidgetItem([
                    str(result.get("name", "")),
                    str(result.get("cell", "")),
                    str(result.get("rows", "")),
                    ref_text,
                ])
                child.setData(0, Qt.ItemDataRole.UserRole, idx)
                details = result.get("ref_details", {}) or {}
                if details:
                    child.setToolTip(3, self._format_ref_details(details))
                if ref_status == "ok":
                    child.setForeground(3, QBrush(QColor(0, 150, 0)))
                elif ref_status == "no_ref":
                    child.setForeground(3, QBrush(QColor(120, 120, 120)))
                elif ref_status not in (None, "ok", "no_ref"):
                    child.setForeground(3, QBrush(QColor(180, 0, 0)))
                group_item.addChild(child)

    def is_x_sidecar_result(self, result: dict[str, Any]) -> bool:
        if result.get("kind") != "x":
            return False
        path = str(result.get("source_path") or result.get("absolute_path") or result.get("name") or "")
        return any(marker in path for marker in ("_x_XFB", "_x_XS_full", "_x_XT_full"))

    def base_x_result_for_sidecar(self, sidecar: dict[str, Any]) -> dict[str, Any] | None:
        if not self.project:
            return None
        manifest_key = sidecar.get("manifest_key")
        run_id = sidecar.get("run_id")
        for result in self.project.get("results", []):
            if result.get("kind") != "x" or self.is_x_sidecar_result(result):
                continue
            if result.get("manifest_key") == manifest_key and result.get("run_id") == run_id:
                return result
        return None

    def _find_reference_csv(self, cell_name: str, manifest_key: str, kind: str) -> Path | None:
        ref_root = ROOT / "references"
        if not ref_root.exists():
            return None
        manifest_name = "x_cache_manifest.json" if kind == "x" else "cache_manifest.json"
        base_name = manifest_key.split("__p_")[0] if "__p_" in manifest_key else manifest_key
        candidate_dirs = sorted(
            (d for d in ref_root.iterdir() if d.is_dir()),
            key=lambda d: (0 if cell_name in d.name else 1, d.name),
        )
        for d in candidate_dirs:
            manifest_path = d / "cache" / manifest_name
            if not manifest_path.exists():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            for key, entry in manifest.items():
                ref_base = key.split("__p_")[0] if "__p_" in key else key
                if ref_base == base_name:
                    stale = Path(str(entry.get("csv", "")))
                    actual = d / "cache" / stale.name
                    if actual.exists():
                        return actual
        return None

    def _format_deviation(self, value: Any) -> str:
        try:
            return f"{float(value):.3e}"
        except (TypeError, ValueError):
            return "?"

    def _format_ref_details(self, details: dict[str, Any]) -> str:
        lines = [
            f"Status: {details.get('status', 'fail')}",
            f"Max absolute deviation: {self._format_deviation(details.get('max_abs'))}",
        ]
        if details.get("max_rel") is not None:
            lines.append(f"Max relative deviation: {self._format_deviation(details.get('max_rel'))}")
        if details.get("worst_location"):
            lines.append(f"Worst location: {details.get('worst_location')}")
        if details.get("reason"):
            lines.append(f"Reason: {details.get('reason')}")
        if details.get("reference_path"):
            lines.append(f"Reference: {details.get('reference_path')}")
        return "\n".join(lines)

    def _compare_csvs(self, path_a: Path, path_b: Path, tol: float = 1e-4) -> dict[str, Any]:
        try:
            import numpy as np
            data_a = np.loadtxt(str(path_a), delimiter=",")
            data_b = np.loadtxt(str(path_b), delimiter=",")
            data_a = np.atleast_2d(data_a)
            data_b = np.atleast_2d(data_b)
            if data_a.shape != data_b.shape:
                return {
                    "status": "fail",
                    "reason": f"shape mismatch: {data_a.shape} vs {data_b.shape}",
                    "reference_path": str(path_b),
                }
            compare_a = data_a[:, 1:] if data_a.shape[1] > 1 else data_a
            compare_b = data_b[:, 1:] if data_b.shape[1] > 1 else data_b
            abs_err = np.abs(compare_a - compare_b)
            rel_err = abs_err / np.maximum(np.abs(compare_b), 1e-12)
            max_abs = float(np.max(abs_err)) if abs_err.size else 0.0
            max_rel = float(np.max(rel_err)) if rel_err.size else 0.0
            worst = tuple(int(v) for v in np.unravel_index(int(np.argmax(abs_err)), abs_err.shape)) if abs_err.size else (0, 0)
            row = worst[0]
            col = worst[1] + (1 if data_a.shape[1] > 1 else 0)
            freq = data_a[row, 0] if data_a.shape[1] > 1 else None
            location = f"row {row + 1}, column {col + 1}"
            if freq is not None:
                location += f", freq {float(freq):.6g}"
            frequency_delta = None
            if data_a.shape[1] > 1:
                frequency_delta = float(np.max(np.abs(data_a[:, 0] - data_b[:, 0])))
            status = "ok" if max_abs < tol and (frequency_delta is None or frequency_delta < tol) else "fail"
            reason = ""
            if frequency_delta is not None and frequency_delta >= tol:
                reason = f"frequency grid differs by up to {frequency_delta:.3e}"
            elif status == "fail":
                reason = f"exceeds tolerance {tol:.1e}"
            return {
                "status": status,
                "max_abs": max_abs,
                "max_rel": max_rel,
                "worst_location": location,
                "frequency_delta": frequency_delta,
                "tolerance": tol,
                "reason": reason,
                "reference_path": str(path_b),
            }
        except Exception as exc:
            return {"status": "fail", "reason": str(exc), "reference_path": str(path_b)}

    def clear_results(self) -> None:
        if self.project:
            self.project["results"] = []
            self.refresh_results()

    def _on_result_double_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        idx = item.data(0, Qt.ItemDataRole.UserRole)
        if idx is not None:
            self.open_result_plot(idx)

    def selected_result_row(self) -> int:
        item = self.results.currentItem()
        if item is None:
            for group_idx in range(self.results.topLevelItemCount()):
                group = self.results.topLevelItem(group_idx)
                if group.childCount() > 0:
                    idx = group.child(0).data(0, Qt.ItemDataRole.UserRole)
                    return idx if idx is not None else -1
            return -1
        idx = item.data(0, Qt.ItemDataRole.UserRole)
        if idx is None:
            if item.childCount() > 0:
                idx = item.child(0).data(0, Qt.ItemDataRole.UserRole)
        return idx if idx is not None else -1

    def open_selected_result_plot(self) -> None:
        self.open_result_plot(self.selected_result_row())

    def export_selected_result_csv(self) -> None:
        if not self.project:
            return
        results = self.project.get("results", [])
        row = self.selected_result_row()
        if 0 <= row < len(results):
            self.export_result_csv(results[row])

    def open_result_plot(self, row: int) -> None:
        if not self.project:
            return
        results = self.project.get("results", [])
        if row < 0 or row >= len(results):
            return
        result = results[row]
        if self.is_x_sidecar_result(result):
            replacement = self.base_x_result_for_sidecar(result)
            if replacement is None:
                return
            result = replacement
        if not result.get("port_names"):
            result["port_names"] = self.result_port_names(str(result.get("manifest_key") or result.get("name", "")))
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Plot: {result.get('name')}")
        layout = QVBoxLayout(dialog)
        top = QWidget()
        top_layout = QHBoxLayout(top)
        controls = QComboBox()
        plot = PlotWidget(result)
        controls.addItems(plot.available_plot_types())
        initial_plot_type = controls.currentText()
        if result.get("kind") == "x" and controls.findText("X transfers magnitude") >= 0:
            controls.setCurrentText("X transfers magnitude")
            initial_plot_type = "X transfers magnitude"
        reset_view = QPushButton("Reset View")
        reset_view.clicked.connect(plot.reset_view)
        cursor = QLabel("Cursor: move over plot")
        plot.set_cursor_label(cursor)
        top_layout.addWidget(QLabel("Plot type"))
        top_layout.addWidget(controls)
        top_layout.addWidget(reset_view)
        y_min_edit = QLineEdit()
        y_min_edit.setPlaceholderText("Y min")
        y_min_edit.setMaximumWidth(70)
        y_max_edit = QLineEdit()
        y_max_edit.setPlaceholderText("Y max")
        y_max_edit.setMaximumWidth(70)
        def apply_y_range() -> None:
            try:
                y_min = float(y_min_edit.text())
                y_max = float(y_max_edit.text())
                if y_min < y_max:
                    plot.y_range = (y_min, y_max)
                    plot._redraw()
            except ValueError:
                pass
        y_min_edit.returnPressed.connect(apply_y_range)
        y_max_edit.returnPressed.connect(apply_y_range)
        reset_y = QPushButton("Reset Y")
        reset_y.setMaximumWidth(70)
        reset_y.clicked.connect(lambda: (setattr(plot, "y_range", None), plot._redraw()))
        top_layout.addWidget(QLabel("Y:"))
        top_layout.addWidget(y_min_edit)
        top_layout.addWidget(y_max_edit)
        top_layout.addWidget(reset_y)
        top_layout.addWidget(cursor, 1)
        layout.addWidget(top)
        body = QSplitter(Qt.Orientation.Horizontal)
        sidebar = QWidget()
        sb_lay = QVBoxLayout(sidebar)
        sb_lay.setContentsMargins(4, 4, 4, 4)
        sb_lay.addWidget(QLabel("Visible curves"))
        curve_list = QListWidget()
        curve_list.setMinimumWidth(140)
        sb_lay.addWidget(curve_list, 1)
        body.addWidget(sidebar)
        def refresh_curve_list() -> None:
            curve_list.blockSignals(True)
            curve_list.clear()
            for name in plot.curve_names()[:48]:
                item = QListWidgetItem(name)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Checked)
                curve_list.addItem(item)
            curve_list.blockSignals(False)

        plot.set_plot_type(initial_plot_type)
        refresh_curve_list()
        curve_list.itemChanged.connect(lambda item: plot.set_curve_visible(item.text(), item.checkState() == Qt.CheckState.Checked))
        controls.currentTextChanged.connect(plot.set_plot_type)
        controls.currentTextChanged.connect(lambda _value: refresh_curve_list())
        canvas_panel = QWidget()
        cp_lay = QVBoxLayout(canvas_panel)
        cp_lay.setContentsMargins(0, 0, 0, 0)
        toolbar = NavigationToolbar2QT(plot._canvas, canvas_panel)
        cp_lay.addWidget(toolbar)
        cp_lay.addWidget(plot, 1)
        body.addWidget(canvas_panel)
        body.setStretchFactor(0, 0)
        body.setStretchFactor(1, 1)
        body.setSizes([160, 720])
        layout.addWidget(body)
        actions = QWidget()
        actions_layout = QHBoxLayout(actions)
        export = QPushButton("Export CSV")
        export.clicked.connect(lambda: self.export_result_csv(result))
        export_png = QPushButton("Export PNG")
        export_png.clicked.connect(lambda: self.export_plot_png(plot, result))
        copy_summary = QPushButton("Copy Figure Summary")
        copy_summary.clicked.connect(lambda: QApplication.clipboard().setText(f"{result.get('name')} | {controls.currentText()} | {len(plot.curve_names())} curves | source={result.get('source_path', '')}"))
        actions_layout.addWidget(export)
        actions_layout.addWidget(export_png)
        actions_layout.addWidget(copy_summary)
        layout.addWidget(actions)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.resize(900, 650)
        dialog.setMinimumSize(700, 500)
        dialog.exec()

    def export_result_csv(self, result: dict[str, Any]) -> None:
        file_name, _ = QFileDialog.getSaveFileName(self, "Export Result CSV", result.get("name", "result.csv"), "CSV files (*.csv)")
        if file_name:
            Path(file_name).write_text(result.get("csv", ""), encoding="utf-8")

    def export_plot_png(self, plot: PlotWidget, result: dict[str, Any]) -> None:
        default = f"{Path(str(result.get('name', 'result'))).stem}.png"
        file_name, _ = QFileDialog.getSaveFileName(self, "Export Plot PNG", default, "PNG files (*.png)")
        if not file_name:
            return
        try:
            plot._fig.savefig(file_name, dpi=150, bbox_inches="tight")
        except Exception as exc:
            self.popup("Export failed", str(exc), "warning")

    def refresh_file_watcher(self) -> None:
        old = self.watcher.files()
        if old:
            self.watcher.removePaths(old)
        if not self.project_dir or not self.project:
            return
        paths = []
        self.imported_watch_paths = set()
        for cell in self.project.get("cells", {}).values():
            name = cell.get("fileName")
            if name and not cell.get("readOnly"):
                path = self.project_dir / name
                if path.exists():
                    paths.append(str(path))
                    self.file_mtimes[name] = path.stat().st_mtime
        for imp in self.project.get("imports", []):
            if not imp.get("enabled", True):
                continue
            import_dir = Path(str(imp.get("path", "")))
            if import_dir.exists():
                paths.append(str(import_dir))
                self.imported_watch_paths.add(str(import_dir))
        if paths:
            self.watcher.addPaths(paths)

    def autosave_dirty_cells(self) -> None:
        if not self.project_dir or not self.project:
            return
        for cell in self.project.get("cells", {}).values():
            if not cell.get("dirty") or cell.get("readOnly"):
                continue
            path = self.project_dir / f".{clean_name(cell.get('name', 'cell'))}.autosave.json"
            try:
                path.write_text(json.dumps(self.export_pipeline_cell(cell), indent=2), encoding="utf-8")
                self.add_message("Info", f"Autosaved {cell.get('name')}.")
            except Exception as exc:
                self.add_message("Warning", f"Autosave failed for {cell.get('name')}.", str(exc))

    def on_file_changed(self, path_text: str) -> None:
        if not self.project or not self.project_dir:
            return
        path = Path(path_text)
        if str(path) in self.imported_watch_paths:
            self.reload_imported_projects(silent=False)
            self.refresh_file_watcher()
            return
        cell = next((c for c in self.project.get("cells", {}).values() if c.get("fileName") == path.name), None)
        if not cell or not path.exists():
            return
        current_mtime = path.stat().st_mtime
        if current_mtime <= self.file_mtimes.get(path.name, 0) + 0.5:
            self.refresh_file_watcher()
            return
        if cell.get("dirty"):
            choice = self.popup(
                "File changed externally",
                f'Cell "{cell.get("name")}" changed on disk. Reload from disk and discard unsaved GUI edits?',
                "question",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if choice != QMessageBox.StandardButton.Yes:
                self.refresh_file_watcher()
                return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            imported = self.import_pipeline_cell(raw)
            imported["id"] = cell["id"]
            imported["fileName"] = path.name
            imported["dirty"] = False
            self.project["cells"][cell["id"]] = imported
            self.file_mtimes[path.name] = current_mtime
            self.add_message("Info", f"Reloaded externally changed cell {imported.get('name')}.")
            self.refresh_all()
        except Exception as exc:
            self.add_message("Warning", f"Could not reload {path.name}.", str(exc))
        self.refresh_file_watcher()

    def show_project_summary(self) -> None:
        if not self.project:
            self.popup("Project Summary", "No project is open.")
            return
        missing = [item for item in self.project.get("imports", []) if item.get("enabled") and not Path(item.get("path", "")).exists()]
        text = "\n".join(
            [
                f"Name: {self.project.get('name')}",
                f"Path: {self.project_dir or self.project.get('path', '')}",
                f"Cells: {len(self.project.get('cells', {}))}",
                f"Imported projects: {len(self.project.get('imports', []))}",
                f"Results: {len(self.project.get('results', []))}",
                f"Missing references: {len(missing)}",
            ]
        )
        self.popup("Project Summary", text)

    def show_shortcuts(self) -> None:
        self.popup(
            "Keyboard Shortcuts",
            "\n".join(
                [
                    "i: Add instance",
                    "q: Show properties",
                    "o: Open selected block",
                    "p: Add pin label",
                    "l: Add internal label",
                    "w: Wiring mode",
                    "Esc: Exit current mode",
                    "Del: Delete selection",
                    "Ctrl+S: Save current cell",
                    "Ctrl+Z: Undo",
                    "Ctrl+Y / Ctrl+Shift+Z: Redo",
                    "Ctrl+C: Copy",
                    "Ctrl+V: Paste",
                    "Ctrl+A: Select all",
                ]
            ),
        )

    def show_about(self) -> None:
        self.popup(
            "About",
            "Circuit Project GUI V2\n"
            "Native PyQt6 project/cell/schematic editor with hidden pipeline adapter.\n\n"
            "For questions, suggestions or complaints, don't hesitate to contact me:\n"
            "Benedikt Eble\n"
            "bene.eble@gmail.com  |  beeble@student.ethz.ch",
        )


def main() -> int:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
