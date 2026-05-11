from __future__ import annotations

import csv
import json
import math
import os
import sys
from pathlib import Path
from typing import Any

os.environ.setdefault("MPLCONFIGDIR", "/tmp/matplotlib")

import matplotlib
matplotlib.use("QtAgg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg, NavigationToolbar2QT

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from gui_core import LOGIC_DIR


class PlotWidget(QWidget):
    """Interactive Qt plotter aligned with the standalone plotting script.

    Expected base S CSV format: frequency_GHz followed by real/imaginary column pairs
    for a square S matrix in out-port-major, in-port-minor order. This matches
    load_s_csv() in the plotting script. Headerless files and the GUI's normalized
    header form are both accepted.
    """

    def __init__(self, result: dict[str, Any] | str, parent=None):
        super().__init__(parent)
        if isinstance(result, dict):
            self.result = result
            self.csv_text = str(result.get("csv", ""))
        else:
            self.result = {"csv": str(result)}
            self.csv_text = str(result)
        self.plot_type = "S |S| dB"
        self.hidden_curves: set[str] = set()
        self.x_range: tuple[float, float] | None = None
        self.y_range: tuple[float, float] | None = None
        self.cursor_label: QLabel | None = None
        self.last_plot_rect = None
        self.last_x_key = ""
        self.last_xs: list[float] = []
        self.last_curves: list[dict[str, Any]] = []
        self.last_status = ""
        self.setMinimumHeight(320)
        self.setMouseTracking(True)
        self._fig = Figure(tight_layout=True)
        self._canvas = FigureCanvasQTAgg(self._fig)
        self._canvas.setParent(self)
        self._ax = self._fig.add_subplot(111)
        self._canvas.mpl_connect("motion_notify_event", self._on_mpl_motion)
        _layout = QVBoxLayout(self)
        _layout.setContentsMargins(0, 0, 0, 0)
        _layout.addWidget(self._canvas)

    def source_csv_path(self) -> Path | None:
        source_path = str(self.result.get("source_path", ""))
        if source_path:
            candidates = [Path(source_path), LOGIC_DIR / source_path]
            if self.result.get("absolute_path"):
                candidates.insert(0, Path(str(self.result.get("absolute_path"))))
            for candidate in candidates:
                if candidate.exists():
                    return candidate
        return None

    def canonical_base_csv_path(self, csv_path: Path) -> Path:
        text = str(csv_path)
        for suffix in ["_x_XFB.csv", "_x_XS_full.csv", "_x_XT_full.csv", "_nodeflux.csv"]:
            if text.endswith(suffix):
                return Path(text[: -len(suffix)] + ".csv")
        return csv_path

    def sibling_paths(self, csv_path: Path) -> dict[str, Path]:
        base = self.canonical_base_csv_path(csv_path).with_suffix("")
        return {
            "multimode_full": Path(f"{base}_multimode_full.csv"),
            "multimode_modes": Path(f"{base}_multimode_modes.json"),
            "multimode_power": Path(f"{base}_multimode_power.csv"),
            "multimode_diagnostics": Path(f"{base}_multimode_diagnostics.csv"),
            "xfb": Path(f"{base}_x_XFB.csv"),
            "xs_full": Path(f"{base}_x_XS_full.csv"),
            "xt_full": Path(f"{base}_x_XT_full.csv"),
            "x_modes": Path(f"{base}_x_modes.json"),
            "nodeflux": Path(f"{base}_nodeflux.csv"),
        }

    def available_plot_types(self) -> list[str]:
        types = ["S |S| dB", "S phase degrees", "S real", "S imaginary"]
        csv_path = self.source_csv_path()
        if csv_path:
            paths = self.sibling_paths(csv_path)
            if paths["multimode_full"].exists() and paths["multimode_modes"].exists():
                types.extend(["Multimode output power", "Signal/idler transfers"])
            if paths["multimode_power"].exists():
                types.extend(["Power by mode", "Percent total power", "Signed power"])
            if paths["multimode_diagnostics"].exists():
                types.append("Diagnostics")
            if paths["nodeflux"].exists():
                types.append("Nodeflux bode")
            if paths["x_modes"].exists():
                if paths["xfb"].exists():
                    types.extend(["XFB magnitude", "XFB phase"])
                if paths["xs_full"].exists():
                    types.extend(["X transfers magnitude", "X transfers phase", "XS focused magnitude", "XS focused phase"])
                if paths["xt_full"].exists():
                    types.extend(["XT focused magnitude", "XT focused phase"])
        return types

    def set_plot_type(self, value: str) -> None:
        self.plot_type = value
        self.hidden_curves.clear()
        self.x_range = None
        self._redraw()

    def set_cursor_label(self, label: QLabel) -> None:
        self.cursor_label = label

    def curve_names(self) -> list[str]:
        if self.last_curves:
            return [curve["label"] for curve in self.last_curves]
        curves, bars, _ylabel = self.current_plot_data()
        if bars:
            return [bar[0] for bar in bars]
        return [curve["label"] for curve in curves]

    def set_curve_visible(self, key: str, visible: bool) -> None:
        if visible:
            self.hidden_curves.discard(key)
        else:
            self.hidden_curves.add(key)
        self._redraw()
        self.update()

    def reset_view(self) -> None:
        self.x_range = None
        self.y_range = None
        self._ax.autoscale(True)
        self._canvas.draw_idle()

    def rows_from_text(self, text: str) -> list[list[float]]:
        rows: list[list[float]] = []
        for raw in csv.reader(text.splitlines()):
            if not raw:
                continue
            try:
                rows.append([float(value) for value in raw])
            except ValueError:
                continue
        return rows

    def rows_from_path(self, path: Path) -> list[list[float]]:
        return self.rows_from_text(path.read_text(encoding="utf-8"))

    def load_base_rows(self) -> list[list[float]]:
        csv_path = self.source_csv_path()
        if csv_path and csv_path.exists():
            return self.rows_from_path(self.canonical_base_csv_path(csv_path))
        return self.rows_from_text(self.csv_text)

    def load_s_csv(self, csv_path: Path | None = None) -> tuple[list[float], list[list[list[complex]]]]:
        rows = self.rows_from_path(csv_path) if csv_path else self.load_base_rows()
        if not rows:
            raise ValueError("No numeric CSV rows found.")
        ncols = len(rows[0])
        num_ports_float = math.sqrt(max(0, (ncols - 1) / 2))
        num_ports = int(num_ports_float)
        if 1 + 2 * num_ports * num_ports != ncols:
            raise ValueError("Cannot infer square S-matrix size from CSV columns.")
        freqs = [row[0] for row in rows]
        matrix = [[[0j for _ in freqs] for _ in range(num_ports)] for _ in range(num_ports)]
        col = 1
        for out_p in range(num_ports):
            for in_p in range(num_ports):
                for k, row in enumerate(rows):
                    matrix[out_p][in_p][k] = complex(row[col], row[col + 1])
                col += 2
        return freqs, matrix

    def load_s_csv_pairs(self, csv_path: Path, size: int, pairs: list[tuple[int, int]]) -> tuple[list[float], dict[tuple[int, int], list[complex]]]:
        needed = sorted(set(pairs))
        values = {pair: [] for pair in needed}
        freqs: list[float] = []
        if not needed:
            return freqs, values
        pair_cols = {
            pair: 1 + 2 * (pair[0] * size + pair[1])
            for pair in needed
            if 0 <= pair[0] < size and 0 <= pair[1] < size
        }
        if not pair_cols:
            return freqs, values
        try:
            import numpy as np
            usecols = [0]
            for pair in needed:
                col = pair_cols.get(pair)
                if col is not None:
                    usecols.extend([col, col + 1])
            data = np.loadtxt(str(csv_path), delimiter=",", usecols=usecols)
            data = np.atleast_2d(data)
            freqs = [float(value) for value in data[:, 0]]
            data_col = 1
            for pair in needed:
                if pair not in pair_cols:
                    continue
                values[pair] = [
                    complex(float(real), float(imag))
                    for real, imag in zip(data[:, data_col], data[:, data_col + 1])
                ]
                data_col += 2
            return freqs, values
        except Exception:
            pass
        max_col = max(col + 1 for col in pair_cols.values())
        with csv_path.open("r", encoding="utf-8", newline="") as handle:
            for row in csv.reader(handle):
                if len(row) <= max_col:
                    continue
                try:
                    freqs.append(float(row[0]))
                    for pair, col in pair_cols.items():
                        values[pair].append(complex(float(row[col]), float(row[col + 1])))
                except ValueError:
                    continue
        return freqs, values

    def load_csv_with_header(self, path: Path) -> tuple[list[str], list[list[float]]]:
        lines = path.read_text(encoding="utf-8").splitlines()
        if not lines:
            return [], []
        header = next(csv.reader([lines[0]]))
        data: list[list[float]] = []
        for row in csv.reader(lines[1:]):
            if not row:
                continue
            try:
                data.append([float(value) for value in row])
            except ValueError:
                continue
        return header, data

    def load_json(self, path: Path) -> dict[str, Any]:
        return json.loads(path.read_text(encoding="utf-8"))

    def load_nodeflux_csv(self, path: Path) -> dict[str, list[tuple[float, complex]]]:
        lines = path.read_text(encoding="utf-8").splitlines()
        if not lines:
            return {}
        reader = csv.DictReader(lines)
        by_node: dict[str, list[tuple[float, complex]]] = {}
        for row in reader:
            try:
                node = str(row.get("node", "")).strip()
                freq = float(row.get("frequency_GHz", "nan"))
                real = float(row.get("real", "nan"))
                imag = float(row.get("imag", "nan"))
            except ValueError:
                continue
            if not node or math.isnan(freq):
                continue
            by_node.setdefault(node, []).append((freq, complex(real, imag)))
        for values in by_node.values():
            values.sort(key=lambda item: item[0])
        return by_node

    def db20(self, value: complex | float) -> float:
        return 20 * math.log10(max(abs(value), 1e-300))

    def nodeflux_db20(self, value: complex | float) -> float:
        return 20 * math.log10(max(abs(value), sys.float_info.epsilon))

    def db10_power(self, value: float) -> float:
        return 10 * math.log10(max(float(value), 1e-300))

    def phase_deg(self, value: complex) -> float:
        return math.degrees(math.atan2(value.imag, value.real))

    def port_label(self, index: int) -> str:
        names = self.result.get("port_names", []) if isinstance(self.result, dict) else []
        if isinstance(names, list) and 0 <= index < len(names) and str(names[index]).strip():
            return str(names[index])
        return str(index + 1)

    def s_curve_label(self, out_p: int, in_p: int) -> str:
        out_name = self.port_label(out_p)
        in_name = self.port_label(in_p)
        if out_name == str(out_p + 1) and in_name == str(in_p + 1):
            return f"S{out_p + 1}{in_p + 1}"
        return f"S {out_name}\u2190{in_name}"

    def unwrap_degrees(self, values: list[complex]) -> list[float]:
        out: list[float] = []
        offset = 0.0
        previous: float | None = None
        for value in values:
            phase = self.phase_deg(value) + offset
            if previous is not None:
                while phase - previous > 180:
                    offset -= 360
                    phase -= 360
                while phase - previous < -180:
                    offset += 360
                    phase += 360
            out.append(phase)
            previous = phase
        return out

    def unwrap_radians(self, values: list[complex]) -> list[float]:
        out: list[float] = []
        offset = 0.0
        previous: float | None = None
        for value in values:
            phase = math.atan2(value.imag, value.real) + offset
            if previous is not None:
                while phase - previous > math.pi:
                    offset -= 2 * math.pi
                    phase -= 2 * math.pi
                while phase - previous < -math.pi:
                    offset += 2 * math.pi
                    phase += 2 * math.pi
            out.append(phase)
            previous = phase
        return out

    def mode_port_index(self, mode_idx: int, port_idx: int, num_ports: int) -> int:
        return mode_idx * num_ports + port_idx

    def port_major_index(self, port_idx: int, mode_idx: int, num_modes: int) -> int:
        return port_idx * num_modes + mode_idx

    def s_matrix_curves(self) -> tuple[list[dict[str, Any]], str]:
        freqs, matrix = self.load_s_csv()
        num_ports = len(matrix)
        curves: list[dict[str, Any]] = []
        for in_p in range(num_ports):
            for out_p in range(num_ports):
                values = matrix[out_p][in_p]
                label = self.s_curve_label(out_p, in_p)
                if self.plot_type == "S phase degrees":
                    ys = self.unwrap_degrees(values)
                    ylabel = "Phase (deg)"
                elif self.plot_type == "S real":
                    ys = [v.real for v in values]
                    ylabel = "Real"
                elif self.plot_type == "S imaginary":
                    ys = [v.imag for v in values]
                    ylabel = "Imaginary"
                else:
                    ys = [self.db20(v) for v in values]
                    ylabel = "|S| (dB)"
                curves.append({"label": label, "x": freqs, "y": ys})
        return curves, ylabel

    def multimode_output_curves(self, csv_path: Path) -> tuple[list[dict[str, Any]], str]:
        paths = self.sibling_paths(csv_path)
        meta = self.load_json(paths["multimode_modes"])
        ports = [int(p) for p in meta.get("ports", [])]
        modes = [int(m) for m in meta.get("modes", [])]
        if not ports or not modes:
            return [], "Power from reference input (dB)"
        ref_mode = int(meta.get("reference_input_mode", 0))
        ref_port = int(meta.get("reference_input_port", ports[0]))
        freqs, s_full = self.load_s_csv(paths["multimode_full"])
        num_ports = len(ports)
        ref_mode_idx = modes.index(ref_mode) if ref_mode in modes else (modes.index(0) if 0 in modes else 0)
        ref_port_idx = ports.index(ref_port) if ref_port in ports else 0
        ref_col = self.mode_port_index(ref_mode_idx, ref_port_idx, num_ports)
        powers: list[tuple[int, list[float], float]] = []
        for mode_idx, mode in enumerate(modes):
            ys = []
            for k in range(len(freqs)):
                total = 0.0
                for port_idx in range(num_ports):
                    row = self.mode_port_index(mode_idx, port_idx, num_ports)
                    total += abs(s_full[row][ref_col][k]) ** 2
                ys.append(self.db10_power(total))
            peak = max(ys) if ys else -1e300
            powers.append((mode, ys, peak))
        curves = [
            {"label": f"mode ({mode},)", "x": freqs, "y": ys}
            for mode, ys, _peak in sorted(powers, key=lambda item: item[2], reverse=True)[:12]
        ]
        return curves, "Power from reference input (dB)"

    def signal_idler_curves(self, csv_path: Path) -> tuple[list[dict[str, Any]], str]:
        paths = self.sibling_paths(csv_path)
        meta = self.load_json(paths["multimode_modes"])
        ports = [int(p) for p in meta.get("ports", [])]
        modes = [int(m) for m in meta.get("modes", [])]
        if 0 not in modes or -2 not in modes or len(ports) < 2:
            return [], "|S| (dB)"
        freqs, s_full = self.load_s_csv(paths["multimode_full"])
        num_ports = len(ports)
        sig_idx = modes.index(0)
        idl_idx = modes.index(-2)

        def idx(mode_idx: int, port_number: int) -> int:
            port_idx = ports.index(port_number) if port_number in ports else port_number - 1
            return self.mode_port_index(mode_idx, port_idx, num_ports)

        p1, p2 = ports[0], ports[1]
        terms = [
            (idx(sig_idx, p2), idx(sig_idx, p1), "sig→sig forward"),
            (idx(idl_idx, p2), idx(sig_idx, p1), "sig→idler forward"),
            (idx(sig_idx, p1), idx(sig_idx, p2), "sig→sig reverse"),
            (idx(idl_idx, p1), idx(sig_idx, p2), "sig→idler reverse"),
        ]
        curves = [
            {"label": label, "x": freqs, "y": [self.db20(s_full[row][col][k]) for k in range(len(freqs))]}
            for row, col, label in terms
        ]
        return curves, "|S| (dB)"

    def multimode_power_bars(self, csv_path: Path) -> tuple[list[tuple[str, float]], str]:
        header, data = self.load_csv_with_header(self.sibling_paths(csv_path)["multimode_power"])
        col = {name: i for i, name in enumerate(header)}
        if "mode_n" not in col:
            return [], ""
        if self.plot_type == "Percent total power":
            value_key, ylabel = "percent_total", "%"
        elif self.plot_type == "Signed power":
            value_key, ylabel = "signed_power", "σ × power"
        else:
            value_key, ylabel = "power", "Power"
        if value_key not in col:
            return [], ylabel
        return [(f"({int(row[col['mode_n']])},)", row[col[value_key]]) for row in data], ylabel

    def diagnostics_curves(self, csv_path: Path) -> tuple[list[dict[str, Any]], str]:
        header, data = self.load_csv_with_header(self.sibling_paths(csv_path)["multimode_diagnostics"])
        col = {name: i for i, name in enumerate(header)}
        freq_col = col.get("frequency_GHz")
        if freq_col is None:
            return [], "Diagnostics"
        freqs = [row[freq_col] for row in data]
        curves = []
        for key, label, offset in [
            ("max_abs_SSigmaSdagger_minus_Sigma", "log10 max|SΣS†−Σ|", 0.0),
            ("max_abs_SdaggerS_minus_I", "log10 max|S†S−I|", 0.0),
            ("bogoliubov_sum", "log10 |Bogoliubov−1|", 1.0),
        ]:
            if key in col:
                ys = [math.log10(max(abs(row[col[key]] - offset), 1e-300)) for row in data]
                curves.append({"label": label, "x": freqs, "y": ys})
        if "bogoliubov_sum" in col:
            curves.append({"label": "Bogoliubov sum", "x": freqs, "y": [row[col["bogoliubov_sum"]] for row in data]})
        return curves, "Diagnostics"

    def xparam_ports_modes(self, csv_path: Path) -> tuple[list[int], list[int]]:
        paths = self.sibling_paths(csv_path)
        meta = self.load_json(paths["x_modes"])
        matrix_ports = meta.get("matrix_ports", meta.get("saved_signal_ports", meta.get("ports", [])))
        ports = [int(p) for p in matrix_ports]
        modes = [int(m) for m in meta.get("signal_modes", [])]
        return ports, modes

    def x_transfer_values(
        self,
        xmat: list[list[list[complex]]],
        ports: list[int],
        modes: list[int],
        out_port: int,
        out_mode: int,
        in_port: int,
        in_mode: int,
    ) -> list[complex] | None:
        if out_port not in ports or in_port not in ports or out_mode not in modes or in_mode not in modes:
            return None
        row = self.port_major_index(ports.index(out_port), modes.index(out_mode), len(modes))
        col = self.port_major_index(ports.index(in_port), modes.index(in_mode), len(modes))
        if row >= len(xmat) or col >= len(xmat[row]):
            return None
        return xmat[row][col]

    def default_x_transfer_specs(self, ports: list[int], modes: list[int]) -> list[tuple[str, int, int, int, int, str]]:
        if not ports or not modes:
            return []
        in_port = ports[0]
        out_port = ports[1] if len(ports) > 1 else ports[0]
        in_mode = 0 if 0 in modes else modes[0]
        specs = [("XS", out_port, in_mode, in_port, in_mode, "Signal gain")]
        if -2 in modes:
            specs.append(("XS", out_port, -2, in_port, in_mode, "Idler generation"))
        conj_mode = -in_mode
        if conj_mode in modes:
            specs.append(("XT", out_port, conj_mode, in_port, in_mode, "Conjugate response"))
        return specs

    def x_default_transfer_curves(self, csv_path: Path, phase: bool) -> tuple[list[dict[str, Any]], str]:
        paths = self.sibling_paths(csv_path)
        ports, modes = self.xparam_ports_modes(csv_path)
        if not ports or not modes:
            return [], "Phase (deg)" if phase else "Magnitude (dB)"
        num_ports = len(ports)
        num_modes = len(modes)
        expected_size = num_ports * num_modes
        ylabel = "Phase (deg)" if phase else "Magnitude (dB)"
        transform = self.unwrap_degrees if phase else lambda values: [self.db20(v) for v in values]
        curves: list[dict[str, Any]] = []
        pairs_by_kind: dict[str, list[tuple[int, int]]] = {"XS": [], "XT": []}
        specs: list[tuple[str, tuple[int, int], str, int, int, int, int]] = []
        for kind, out_port, out_mode, in_port, in_mode, label in self.default_x_transfer_specs(ports, modes):
            if out_port not in ports or in_port not in ports or out_mode not in modes or in_mode not in modes:
                continue
            row = self.port_major_index(ports.index(out_port), modes.index(out_mode), num_modes)
            col = self.port_major_index(ports.index(in_port), modes.index(in_mode), num_modes)
            if row >= expected_size or col >= expected_size:
                continue
            pair = (row, col)
            pairs_by_kind[kind].append(pair)
            specs.append((kind, pair, label, out_port, out_mode, in_port, in_mode))
        freqs, xs_values = self.load_s_csv_pairs(paths["xs_full"], expected_size, pairs_by_kind["XS"])
        xt_values: dict[tuple[int, int], list[complex]] = {}
        if paths["xt_full"].exists() and pairs_by_kind["XT"]:
            xt_freqs, xt_values = self.load_s_csv_pairs(paths["xt_full"], expected_size, pairs_by_kind["XT"])
            if not freqs:
                freqs = xt_freqs
        for kind, pair, label, out_port, out_mode, in_port, in_mode in specs:
            values = (xs_values if kind == "XS" else xt_values).get(pair)
            if not values:
                continue
            curves.append(
                {
                    "label": f"{label}: {kind} p{out_port},m{out_mode} <- p{in_port},m{in_mode}",
                    "x": freqs,
                    "y": transform(values),
                }
            )
        return curves, ylabel

    def x_focused_transfer_curves(self, csv_path: Path, x_kind: str, phase: bool) -> tuple[list[dict[str, Any]], str]:
        paths = self.sibling_paths(csv_path)
        matrix_path = paths["xs_full"] if x_kind == "XS" else paths["xt_full"]
        ports, modes = self.xparam_ports_modes(csv_path)
        if not ports or not modes:
            return [], "Phase (deg)" if phase else "Magnitude (dB)"

        in_port = ports[0]
        out_port = ports[1] if len(ports) > 1 else ports[0]
        in_mode = 0 if 0 in modes else modes[0]
        out_mode = in_mode if x_kind == "XS" else -in_mode
        if out_port not in ports or in_port not in ports or out_mode not in modes or in_mode not in modes:
            return [], "Phase (deg)" if phase else "Magnitude (dB)"
        num_modes = len(modes)
        expected_size = len(ports) * num_modes
        row = self.port_major_index(ports.index(out_port), modes.index(out_mode), num_modes)
        col = self.port_major_index(ports.index(in_port), modes.index(in_mode), num_modes)
        freqs, pair_values = self.load_s_csv_pairs(matrix_path, expected_size, [(row, col)])
        values = pair_values.get((row, col), [])
        if not values:
            return [], "Phase (deg)" if phase else "Magnitude (dB)"

        ylabel = "Phase (deg)" if phase else "Magnitude (dB)"
        transform = self.unwrap_degrees if phase else lambda vals: [self.db20(v) for v in vals]
        transfer = f"p{out_port},m{out_mode} <- p{in_port},m{in_mode}"
        curves = [{"label": f"{x_kind} {transfer}", "x": freqs, "y": transform(values)}]
        return curves, "Phase (deg)" if phase else "Magnitude (dB)"

    def s_vs_xs_curves(self, csv_path: Path, phase: bool) -> tuple[list[dict[str, Any]], str]:
        paths = self.sibling_paths(csv_path)
        meta = self.load_json(paths["x_modes"])
        matrix_ports = meta.get("matrix_ports", meta.get("saved_signal_ports", meta.get("ports", [])))
        ports = [int(p) for p in matrix_ports]
        modes = [int(m) for m in meta.get("signal_modes", [])]
        if len(ports) < 2 or 0 not in modes:
            return [], "Phase (deg)" if phase else "Magnitude (dB)"

        freqs, smat = self.load_s_csv(csv_path)
        _xfreqs, xs_full = self.load_s_csv(paths["xs_full"])
        sig_idx = modes.index(0)
        num_modes = len(modes)
        out_i = 1
        in_i = 0
        xs_row = self.port_major_index(out_i, sig_idx, num_modes)
        xs_col = self.port_major_index(in_i, sig_idx, num_modes)
        if xs_row >= len(xs_full) or xs_col >= len(xs_full[xs_row]):
            return [], "Phase (deg)" if phase else "Magnitude (dB)"

        s_values = smat[out_i][in_i]
        xs_values = xs_full[xs_row][xs_col]
        ylabel = "Phase (deg)" if phase else "Magnitude (dB)"
        transform = self.unwrap_degrees if phase else lambda values: [self.db20(v) for v in values]
        transfer = f"p{ports[out_i]},m0 <- p{ports[in_i]},m0"
        return [
            {"label": f"S {transfer}", "x": freqs, "y": transform(s_values)},
            {"label": f"XS {transfer}", "x": freqs, "y": transform(xs_values)},
        ], ylabel

    def xfb_curves(self, csv_path: Path, phase: bool) -> tuple[list[dict[str, Any]], str]:
        header, data = self.load_csv_with_header(self.sibling_paths(csv_path)["xfb"])
        col = {name: i for i, name in enumerate(header)}
        required = {"port", "mode", "real", "imag"}
        if not required.issubset(col):
            return [], "Phase (deg)" if phase else "Magnitude (dB)"
        by_port: dict[int, dict[int, complex]] = {}
        for row in data:
            port = int(row[col["port"]])
            mode = int(row[col["mode"]])
            value = complex(row[col["real"]], row[col["imag"]])
            by_port.setdefault(port, {})[mode] = value
        modes = sorted({mode for values in by_port.values() for mode in values})
        ylabel = "Phase (deg)" if phase else "Magnitude (dB)"
        curves: list[dict[str, Any]] = []
        for port in sorted(by_port):
            values = [by_port[port].get(mode, 0j) for mode in modes]
            ys = [self.phase_deg(value) for value in values] if phase else [self.db20(value) for value in values]
            curves.append({"label": f"port {port}", "x": modes, "y": ys})
        return curves, ylabel

    def nodeflux_bode_curves(self, csv_path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        by_node = self.load_nodeflux_csv(self.sibling_paths(csv_path)["nodeflux"])
        mag_curves: list[dict[str, Any]] = []
        phase_curves: list[dict[str, Any]] = []
        for node in sorted(by_node, key=lambda value: (value != "0", value)):
            points = by_node[node]
            xs = [freq for freq, _value in points]
            values = [value for _freq, value in points]
            mag_curves.append({"label": f"node {node}", "x": xs, "y": [self.nodeflux_db20(value) for value in values]})
            phase_curves.append({"label": f"node {node}", "x": xs, "y": self.unwrap_radians(values)})
        return mag_curves, phase_curves

    def fallback_generic_curves(self) -> tuple[list[dict[str, Any]], str]:
        rows = self.load_base_rows()
        if not rows or len(rows[0]) < 2:
            return [], ""
        xs = [row[0] for row in rows]
        curves = []
        for col in range(1, len(rows[0])):
            curves.append({"label": f"col_{col}", "x": xs, "y": [row[col] for row in rows if col < len(row)]})
        return curves, "Value"

    def current_plot_data(self) -> tuple[list[dict[str, Any]], list[tuple[str, float]], str]:
        self.last_status = ""
        try:
            csv_path = self.source_csv_path()
            if self.plot_type in {"S |S| dB", "S phase degrees", "S real", "S imaginary"}:
                curves, ylabel = self.s_matrix_curves()
                return curves, [], ylabel
            if not csv_path:
                self.last_status = "Sidecar plot data needs the original source CSV path."
                return [], [], ""
            if self.plot_type == "Multimode output power":
                curves, ylabel = self.multimode_output_curves(csv_path)
                return curves, [], ylabel
            if self.plot_type == "Signal/idler transfers":
                curves, ylabel = self.signal_idler_curves(csv_path)
                return curves, [], ylabel
            if self.plot_type in {"Power by mode", "Percent total power", "Signed power"}:
                bars, ylabel = self.multimode_power_bars(csv_path)
                return [], bars, ylabel
            if self.plot_type == "Diagnostics":
                curves, ylabel = self.diagnostics_curves(csv_path)
                return curves, [], ylabel
            if self.plot_type == "Nodeflux bode":
                curves, _phase_curves = self.nodeflux_bode_curves(csv_path)
                return curves, [], "Magnitude (dB)"
            if self.plot_type in {"X transfers magnitude", "X transfers phase"}:
                curves, ylabel = self.x_default_transfer_curves(csv_path, self.plot_type.endswith("phase"))
                return curves, [], ylabel
            if self.plot_type in {"XS focused magnitude", "XS focused phase", "XT focused magnitude", "XT focused phase"}:
                x_kind = "XS" if self.plot_type.startswith("XS") else "XT"
                phase = self.plot_type.endswith("phase")
                curves, ylabel = self.x_focused_transfer_curves(csv_path, x_kind, phase)
                return curves, [], ylabel
            if self.plot_type in {"XFB magnitude", "XFB phase"}:
                curves, ylabel = self.xfb_curves(csv_path, self.plot_type.endswith("phase"))
                return curves, [], ylabel
        except Exception as exc:
            self.last_status = str(exc)
            try:
                curves, ylabel = self.fallback_generic_curves()
                return curves, [], ylabel or "Value"
            except Exception:
                return [], [], ""
        return [], [], ""

    def _redraw(self) -> None:
        self._fig.clear()
        if self.plot_type == "Nodeflux bode":
            self._draw_nodeflux_bode()
            self._canvas.draw_idle()
            return
        self._ax = self._fig.add_subplot(111)
        curves, bars, ylabel = self.current_plot_data()
        self.last_curves = curves
        if bars:
            self._draw_bars(bars, ylabel)
        else:
            self._draw_curves(curves, ylabel)
        self._canvas.draw_idle()

    def _draw_curves(self, curves: list[dict[str, Any]], ylabel: str) -> None:
        COLORS = ["#236a5b", "#8e4b32", "#3b5fa0", "#7a3b8e", "#1a7a4a", "#c46a00", "#5a3b1a", "#1a4a6a"]
        visible = [c for c in curves if c["label"] not in self.hidden_curves]
        for i, curve in enumerate(visible):
            self._ax.plot(curve["x"], curve["y"], label=curve["label"],
                          color=COLORS[i % len(COLORS)], linewidth=1.4)
        if self.x_range:
            self._ax.set_xlim(self.x_range)
        if self.y_range:
            self._ax.set_ylim(self.y_range)
        xlabel = "Pump mode k" if self.plot_type.startswith("XFB") else "Frequency (GHz)"
        self._ax.set_xlabel(xlabel, fontsize=9)
        self._ax.set_ylabel(ylabel, fontsize=9)
        self._ax.grid(True, alpha=0.3)
        if visible:
            ncol = 2 if len(visible) > 8 else 1
            if len(visible) > 12:
                self._ax.legend(loc="center left", bbox_to_anchor=(1.01, 0.5),
                                fontsize=7, ncol=ncol)
                self._fig.tight_layout(rect=[0, 0, 0.80, 1])
            else:
                self._ax.legend(loc="best", fontsize=7, ncol=ncol, framealpha=0.85)
                self._fig.tight_layout()

    def _draw_nodeflux_bode(self) -> None:
        csv_path = self.source_csv_path()
        self._ax = self._fig.add_subplot(211)
        phase_ax = self._fig.add_subplot(212, sharex=self._ax)
        if not csv_path:
            self.last_status = "Nodeflux plot data needs the original source CSV path."
            return
        try:
            mag_curves, phase_curves = self.nodeflux_bode_curves(csv_path)
        except Exception as exc:
            self.last_status = str(exc)
            return

        COLORS = ["#236a5b", "#8e4b32", "#3b5fa0", "#7a3b8e", "#1a7a4a", "#c46a00", "#5a3b1a", "#1a4a6a"]
        visible_mag = [c for c in mag_curves if c["label"] not in self.hidden_curves]
        visible_phase = [c for c in phase_curves if c["label"] not in self.hidden_curves]
        for i, curve in enumerate(visible_mag):
            self._ax.plot(curve["x"], curve["y"], marker="o", markersize=2.5,
                          label=curve["label"], color=COLORS[i % len(COLORS)], linewidth=1.2)
        for i, curve in enumerate(visible_phase):
            phase_ax.plot(curve["x"], curve["y"], marker="o", markersize=2.5,
                          label=curve["label"], color=COLORS[i % len(COLORS)], linewidth=1.2)
        self._ax.set_ylabel("Magnitude (dB)", fontsize=9)
        phase_ax.set_xlabel("Frequency (GHz)", fontsize=9)
        phase_ax.set_ylabel("Phase (rad)", fontsize=9)
        self._ax.set_title("Nodeflux Bode Magnitude", fontsize=10)
        phase_ax.set_title("Nodeflux Bode Phase", fontsize=10)
        for ax in (self._ax, phase_ax):
            ax.grid(True, alpha=0.3)
        if self.x_range:
            self._ax.set_xlim(self.x_range)
        if self.y_range:
            self._ax.set_ylim(self.y_range)
        if visible_mag:
            ncol = 2 if len(visible_mag) > 8 else 1
            self._ax.legend(loc="center left", bbox_to_anchor=(1.01, 0.5), fontsize=7, ncol=ncol)
            self._fig.tight_layout(rect=[0, 0, 0.80, 1])
        else:
            self._fig.tight_layout()

    def _draw_bars(self, bars: list[tuple[str, float]], ylabel: str) -> None:
        visible = [(lbl, v) for lbl, v in bars if lbl not in self.hidden_curves]
        if not visible:
            return
        labels, values = zip(*visible)
        self._ax.bar(range(len(values)), values, color="#236a5b")
        self._ax.set_xticks(range(len(labels)))
        self._ax.set_xticklabels(labels, rotation=55, ha="right", fontsize=7)
        self._ax.axhline(0, color="#66716d", linewidth=0.8)
        self._ax.set_ylabel(ylabel, fontsize=9)
        if self.y_range:
            self._ax.set_ylim(self.y_range)
        self._ax.grid(True, axis="y", alpha=0.3)
        self._fig.tight_layout()

    def _on_mpl_motion(self, event) -> None:
        if not self.cursor_label or event.inaxes is not self._ax or event.xdata is None:
            if self.cursor_label:
                self.cursor_label.setText("Cursor: move over plot")
            return
        curves = self.last_curves
        all_xs = sorted({x for c in curves for x in c["x"]})
        if not all_xs:
            return
        nearest = min(all_xs, key=lambda v: abs(v - event.xdata))
        xlabel = "Pump mode k" if self.plot_type.startswith("XFB") else "Frequency (GHz)"
        self.cursor_label.setText(f"Cursor: {xlabel} {nearest:.6g}")
