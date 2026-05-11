from __future__ import annotations

import json
import re
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any


HB_VALUE_KEYS = {
    "P": "port",
    "R": "R",
    "L": "L",
    "C": "C",
    "Lj": "Lj",
    "Cj": "Cj",
    "I": "I",
    "NL": "NL",
}

HB_PRIMITIVE_TYPES = set(HB_VALUE_KEYS) | {"K"}
STOP_RE = re.compile(r"(^|[^\w!])(@time\s+)?(\w+\s*=\s*)?(hbsolve|solveS|plot!?|display)\s*\(")
BLOCK_START_RE = re.compile(r"^\s*(for|if|while|function|let|begin|try|quote|macro|struct|mutable\s+struct)\b")
SYMBOL_RE = re.compile(r"\b[A-Za-z_]\w*\b")
KNOWN_EXPR_NAMES = {
    "im", "pi", "π", "exp", "sqrt", "sin", "cos", "tan", "log", "log10",
    "abs", "real", "imag", "IctoLj",
}

SOLVES_DETECT_RE = re.compile(r"\bsolveS\s*\(")
HBSOLVE_DETECT_RE = re.compile(r"\bhbsolve\s*\(")

# Julia stdlib functions that only allocate arrays — not simulation components.
JULIA_ARRAY_INIT_FNS = frozenset({
    "zeros", "ones", "fill", "similar", "rand", "randn", "copy", "deepcopy",
    "Array", "Matrix", "Vector", "repeat", "reshape",
})


BUILTIN_S_SOLVE_PORTS = {
    "ABCD_attenuator_Pi_matched": ["p1", "p2"],
    "ABCD_attenuator_Pi_unmatched": ["p1", "p2"],
    "ABCD_attenuator_T_matched": ["p1", "p2"],
    "ABCD_attenuator_T_unmatched": ["p1", "p2"],
    "ABCD_coupled_tline": ["p1", "p2", "p3", "p4"],
    "ABCD_PiY": ["p1", "p2"],
    "ABCD_seriesZ": ["p1", "p2"],
    "ABCD_shunt_signal2signal": ["p1", "p2"],
    "ABCD_shuntY": ["p1", "p2"],
    "ABCD_thru": ["p1", "p2"],
    "ABCD_tline": ["p1", "p2"],
    "ABCD_TZ": ["p1", "p2"],
    "S_circulator_clockwise": ["p1", "p2", "p3"],
    "S_circulator_counterclockwise": ["p1", "p2", "p3"],
    "S_directional_coupler": ["p1", "p2", "p3", "p4"],
    "S_directional_coupler_antisymmetric": ["p1", "p2", "p3", "p4"],
    "S_directional_coupler_symmetric": ["p1", "p2", "p3", "p4"],
    "S_hybrid_coupler_antisymmetric": ["p1", "p2", "p3", "p4"],
    "S_hybrid_coupler_symmetric": ["p1", "p2", "p3", "p4"],
    "S_match": ["p1"],
    "S_open": ["p1"],
    "S_short": ["p1"],
    "S_splitter": ["p1", "p2", "p3"],
    "S_termination": ["p1"],
}

BUILTIN_S_SOLVE_PARAMS = {
    "ABCD_attenuator_Pi_matched": ["attenuation_dB", "Z0"],
    "ABCD_attenuator_Pi_unmatched": ["Z1", "Z2", "Z0"],
    "ABCD_attenuator_T_matched": ["attenuation_dB", "Z0"],
    "ABCD_attenuator_T_unmatched": ["Z1", "Z2", "Z0"],
    "ABCD_coupled_tline": ["Z0e", "Z0o", "thetae", "thetao"],
    "ABCD_PiY": ["Y1", "Y2", "Y3"],
    "ABCD_seriesZ": ["Z1"],
    "ABCD_shunt_signal2signal": ["Y1"],
    "ABCD_shuntY": ["Y1"],
    "ABCD_thru": [],
    "ABCD_tline": ["Z0", "theta"],
    "ABCD_TZ": ["Z1", "Z2", "Z3"],
    "S_match": [],
    "S_open": [],
    "S_short": [],
    "S_termination": [],
}


def clean_name(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_]", "_", str(value or "").strip())
    if not text:
        return ""
    if not re.match(r"[A-Za-z_]", text[0]):
        text = "_" + text
    return text


def looks_numeric(value: Any) -> bool:
    try:
        float(str(value).strip())
        return True
    except (TypeError, ValueError):
        return False


def symbols_in_expr(expr: Any) -> set[str]:
    text = str(expr or "")
    function_names = set(re.findall(r"\b[A-Za-z_]\w*\b(?=\s*\()", text))
    symbols = {item for item in SYMBOL_RE.findall(text) if not item[0].isdigit()}
    return {item for item in symbols - function_names - KNOWN_EXPR_NAMES if clean_name(item) == item}


def numeric_token_or_expr(expr: Any) -> str:
    text = str(expr or "").strip()
    if looks_numeric(text):
        return text
    return text


def normalize_import_expr(expr: Any) -> str:
    text = str(expr or "").strip()
    text = text.replace("JosephsonCircuits.speed_of_light", "299792458.0")
    return text


def fallback_variable_default(expr: Any) -> tuple[str, bool]:
    """Return (default, export) for an imported helper assignment.

    Static constants are kept. Values that depend on unavailable helper calls
    are exposed as editable variables with a neutral default.
    """
    text = normalize_import_expr(expr)
    if looks_numeric(text):
        return text, False
    if re.search(r"\binclude\s*\(|\b[A-Za-z_]\w*_interp\b|[A-Za-z_]\w*\s*\.\s*\(", text):
        return "1.0", True
    return text, True


def parse_circuitdefs_from_source(source: str) -> dict[str, str]:
    defs: dict[str, str] = {}
    match = re.search(r"\bcircuitdefs\s*=\s*Dict\s*\(", source, re.DOTALL)
    if not match:
        return defs
    open_idx = source.find("(", match.start())
    close_idx = find_matching(source, open_idx, "(", ")")
    if close_idx == -1:
        return defs
    body = source[open_idx + 1:close_idx]
    for item in split_top_level(body):
        if "=>" not in item:
            continue
        key, value = item.split("=>", 1)
        key_name = clean_name(key.strip())
        value_text = value.split("#", 1)[0].strip().rstrip(",")
        if key_name and value_text:
            defs[key_name] = value_text
    return defs


def strip_runtime_section(source: str) -> str:
    """Keep setup/generator code and drop hbsolve/solveS/plot/display execution."""
    lines: list[str] = []
    depth = 0
    for raw_line in source.splitlines():
        line = raw_line.strip()
        if depth == 0 and STOP_RE.search(line):
            break
        lines.append(raw_line)
        code = line.split("#", 1)[0].strip()
        if not code:
            continue
        if BLOCK_START_RE.match(code):
            depth += 1
        if re.match(r"^\s*end\b", code):
            depth = max(0, depth - 1)
    return "\n".join(lines).strip() + "\n"


def detect_julia_simulation_type(source: str) -> str:
    """Returns 'hb', 'solves', or 'unknown'."""
    if HBSOLVE_DETECT_RE.search(source):
        return "hb"
    if SOLVES_DETECT_RE.search(source):
        return "solves"
    return "unknown"


JULIA_PROBE = r'''
using JosephsonCircuits
try
    using Symbolics
catch
end

include(ARGS[1])
out_path = ARGS[2]

function json_escape(s)
    s = replace(String(s), "\\" => "\\\\")
    s = replace(s, "\"" => "\\\"")
    s = replace(s, "\n" => "\\n")
    s = replace(s, "\r" => "\\r")
    s = replace(s, "\t" => "\\t")
    return "\"" * s * "\""
end

function json_value(x)
    if x === nothing
        return "null"
    elseif x isa Bool
        return x ? "true" : "false"
    elseif x isa Integer || x isa AbstractFloat
        return string(x)
    elseif x isa AbstractString || x isa Symbol
        return json_escape(string(x))
    elseif x isa Tuple || x isa AbstractVector
        return "[" * join([json_value(v) for v in x], ",") * "]"
    elseif x isa NamedTuple
        return json_value(Dict(string(k) => getfield(x, k) for k in keys(x)))
    elseif x isa AbstractDict
        parts = String[]
        for (k, v) in x
            push!(parts, json_escape(string(k)) * ":" * json_value(v))
        end
        return "{" * join(parts, ",") * "}"
    else
        return json_escape(string(x))
    end
end

function scalar_string(x)
    return string(x)
end

function maybe_float(x)
    try
        return Float64(x)
    catch
        return nothing
    end
end

components = Any[]
if !@isdefined(circuit)
    error("Snippet did not define a variable named circuit")
end

for item in circuit
    if length(item) < 4
        error("Circuit item has fewer than 4 entries: $(item)")
    end
    push!(components, Dict(
        "name" => string(item[1]),
        "node1" => string(item[2]),
        "node2" => string(item[3]),
        "value" => scalar_string(item[4]),
    ))
end

defs = Dict{String, Any}()
if @isdefined(circuitdefs)
    for (k, v) in circuitdefs
        defs[string(k)] = scalar_string(v)
    end
end

sim = Dict{String, Any}()
if @isdefined(ws)
    try
        sim["freq_start"] = Float64(first(ws)) / (2*pi*1e9)
        sim["freq_stop"] = Float64(last(ws)) / (2*pi*1e9)
        sim["freq_points"] = length(ws)
    catch err
        sim["ws_warning"] = string(err)
    end
end
if @isdefined(wp)
    vals = []
    try
        for item in wp
            push!(vals, Float64(item) / (2*pi*1e9))
        end
        sim["pump_frequencies"] = vals
    catch err
        sim["wp_warning"] = string(err)
    end
end
let best_src = nothing, best_score = -1.0
    for sym in names(Main)
        try
            val = getfield(Main, sym)
            val isa AbstractVector || continue
            isempty(val) && continue
            item = first(val)
            item isa NamedTuple || continue
            ks = keys(item)
            (:port in ks && :current in ks && :mode in ks) || continue
            score = 0.0
            for src in val
                try; any(!=(0), src.mode) && (score += abs(Float64(src.current))); catch; end
            end
            if score > best_score; best_score = score; best_src = val; end
        catch; end
    end
    if best_src !== nothing
        ports_out = Any[]; currents_out = Any[]; modes_out = Any[]
        try
            for src in best_src
                push!(ports_out, Int(src.port))
                push!(currents_out, scalar_string(src.current))
                push!(modes_out, collect(src.mode))
            end
            sim["source_ports"] = ports_out
            sim["source_currents"] = currents_out
            sim["source_modes"] = modes_out
        catch err
            sim["sources_warning"] = string(err)
        end
    end
end
if @isdefined(Nmodulationharmonics)
    try
        sim["modulation_harmonics"] = [Int(x) for x in Nmodulationharmonics]
    catch err
        sim["modulation_warning"] = string(err)
    end
end
if @isdefined(Npumpharmonics)
    try
        sim["pump_harmonics"] = [Int(x) for x in Npumpharmonics]
    catch err
        sim["pump_harmonics_warning"] = string(err)
    end
end

payload = Dict(
    "components" => components,
    "circuitdefs" => defs,
    "simulation" => sim,
)

open(out_path, "w") do io
    write(io, json_value(payload))
end
'''


def probe_julia_source(source: str, *, timeout: int = 60) -> dict[str, Any]:
    prefix = strip_runtime_section(source)
    with tempfile.TemporaryDirectory(prefix="julia_hb_probe_") as tmp:
        tmp_path = Path(tmp)
        source_path = tmp_path / "snippet_prefix.jl"
        wrapper_path = tmp_path / "probe.jl"
        out_path = tmp_path / "out.json"
        source_path.write_text(prefix, encoding="utf-8")
        wrapper_path.write_text(JULIA_PROBE, encoding="utf-8")
        try:
            result = subprocess.run(
                ["julia", str(wrapper_path), str(source_path), str(out_path)],
                cwd=str(tmp_path),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("Julia executable was not found on PATH.") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"Julia snippet probe timed out after {timeout} seconds.") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(f"Julia snippet probe failed.\n{detail}")
        try:
            data = json.loads(out_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError("Julia snippet probe did not produce valid JSON.") from exc
    data["source_prefix"] = prefix
    data.setdefault("warnings", [])
    return data


def primitive_type_from_name(name: str) -> str:
    raw = str(name)
    if raw == "K":
        return "K"
    for primitive in ("Lj", "Cj", "NL", "P", "R", "L", "C", "I"):
        if raw.startswith(primitive):
            return primitive
    match = re.match(r"([A-Za-z]+)", raw)
    if match:
        return match.group(1)
    return raw


def make_uid(name: str, used: set[str]) -> str:
    base = clean_name(name) or "U"
    uid = base
    idx = 2
    while uid in used:
        uid = f"{base}_{idx}"
        idx += 1
    used.add(uid)
    return uid


def generated_symbol(port_names: list[str]) -> dict[str, Any]:
    return {
        "shape": "rectangle",
        "width": 160,
        "height": max(70, 26 * max(2, len(port_names))),
        "label_position": "center",
        "show_type_name": True,
        "show_uid": True,
        "port_layout": [
            {
                "port": port,
                "side": "left" if idx % 2 == 0 else "right",
                "position": (idx + 1) / (len(port_names) + 1),
                "label_visible": True,
            }
            for idx, port in enumerate(port_names)
        ],
    }


def strip_julia_comments(source: str) -> str:
    out: list[str] = []
    in_block = 0
    in_string = False
    triple = False
    i = 0
    while i < len(source):
        if in_block:
            if source.startswith("#=", i):
                in_block += 1
                i += 2
                continue
            if source.startswith("=#", i):
                in_block -= 1
                i += 2
                continue
            out.append("\n" if source[i] == "\n" else " ")
            i += 1
            continue
        if not in_string and source.startswith("#=", i):
            in_block = 1
            i += 2
            continue
        ch = source[i]
        if ch == '"' and (i == 0 or source[i - 1] != "\\"):
            if source.startswith('"""', i):
                triple = not triple
                in_string = triple
                out.append('"""')
                i += 3
                continue
            if not triple:
                in_string = not in_string
        if not in_string and ch == "#":
            while i < len(source) and source[i] != "\n":
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def split_top_level(text: str, sep: str = ",") -> list[str]:
    parts: list[str] = []
    start = 0
    depth = 0
    in_string = False
    quote = ""
    i = 0
    while i < len(text):
        ch = text[i]
        if in_string:
            if ch == quote and (i == 0 or text[i - 1] != "\\"):
                in_string = False
            i += 1
            continue
        if ch in {'"', "'"}:
            in_string = True
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        elif ch == sep and depth == 0:
            parts.append(text[start:i].strip())
            start = i + 1
        i += 1
    tail = text[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def find_matching(text: str, open_idx: int, open_ch: str, close_ch: str) -> int:
    depth = 0
    in_string = False
    quote = ""
    i = open_idx
    while i < len(text):
        ch = text[i]
        if in_string:
            if ch == quote and text[i - 1] != "\\":
                in_string = False
            i += 1
            continue
        if ch in {'"', "'"}:
            in_string = True
            quote = ch
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def assignment_value(source: str, name: str, before: int | None = None) -> str:
    limit = len(source) if before is None else max(0, before)
    matches = list(re.finditer(rf"(?m)^\s*{re.escape(name)}\s*=", source[:limit]))
    if not matches:
        return ""
    start = matches[-1].end()
    while start < len(source) and source[start].isspace():
        start += 1
    if start >= len(source):
        return ""
    first_line_end = source.find("\n", start, limit)
    first_line_end = limit if first_line_end == -1 else first_line_end
    bracket_candidates = [idx for idx in (source.find("[", start, limit), source.find("(", start, limit)) if idx != -1]
    if source[start] in "[(" or (bracket_candidates and min(bracket_candidates) < first_line_end):
        value_start = start if source[start] in "[(" else min(bracket_candidates)
        close = "]" if source[value_start] == "[" else ")"
        end = find_matching(source, value_start, source[value_start], close)
        return source[start:end + 1].strip() if end != -1 else source[start:limit].strip()
    lines = source[start:limit].splitlines()
    return lines[0].strip() if lines else ""


def simple_assignments(source: str, before: int | None = None) -> dict[str, str]:
    limit = len(source) if before is None else max(0, before)
    values: dict[str, str] = {}
    for match in re.finditer(r"(?m)^\s*(?:const\s+|global\s+|local\s+)?([A-Za-z_]\w*)\s*=\s*(.+)$", source[:limit]):
        name, expr = match.group(1), match.group(2).strip()
        if name in {"networks", "connections", "circuit", "sources"}:
            continue
        if any(ch in expr for ch in "[("):
            values[name] = assignment_value(source, name, limit) or expr
        else:
            values[name] = expr
    return values


def parse_function_blocks(source: str) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    starts: list[dict[str, Any]] = []
    for match in re.finditer(r"(?m)^\s*function\s+([A-Za-z_]\w*)\s*\(", source):
        open_idx = source.find("(", match.start())
        close_idx = find_matching(source, open_idx, "(", ")")
        if close_idx == -1:
            continue
        starts.append({"match": match, "body_start": close_idx + 1, "args": source[open_idx + 1:close_idx]})
    for idx, start_info in enumerate(starts):
        match = start_info["match"]
        search_end = starts[idx + 1]["match"].start() if idx + 1 < len(starts) else len(source)
        depth = 1
        body_start = start_info["body_start"]
        end_pos = search_end
        for line_match in re.finditer(r"(?m)^\s*(function\b|if\b|for\b|while\b|let\b|begin\b|try\b|quote\b|end\b)", source[body_start:search_end]):
            token = line_match.group(1)
            if token == "end":
                depth -= 1
                if depth == 0:
                    end_pos = body_start + line_match.start()
                    break
            else:
                depth += 1
        blocks.append({
            "name": match.group(1),
            "args": start_info["args"],
            "body": source[body_start:end_pos],
            "start": match.start(),
            "end": end_pos,
        })
    return blocks


def parse_function_parameters(args_text: str) -> dict[str, str]:
    text = str(args_text or "").replace("\n", " ")
    text = text.replace(";", ",")
    params: dict[str, str] = {}
    for part in split_top_level(text):
        part = part.strip()
        if not part or part.endswith("..."):
            continue
        if "::" in part:
            part = part.split("::", 1)[0].strip()
        if "=" in part:
            name, default = part.split("=", 1)
            name = clean_name(name.strip())
            if name and name != "w":
                params[name] = default.strip()
    return params


def call_parameters_from_expr(expr: str, function_params: dict[str, str]) -> dict[str, str]:
    callee = call_name(expr)
    function_values = function_params.get(clean_name(callee), {}) if callee else {}
    if not function_values:
        return {}
    open_idx = str(expr).find("(")
    close_idx = find_matching(str(expr), open_idx, "(", ")") if open_idx != -1 else -1
    if close_idx == -1:
        return dict(function_values)
    raw_args = str(expr)[open_idx + 1:close_idx].replace(";", ",")
    args = split_top_level(raw_args)
    params = dict(function_values)
    positional_names = list(function_values)
    positional_idx = 0
    for arg in args:
        item = arg.strip()
        if not item or item == "w" or item.endswith("..."):
            continue
        if "=" in item:
            key, value = item.split("=", 1)
            key = clean_name(key.strip())
            if key in params:
                params[key] = value.strip()
        elif positional_idx < len(positional_names):
            params[positional_names[positional_idx]] = item
            positional_idx += 1
    return {key: value for key, value in params.items() if key and value not in [None, ""]}


def parse_named_tuple_entries(text: str) -> list[tuple[str, str]]:
    inner = text.strip()
    if inner.startswith("[") and inner.endswith("]"):
        inner = inner[1:-1]
    entries: list[tuple[str, str]] = []
    for match in re.finditer(r'\(\s*"([^"]+)"\s*,', inner):
        open_idx = match.start()
        close_idx = find_matching(inner, open_idx, "(", ")")
        if close_idx == -1:
            continue
        tuple_text = inner[open_idx + 1:close_idx]
        parts = split_top_level(tuple_text)
        if len(parts) >= 2:
            entries.append((parts[0].strip().strip('"'), parts[1].strip()))
    return entries


def parse_connection_entries(text: str) -> list[list[tuple[str, int]]]:
    inner = text.strip()
    if not inner.startswith("[") and "[" in inner:
        inner = inner[inner.find("["):]
    if inner.startswith("[") and inner.endswith("]"):
        inner = inner[1:-1]
    connections: list[list[tuple[str, int]]] = []
    direct = [(name, int(port)) for name, port in re.findall(r'\(\s*"([^"]+)"\s*,\s*(\d+)\s*\)', inner)]
    if len(direct) >= 2 and "[" not in inner:
        return [direct]
    i = 0
    while i < len(inner):
        if inner[i] != "[":
            i += 1
            continue
        end = find_matching(inner, i, "[", "]")
        if end == -1:
            break
        endpoint_text = inner[i + 1:end]
        endpoints: list[tuple[str, int]] = []
        for name, port in re.findall(r'\(\s*"([^"]+)"\s*,\s*(\d+)\s*\)', endpoint_text):
            endpoints.append((name, int(port)))
        if len(endpoints) >= 2:
            connections.append(endpoints)
        i = end + 1
    return connections


def parse_push_network_entries(source: str, before: int | None = None) -> list[tuple[str, str]]:
    limit = len(source) if before is None else max(0, before)
    entries: list[tuple[str, str]] = []
    for match in re.finditer(r"push!\s*\(\s*networks\s*,", source[:limit]):
        open_idx = source.find("(", match.start())
        close_idx = find_matching(source, open_idx, "(", ")")
        if close_idx == -1:
            continue
        args = split_top_level(source[open_idx + 1:close_idx])
        if len(args) >= 2:
            entries.extend(parse_named_tuple_entries(args[1]))
    return entries


def parse_push_connection_entries(source: str, before: int | None = None) -> list[list[tuple[str, int]]]:
    limit = len(source) if before is None else max(0, before)
    connections: list[list[tuple[str, int]]] = []
    for match in re.finditer(r"push!\s*\(\s*connections\s*,", source[:limit]):
        open_idx = source.find("(", match.start())
        close_idx = find_matching(source, open_idx, "(", ")")
        if close_idx == -1:
            continue
        args = split_top_level(source[open_idx + 1:close_idx])
        if len(args) >= 2:
            connections.extend(parse_connection_entries(args[1]))
    return connections


def call_name(expr: str) -> str:
    text = expr.strip()
    text = re.sub(r"^\s*@\w+\s+", "", text)
    match = re.search(r"([A-Za-z_]\w*)\s*\(", text)
    return match.group(1) if match else ""


def builtin_from_expr(expr: str) -> tuple[str, list[str]] | None:
    match = re.search(r"JosephsonCircuits\.(ABCD_[A-Za-z0-9_]+|S_[A-Za-z0-9_]+)\s*\(", expr)
    if not match:
        match = re.search(r"\b(ABCD_[A-Za-z0-9_]+|S_[A-Za-z0-9_]+)\s*\(", expr)
    if not match:
        return None
    name = match.group(1)
    open_idx = expr.find("(", match.end() - 1)
    close_idx = find_matching(expr, open_idx, "(", ")") if open_idx != -1 else -1
    args = split_top_level(expr[open_idx + 1:close_idx]) if close_idx != -1 else []
    return name, args


def find_loop_body_assignment(var_name: str, source: str) -> str:
    """Find `var_name[...] = expr` in source and return the full RHS expr.

    Handles multiline calls like:
        S_inductor[:,:,i] = JosephsonCircuits.AtoS(
            JosephsonCircuits.ABCD_seriesZ(im * w[i] * L_inductor))
    by following parentheses to find the complete expression.
    """
    pattern = rf"(?m)^\s*{re.escape(var_name)}\s*\[.*?\]\s*=\s*(.+)"
    m = re.search(pattern, source)
    if not m:
        return ""
    rhs_start = m.start(1)
    rhs_first_line = m.group(1).strip().split("#", 1)[0].strip()

    # If the first line contains an unclosed paren, follow it across lines
    if "(" in rhs_first_line:
        open_idx = source.find("(", rhs_start)
        if open_idx != -1:
            close_idx = find_matching(source, open_idx, "(", ")")
            if close_idx != -1:
                return source[rhs_start : close_idx + 1].strip()

    return rhs_first_line


JULIA_PLOT_RE = re.compile(r"\b(plot[!]?|scatter[!]?|savefig|display|heatmap[!]?)\s*\(")


def is_direct_s_matrix_function(body: str) -> bool:
    """Return True for functions that compute S-matrices via ABCD loops without solveS."""
    if re.search(r"\b(solveS|hbsolve)\s*\(", body):
        return False
    return bool(
        re.search(r"\bzeros\s*\(\s*ComplexF64\s*,", body)
        and re.search(r"\bS\s*\[", body)
    )


def s_matrix_port_count(body: str) -> int | None:
    m = re.search(r"zeros\s*\(\s*ComplexF64\s*,\s*(\d+)\s*,", body)
    return int(m.group(1)) if m else None


def categorize_block(body: str) -> tuple[str, str]:
    """Return (category, reason) for a function body.
    Categories: 'solveS', 'direct_s', 'plotting', 'other'
    """
    if re.search(r"\b(solveS|hbsolve)\s*\(", body):
        return "solveS", "contains solveS/hbsolve call"
    if is_direct_s_matrix_function(body):
        return "direct_s", "computes S-matrix directly via ABCD loop (no solveS)"
    if JULIA_PLOT_RE.search(body):
        return "plotting", "plotting/display function"
    return "other", "no simulation call detected"


def build_direct_s_cell(
    name: str,
    body: str,
    function_params: dict[str, dict[str, str]],
    global_source: str = "",
    args_text: str = "w",
) -> dict[str, Any]:
    """Build a visible matrix cell for a function that directly computes an S-matrix."""
    port_count = s_matrix_port_count(body) or 2
    port_names = [f"p{i + 1}" for i in range(port_count)]
    params = {k: v for k, v in (function_params.get(clean_name(name), {})).items() if k != "w"}
    variables = [
        {"name": clean_name(k), "default": str(v), "value": str(v), "scope": "cell", "export": False}
        for k, v in params.items()
        if clean_name(k)
    ]
    clean = clean_name(name) or "generated_s"
    keyword_args = ", ".join(f"{clean_name(k)}={clean_name(k)}" for k in params if clean_name(k))
    call_suffix = f"; {keyword_args}" if keyword_args else ""
    definitions = f"function {clean}({args_text})\n{body.rstrip()}\nend"
    return {
        "name": clean,
        "type": "matrix",
        "port_count": len(port_names),
        "port_names": port_names,
        "matrix_type": "S",
        "matrix_definitions": definitions,
        "matrix_values": f"{clean}([w]{call_suffix})[:, :, 1]",
        "variables": variables,
        "simulation": {"z0": 50.0},
        "symbol": generated_symbol(port_names),
        "symbol_port_layout": generated_symbol(port_names)["port_layout"],
        "generated_from": "julia_direct_s",
        "generated_source": definitions + "\n",
    }


def port_symbol(port_names: list[str]) -> dict[str, Any]:
    return generated_symbol(port_names)


def schematic_instance(
    type_name: str,
    uid: str,
    port_names: list[str],
    *,
    parameters: dict[str, str] | None = None,
    position: list[float] | None = None,
) -> dict[str, Any]:
    params = parameters or {}
    symbol = port_symbol(port_names)
    return {
        "type_name": type_name,
        "uid": uid,
        "parameters": params,
        "parameter_order": list(params),
        "parameter_kinds": {key: "positional" for key in params},
        "position": position or [0, 0],
        "port_count": len(port_names),
        "port_names": port_names,
        "rotation_degrees": 0,
        "repeat_count": 1,
        "repeat_connections": [],
        "symbol_port_layout": symbol["port_layout"],
        "symbol": symbol,
    }


def auto_position(index: int, port_count: int = 2) -> list[float]:
    width = 160.0
    height = max(70.0, 26.0 * max(2, port_count))
    margin = 10.0
    gap = 10.0
    return [margin + width / 2.0 + index * (width + gap), margin + height / 2.0]


def normalize_cell_layout(cell: dict[str, Any], margin: float = 10.0) -> dict[str, Any]:
    instances = cell.get("instances", []) or []
    if not instances:
        return cell
    left = float("inf")
    top = float("inf")
    for inst in instances:
        symbol = inst.get("symbol") or generated_symbol(inst.get("port_names", []) or [])
        width = float(symbol.get("width", 120) or 120)
        height = float(symbol.get("height", 70) or 70)
        pos = inst.get("position", [0, 0])
        try:
            x, y = float(pos[0]), float(pos[1])
        except (TypeError, ValueError, IndexError):
            x, y = 0.0, 0.0
        left = min(left, x - width / 2.0)
        top = min(top, y - height / 2.0)
    dx = margin - left if left < margin else 0.0
    dy = margin - top if top < margin else 0.0
    if dx or dy:
        for inst in instances:
            pos = inst.get("position", [0, 0])
            try:
                x, y = float(pos[0]), float(pos[1])
            except (TypeError, ValueError, IndexError):
                x, y = 0.0, 0.0
            inst["position"] = [round(x + dx, 3), round(y + dy, 3)]
    return cell


def first_number(text: Any, default: float | None = None) -> float | None:
    match = re.search(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?", str(text or ""))
    if not match:
        return default
    try:
        return float(match.group(0))
    except ValueError:
        return default


def int_from_expr(text: Any, default: int | None = None) -> int | None:
    value = first_number(text, None)
    return int(value) if value is not None else default


def frequency_ghz_from_expr(text: Any) -> float | None:
    expr = str(text or "")
    coeff_match = re.search(r"([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)\s*\*?\s*1?e9\b", expr)
    if coeff_match:
        try:
            return float(coeff_match.group(1))
        except ValueError:
            pass
    value = first_number(expr, None)
    if value is None:
        return None
    if abs(value) > 1e6:
        return value / (2 * 3.141592653589793 * 1e9)
    return value


def frequency_settings_from_assignments(assignments: dict[str, str]) -> dict[str, Any]:
    settings: dict[str, Any] = {}
    expanded_assignments = dict(assignments)
    for name, expr in list(assignments.items()):
        for dep_name, dep_expr in assignments.items():
            if dep_name == name:
                continue
            if re.search(rf"\b{re.escape(dep_name)}\b", expr):
                expanded_assignments[name] = re.sub(rf"\b{re.escape(dep_name)}\b", f"({dep_expr})", expanded_assignments[name])
    candidates = [
        str(expanded_assignments.get(key, ""))
        for key in ("ws", "w", "freqs", "frequencies", "frequency")
        if expanded_assignments.get(key)
    ]
    candidates.extend(
        str(expr)
        for name, expr in expanded_assignments.items()
        if re.search(r"(range|LinRange|GHz|1e9|10\^9)", str(expr), re.IGNORECASE)
    )
    for expr in candidates:
        range_match = re.search(
            r"range\s*\(\s*([^,\n]+)\s*,\s*([^,\n\)]+).*?\blength\s*=\s*([^\),]+)",
            expr,
            re.DOTALL,
        )
        if not range_match:
            range_match = re.search(
                r"range\s*\(\s*start\s*=\s*([^,\n]+).*?stop\s*=\s*([^,\n]+).*?length\s*=\s*([^\),]+)",
                expr,
                re.DOTALL,
            )
        if range_match:
            start = first_number(range_match.group(1))
            stop = first_number(range_match.group(2))
            points = int_from_expr(range_match.group(3))
            if start is not None:
                settings["simulation_freq_start"] = start
            if stop is not None:
                settings["simulation_freq_stop"] = stop
            if points is not None:
                settings["simulation_freq_points"] = points
            break
        linspace_match = re.search(r"LinRange\s*\(\s*([^,\n]+)\s*,\s*([^,\n]+)\s*,\s*([^\)]+)\)", expr, re.DOTALL)
        if linspace_match:
            start = first_number(linspace_match.group(1))
            stop = first_number(linspace_match.group(2))
            points = int_from_expr(linspace_match.group(3))
            if start is not None:
                settings["simulation_freq_start"] = start
            if stop is not None:
                settings["simulation_freq_stop"] = stop
            if points is not None:
                settings["simulation_freq_points"] = points
            break
        colon_match = re.search(
            r"([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)\s*:\s*"
            r"([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)\s*:\s*"
            r"([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)",
            expr,
        )
        if colon_match:
            start = first_number(colon_match.group(1))
            step = first_number(colon_match.group(2))
            stop = first_number(colon_match.group(3))
            if start is not None:
                settings["simulation_freq_start"] = start
            if stop is not None:
                settings["simulation_freq_stop"] = stop
            if start is not None and stop is not None and step not in (None, 0):
                settings["simulation_freq_points"] = int(abs((stop - start) / step)) + 1
            break
    return settings


def hb_settings_from_assignments(assignments: dict[str, str]) -> dict[str, Any]:
    settings: dict[str, Any] = {}
    if assignments.get("wp"):
        pump_freqs = [
            value
            for value in (frequency_ghz_from_expr(item) for item in split_top_level(str(assignments.get("wp", "")).strip("[]()")))
            if value is not None
        ]
        if pump_freqs:
            settings["hb_pump_frequencies"] = pump_freqs
    if assignments.get("Nmodulationharmonics"):
        value = int_from_expr(assignments.get("Nmodulationharmonics"))
        if value is not None:
            settings["hb_modulation_harmonics"] = [value]
    if assignments.get("Npumpharmonics"):
        value = int_from_expr(assignments.get("Npumpharmonics"))
        if value is not None:
            settings["hb_pump_harmonics"] = [value]
    return settings


def parse_call_arguments(source: str, call_match: re.Match[str]) -> list[str]:
    open_idx = source.find("(", call_match.start())
    close_idx = find_matching(source, open_idx, "(", ")")
    if open_idx == -1 or close_idx == -1:
        return []
    return split_top_level(source[open_idx + 1:close_idx])


def hb_settings_from_call_args(args: list[str]) -> dict[str, Any]:
    settings: dict[str, Any] = {}
    keyword_values: dict[str, str] = {}
    for arg in args:
        items = split_top_level(arg.replace(";", ",")) if ";" in arg else [arg]
        for item in items:
            if "=" not in item:
                continue
            key, value = item.split("=", 1)
            keyword_values[clean_name(key.strip().lstrip(";"))] = value.strip()
    if "Nmodulationharmonics" in keyword_values:
        value = int_from_expr(keyword_values["Nmodulationharmonics"])
        if value is not None:
            settings["hb_modulation_harmonics"] = [value]
    if "Npumpharmonics" in keyword_values:
        value = int_from_expr(keyword_values["Npumpharmonics"])
        if value is not None:
            settings["hb_pump_harmonics"] = [value]
    for source_key, target_key in (
        ("threewavemixing", "hb_threewave_mixing"),
        ("fourwavemixing", "hb_fourwave_mixing"),
    ):
        if source_key in keyword_values:
            settings[target_key] = keyword_values[source_key].lower() not in {"false", "0", "no"}
    return settings


def build_reverse_solve_cell(
    name: str,
    networks: list[tuple[str, str]],
    connections: list[list[tuple[str, int]]],
    assignments: dict[str, str],
    solve_cells: set[str],
    *,
    source: str,
    mode: str = "solveS",
    call_args: list[str] | None = None,
    exported_defaults: dict[str, str] | None = None,
    function_params: dict[str, dict[str, str]] | None = None,
    port_lookup: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    used: set[str] = set()
    instances: list[dict[str, Any]] = []
    uid_by_network: dict[str, str] = {}
    port_names_by_network: dict[str, list[str]] = {}
    inst_idx = 0
    for _idx, (network_name, expr) in enumerate(networks):
        # Strip .S field access: Julia solveS returns a struct; .S accesses its S-matrix.
        # e.g. ("F1_ws", S_ws_sol.S) -> resolve S_ws_sol; or S_tandem0 = sol_tandem.S -> chase two levels.
        actual_expr = expr[:-2] if expr.endswith(".S") else expr
        resolved = assignments.get(actual_expr, assignments.get(expr, expr))
        if resolved.endswith(".S"):
            base = resolved[:-2]
            resolved = assignments.get(base, base)

        builtin = builtin_from_expr(resolved)
        ports: list[str] = ["p1", "p2"]
        params: dict[str, str] = {}

        if builtin:
            type_name, args = builtin
            ports = BUILTIN_S_SOLVE_PORTS.get(type_name, ["p1", "p2"])
            param_names = BUILTIN_S_SOLVE_PARAMS.get(type_name, [f"arg{i + 1}" for i in range(len(args))])
            params = {param_names[i] if i < len(param_names) else f"arg{i + 1}": arg for i, arg in enumerate(args)}
        else:
            callee = call_name(resolved)

            # stdlib array-init (zeros, ones, …): try recovering from loop body
            if callee in JULIA_ARRAY_INIT_FNS:
                loop_expr = find_loop_body_assignment(actual_expr, source)
                if loop_expr:
                    loop_builtin = builtin_from_expr(loop_expr)
                    if loop_builtin:
                        builtin = loop_builtin
                        type_name, args = builtin
                        ports = BUILTIN_S_SOLVE_PORTS.get(type_name, ["p1", "p2"])
                        param_names = BUILTIN_S_SOLVE_PARAMS.get(type_name, [f"arg{i + 1}" for i in range(len(args))])
                        params = {param_names[i] if i < len(param_names) else f"arg{i + 1}": arg for i, arg in enumerate(args)}
                    else:
                        callee = call_name(loop_expr)
                        resolved = loop_expr
                # If still no builtin and no callee → unresolvable, skip
                if not builtin and not callee:
                    continue

            if not builtin:
                # Bare variable (function parameter / raw S-matrix) — skip
                if not callee:
                    continue
                # Inner solveS/hbsolve call: use the variable name before .S stripping as type
                if callee in {"solveS", "hbsolve"}:
                    type_name = clean_name(actual_expr) or clean_name(network_name)
                else:
                    type_name = clean_name(callee) if callee and clean_name(callee) in solve_cells else clean_name(callee or actual_expr or network_name)
                params = call_parameters_from_expr(resolved, function_params or {})
                # Use port count from previously-imported cells of this type if available
                if port_lookup and type_name in port_lookup:
                    ports = [f"p{i + 1}" for i in range(len(port_lookup[type_name]))]

        uid = make_uid(network_name, used)
        uid_by_network[network_name] = uid
        port_names_by_network[network_name] = ports
        instances.append(schematic_instance(type_name, uid, ports, parameters=params, position=auto_position(inst_idx, len(ports))))
        inst_idx += 1

    wires: list[dict[str, Any]] = []
    labels: list[dict[str, Any]] = []
    connected: set[tuple[str, int]] = set()
    for idx, group in enumerate(connections, start=1):
        if len(group) < 2:
            continue
        root = group[0]
        if root[0] in uid_by_network:
            labels.append(
                {
                    "name": f"n_{idx}",
                    "instance_uid": uid_by_network[root[0]],
                    "port": f"p{root[1]}",
                    "position": [0, 0],
                }
            )
        for target in group[1:]:
            if root[0] in uid_by_network and target[0] in uid_by_network:
                wires.append({
                    "source_instance_uid": uid_by_network[root[0]],
                    "source_port": f"p{root[1]}",
                    "target_instance_uid": uid_by_network[target[0]],
                    "target_port": f"p{target[1]}",
                    "name": f"n_{idx}",
                    "gui_wire_id": f"wire_{idx}",
                })
        connected.update(group)

    pins: list[dict[str, Any]] = []
    exposed: list[tuple[str, int]] = []
    for network_name, _expr in networks:
        if network_name not in uid_by_network:
            continue  # was skipped (stdlib init / bare variable)
        for port_idx in range(1, len(port_names_by_network.get(network_name, ["p1", "p2"])) + 1):
            endpoint = (network_name, port_idx)
            if endpoint not in connected:
                exposed.append(endpoint)
    for idx, (network_name, port_idx) in enumerate(exposed, start=1):
        pins.append({"name": f"P{idx}", "instance_uid": uid_by_network[network_name], "port": f"p{port_idx}"})

    variables_by_name: dict[str, dict[str, Any]] = {}
    for var, expr in sorted((exported_defaults or {}).items()):
        clean_var = clean_name(var)
        if clean_var:
            variables_by_name[clean_var] = {
                "name": clean_var,
                "default": str(expr),
                "value": str(expr),
                "scope": "cell",
                "export": True,
            }
    for var, expr in sorted(assignments.items()):
        if var in {"networks", "connections", "ws", "w", "freqs", "frequencies", "frequency"} or not clean_name(var):
            continue
        token = normalize_import_expr(expr)
        if looks_numeric(token):
            clean_var = clean_name(var)
            variables_by_name.setdefault(clean_var, {
                "name": clean_var,
                "default": token,
                "value": token,
                "scope": "cell",
                "export": False,
            })
    dependency_symbols: set[str] = set()
    for inst in instances:
        for expr in (inst.get("parameters", {}) or {}).values():
            dependency_symbols.update(symbols_in_expr(expr))
    dependency_symbols.discard("w")
    for var in sorted(dependency_symbols):
        clean_var = clean_name(var)
        if not clean_var or clean_var in variables_by_name or var not in assignments:
            continue
        default, export = fallback_variable_default(assignments[var])
        variables_by_name[clean_var] = {
            "name": clean_var,
            "default": default,
            "value": default,
            "scope": "cell",
            "export": export,
        }
    variables = list(variables_by_name.values())

    clean = clean_name(name) or "imported_simulation"
    sim_settings = frequency_settings_from_assignments(assignments)
    hb_settings = hb_settings_from_assignments(assignments) if mode == "hbsolve" else {}
    if mode == "hbsolve":
        hb_settings.update(hb_settings_from_call_args(call_args or []))
    cell = {
        "name": clean,
        "type": "schematic",
        "instances": instances,
        "wires": wires,
        "pins": pins,
        "labels": labels,
        "simulation_variables": variables,
        "simulation": {
            "z0": 50.0,
            "hb": {
                "top_block": mode == "hbsolve",
                "pump_ports": [],
                "pump_frequencies": hb_settings.get("hb_pump_frequencies", []),
                "pump_currents": [],
                "dc_ports": [],
                "dc_currents": [],
                "modulation_harmonics": hb_settings.get("hb_modulation_harmonics", [10]),
                "pump_harmonics": hb_settings.get("hb_pump_harmonics", [20]),
                "threewave_mixing": hb_settings.get("hb_threewave_mixing", True),
                "fourwave_mixing": hb_settings.get("hb_fourwave_mixing", True),
            },
        },
        "simulation_mode": mode,
        "simulation_input_ports": [pins[0]["name"]] if pins else [],
        "simulation_output_ports": [pins[-1]["name"]] if pins else [],
        "simulation_freq_start": sim_settings.get("simulation_freq_start", 2.0),
        "simulation_freq_stop": sim_settings.get("simulation_freq_stop", 20.0),
        "simulation_freq_points": sim_settings.get("simulation_freq_points", 200),
        "simulation_sweep_type": "linear",
        "simulation_figure_title": clean,
        "generated_from": "julia_reverse_import",
        "generated_source": extract_circuit_source(source),
    }
    return normalize_cell_layout(cell)


def build_wrapper_cell(
    name: str,
    callees: list[str],
    port_lookup: dict[str, list[str]],
    *,
    source: str,
    function_params: dict[str, dict[str, str]] | None = None,
    exported_defaults: dict[str, str] | None = None,
) -> dict[str, Any]:
    used: set[str] = set()
    instances: list[dict[str, Any]] = []
    pins: list[dict[str, Any]] = []
    pin_idx = 1
    for idx, callee in enumerate(callees):
        ports = port_lookup.get(callee, ["P1", "P2"])
        uid = make_uid(callee, used)
        params = call_parameters_from_expr(f"{callee}()", function_params or {})
        instances.append(schematic_instance(callee, uid, ports, parameters=params, position=auto_position(idx, len(ports))))
        for port in ports:
            pins.append({"name": f"P{pin_idx}", "instance_uid": uid, "port": port})
            pin_idx += 1
    clean = clean_name(name) or "imported_wrapper"
    sim_settings = frequency_settings_from_assignments(simple_assignments(source))
    variables = [
        {
            "name": clean_name(var),
            "default": str(value),
            "value": str(value),
            "scope": "cell",
            "export": True,
        }
        for var, value in sorted((exported_defaults or {}).items())
        if clean_name(var)
    ]
    cell = {
        "name": clean,
        "type": "schematic",
        "instances": instances,
        "wires": [],
        "pins": pins,
        "labels": [],
        "simulation_variables": variables,
        "simulation": {"z0": 50.0, "hb": {"top_block": False}},
        "simulation_mode": "solveS",
        "simulation_input_ports": [pins[0]["name"]] if pins else [],
        "simulation_output_ports": [pins[-1]["name"]] if pins else [],
        "simulation_freq_start": sim_settings.get("simulation_freq_start", 2.0),
        "simulation_freq_stop": sim_settings.get("simulation_freq_stop", 20.0),
        "simulation_freq_points": sim_settings.get("simulation_freq_points", 200),
        "simulation_sweep_type": "linear",
        "simulation_figure_title": clean,
        "generated_from": "julia_reverse_import",
        "generated_source": _extract_wrapper_source(source, callees),
    }
    return normalize_cell_layout(cell)


def _extract_wrapper_source(source: str, callees: list[str]) -> str:
    """Return only the lines of source that call or reference the callee functions."""
    callee_set = {c for c in callees if c}
    lines_out: list[str] = []
    for line in strip_runtime_section(source).splitlines():
        stripped = line.strip()
        if not stripped or stripped == "end":
            continue
        if any(re.search(rf"\b{re.escape(c)}\s*[(\[]", line) for c in callee_set):
            lines_out.append(line)
        elif any(re.search(rf"\b{re.escape(c)}\b", line) for c in callee_set):
            lines_out.append(line)
    return "\n".join(lines_out).strip() + "\n" if lines_out else strip_runtime_section(source)


def called_imported_cells(text: str, available: set[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r"\b([A-Za-z_]\w*)\s*\(", text):
        name = clean_name(match.group(1))
        if name in available and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def copy_hb_top_settings_to_child_instances(cells: list[dict[str, Any]]) -> None:
    hb_cells = {
        clean_name(str(cell.get("name", ""))): cell
        for cell in cells
        if (cell.get("simulation") or {}).get("hb", {}).get("top_block") or cell.get("hb_top_block")
    }
    if not hb_cells:
        return
    for cell in cells:
        for inst in cell.get("instances", []) or []:
            ref = hb_cells.get(clean_name(str(inst.get("type_name", ""))))
            if not ref:
                continue
            inst["hb"] = json.loads(json.dumps((ref.get("simulation") or {}).get("hb") or {}))


def imported_cell_port_names(cell: dict[str, Any]) -> list[str]:
    if cell.get("type") == "matrix":
        ports = [str(p) for p in cell.get("port_names", []) if str(p)]
        if ports:
            return ports
    pins = [str(pin.get("name")) for pin in cell.get("pins", []) if str(pin.get("name", ""))]
    return pins or ["P1", "P2"]


def parse_solve_call_cells(source: str) -> dict[str, Any]:
    """Return {"cells": [...], "skipped": [...]} for all simulation functions in source."""
    clean_source = strip_julia_comments(source)
    blocks = parse_function_blocks(clean_source)
    global_source_chars = list(clean_source)
    for block in blocks:
        for idx in range(block["start"], min(block["end"], len(global_source_chars))):
            global_source_chars[idx] = " "
    global_source = "".join(global_source_chars)
    global_assignments = simple_assignments(global_source)
    function_params = {
        clean_name(block["name"]): parse_function_parameters(block.get("args", ""))
        for block in blocks
    }
    solve_blocks = {
        clean_name(block["name"])
        for block in blocks
        if re.search(r"\b(solveS|hbsolve)\s*\(", block["body"])
    }
    cells: list[dict[str, Any]] = []
    used_names: set[str] = set()
    # Running port lookup: maps cell name → port name list for cells created so far.
    # Passed to build_reverse_solve_cell so it can use the right port count when
    # a network expression resolves to a previously-imported custom cell.
    running_port_lookup: dict[str, list[str]] = {}

    # Pass 1: direct S-matrix functions (zeros + ABCD loop, no solveS)
    for block in blocks:
        body = block["body"]
        block_name = clean_name(block["name"])
        if block_name in solve_blocks:
            continue
        if is_direct_s_matrix_function(body):
            cell_name = make_uid(block_name, used_names)
            new_cell = build_direct_s_cell(cell_name, body, function_params, global_source, args_text=block.get("args", "w"))
            cells.append(new_cell)
            cname = clean_name(str(new_cell.get("name", "")))
            if cname:
                running_port_lookup[cname] = imported_cell_port_names(new_cell)

    # Pass 2: solveS/hbsolve functions
    for block in blocks:
        body = block["body"]
        # Track inner solveS results within this function body.
        # When sol = solveS(...) creates a cell, map "sol" → "cell_name()" so that
        # subsequent solveS calls in the same function can reference it via S_tandem0 = sol.S.
        inner_result_map: dict[str, str] = {}
        for call in re.finditer(r"\b(solveS|hbsolve)\s*\(", body):
            call_mode = call.group(1)
            base_name = clean_name(block["name"]) or f"simulation_{len(cells) + 1}"
            cell_name = make_uid(base_name, used_names)
            # Merge global + local + inner-solve cross-references into assignments.
            assignments = {**global_assignments, **simple_assignments(body, call.start()), **inner_result_map}
            networks_text = assignment_value(body, "networks", call.start())
            connections_text = assignment_value(body, "connections", call.start())
            # If no literal "networks = [...]" found, try parsing from the solveS call args.
            # e.g. solveS(tandem_networks, tandem_connections) → look up those variables.
            if not networks_text or not parse_named_tuple_entries(networks_text):
                call_args_raw = parse_call_arguments(body, call)
                if len(call_args_raw) >= 1:
                    net_var = call_args_raw[0].strip()
                    if re.match(r'^[A-Za-z_]\w*$', net_var):
                        alt = assignment_value(body, net_var, call.start())
                        if alt:
                            networks_text = alt
                if len(call_args_raw) >= 2 and not connections_text:
                    conn_var = call_args_raw[1].strip()
                    if re.match(r'^[A-Za-z_]\w*$', conn_var):
                        alt = assignment_value(body, conn_var, call.start())
                        if alt:
                            connections_text = alt
            networks = parse_named_tuple_entries(networks_text)
            connections = parse_connection_entries(connections_text)
            networks.extend(parse_push_network_entries(body, call.start()))
            connections.extend(parse_push_connection_entries(body, call.start()))
            if networks:
                new_cell = build_reverse_solve_cell(
                    cell_name,
                    networks,
                    connections,
                    assignments,
                    solve_blocks,
                    source=body,
                    mode=call_mode,
                    call_args=parse_call_arguments(body, call),
                    exported_defaults=function_params.get(clean_name(block["name"]), {}),
                    function_params=function_params,
                    port_lookup=running_port_lookup,
                )
                cells.append(new_cell)
                cname = clean_name(str(new_cell.get("name", "")))
                if cname:
                    running_port_lookup[cname] = imported_cell_port_names(new_cell)
                # Record which variable this solveS result was assigned to so that
                # subsequent calls in this function can reference it by name.
                # Pattern: "result_var = solveS(...)" → last non-whitespace before call
                pre_call = body[:call.start()].rstrip()
                assign_m = re.search(r'\b([A-Za-z_]\w*)\s*=\s*$', pre_call)
                if assign_m:
                    result_var = assign_m.group(1)
                    created_name = str(new_cell.get("name", cell_name))
                    # Register both "result_var" and "result_var.S" so .S access resolves
                    inner_result_map[result_var] = created_name + "()"
                    inner_result_map[result_var + ".S"] = created_name + "()"

    imported_names = {clean_name(str(cell.get("name", ""))) for cell in cells}
    port_lookup = {
        clean_name(str(cell.get("name", ""))): imported_cell_port_names(cell)
        for cell in cells
    }

    # Pass 3: wrapper functions that call imported cells
    for block in blocks:
        block_name = clean_name(block["name"])
        if block_name in imported_names:
            continue
        callees = called_imported_cells(block["body"], imported_names)
        if callees:
            cell_name = make_uid(block_name, used_names)
            wrapper = build_wrapper_cell(
                cell_name,
                callees,
                port_lookup,
                source=block["body"],
                function_params=function_params,
                exported_defaults=function_params.get(block_name, {}),
            )
            cells.append(wrapper)
            imported_names.add(clean_name(str(wrapper.get("name", ""))))
            port_lookup[clean_name(str(wrapper.get("name", "")))] = imported_cell_port_names(wrapper)

    top = global_source
    top_result_map: dict[str, str] = {}
    for call in re.finditer(r"\b(solveS|hbsolve)\s*\(", top):
        assignments = {**simple_assignments(top, call.start()), **top_result_map}
        top_networks_text = assignment_value(top, "networks", call.start())
        top_connections_text = assignment_value(top, "connections", call.start())
        if not top_networks_text or not parse_named_tuple_entries(top_networks_text):
            call_args_raw = parse_call_arguments(top, call)
            if len(call_args_raw) >= 1:
                net_var = call_args_raw[0].strip()
                if re.match(r'^[A-Za-z_]\w*$', net_var):
                    alt = assignment_value(top, net_var, call.start())
                    if alt:
                        top_networks_text = alt
            if len(call_args_raw) >= 2 and not top_connections_text:
                conn_var = call_args_raw[1].strip()
                if re.match(r'^[A-Za-z_]\w*$', conn_var):
                    alt = assignment_value(top, conn_var, call.start())
                    if alt:
                        top_connections_text = alt
        networks = parse_named_tuple_entries(top_networks_text)
        connections = parse_connection_entries(top_connections_text)
        networks.extend(parse_push_network_entries(top, call.start()))
        connections.extend(parse_push_connection_entries(top, call.start()))
        if networks:
            # Use the assignment variable name if this solveS is inside a let-block: `var = let ... solveS(...) end`
            pre_top = top[:call.start()].rstrip()
            let_m = re.search(r'\b([A-Za-z_]\w*)\s*=\s*let\b', pre_top)
            # Also handle plain `var = solveS(...)` at top level
            plain_m = re.search(r'\b([A-Za-z_]\w*)\s*=\s*$', pre_top)
            top_base = clean_name((let_m or plain_m).group(1)) if (let_m or plain_m) else "top_simulation"
            cell_name = make_uid(top_base or "top_simulation", used_names)
            new_cell = build_reverse_solve_cell(
                cell_name,
                networks,
                connections,
                assignments,
                solve_blocks,
                source=top,
                mode=call.group(1),
                call_args=parse_call_arguments(top, call),
                exported_defaults={},
                function_params=function_params,
                port_lookup=running_port_lookup,
            )
            cells.append(new_cell)
            cname = clean_name(str(new_cell.get("name", "")))
            if cname:
                running_port_lookup[cname] = imported_cell_port_names(new_cell)
            pre_call = top[:call.start()].rstrip()
            assign_m = re.search(r'\b([A-Za-z_]\w*)\s*=\s*$', pre_call)
            if assign_m:
                result_var = assign_m.group(1)
                created_name = str(new_cell.get("name", cell_name))
                top_result_map[result_var] = created_name + "()"
                top_result_map[result_var + ".S"] = created_name + "()"

    imported_names = {clean_name(str(cell.get("name", ""))) for cell in cells}
    port_lookup = {
        clean_name(str(cell.get("name", ""))): imported_cell_port_names(cell)
        for cell in cells
    }
    top_callees = called_imported_cells(top, imported_names)
    if top_callees:
        cell_name = make_uid("top_import", used_names)
        cells.append(build_wrapper_cell(cell_name, top_callees, port_lookup, source=top, function_params=function_params))

    copy_hb_top_settings_to_child_instances(cells)

    # Collect diagnostics: what wasn't imported and why
    imported_names = {clean_name(str(cell.get("name", ""))) for cell in cells}
    skipped: list[dict[str, str]] = []
    for block in blocks:
        block_name = clean_name(block["name"])
        if block_name in imported_names:
            continue
        _cat, reason = categorize_block(block["body"])
        skipped.append({"name": block["name"], "reason": reason})

    return {"cells": cells, "skipped": skipped}


def import_julia_simulation_hierarchy(source: str, *, name_hint: str = "imported_julia") -> dict[str, Any]:
    """Reverse-import Julia simulation code into ordinary schematic cells."""
    result = parse_solve_call_cells(source)
    cells = result["cells"]
    skipped = result["skipped"]
    if not cells:
        sim_type = detect_julia_simulation_type(source)
        if sim_type == "hb":
            probe = probe_julia_source(source)
            generated = build_generated_cell(name_hint, source, probe)
            cells = [materialize_probe_to_pipeline_cell(generated, probe, {})]
    if not cells:
        raise ValueError("No solveS or hbsolve simulation hierarchy could be extracted from the pasted Julia code.")

    def _cell_kind(c: dict[str, Any]) -> str:
        gf = c.get("generated_from", "")
        if gf == "julia_direct_s":
            return f"direct S-matrix ({len(c.get('pins', []))} ports)"
        if (c.get("simulation") or {}).get("hb", {}).get("top_block") or c.get("hb_top_block"):
            return "hbsolve"
        insts = c.get("instances", [])
        wires = c.get("wires", [])
        if insts and wires:
            return f"solveS ({len(insts)} networks, {len(c.get('pins', []))} ports)"
        if insts and not wires:
            return f"wrapper ({len(insts)} sub-cells)"
        return "solveS (empty)"

    return {
        "cells": cells,
        "skipped": skipped,
        "summary": {
            "cell_count": len(cells),
            "cell_names": [cell.get("name", "") for cell in cells],
            "cell_kinds": {cell.get("name", ""): _cell_kind(cell) for cell in cells},
            "solve_count": sum(1 for c in cells if c.get("simulation_mode") == "solveS"),
            "hbsolve_count": sum(1 for c in cells if c.get("simulation_mode") == "hbsolve" or (c.get("simulation") or {}).get("hb", {}).get("top_block") or c.get("hb_top_block")),
        },
    }


def _display_components_from_probe(probe: dict[str, Any]) -> list[dict[str, Any]]:
    """Return a flat list of components with x/y positions for GUI display."""
    components = probe.get("components", []) or []
    used: set[str] = set()
    uid_by_raw: dict[str, str] = {}
    fake_insts: list[dict[str, Any]] = []
    for comp in components:
        raw = str(comp.get("name", ""))
        if primitive_type_from_name(raw) == "K":
            continue
        uid = make_uid(raw, used)
        uid_by_raw[raw] = uid
        fake_insts.append({"uid": uid, "position": [0, 0]})
    inst_by_uid = {i["uid"]: i for i in fake_insts}
    _assign_layout_positions(components, uid_by_raw, inst_by_uid)
    result = []
    for comp in components:
        raw = str(comp.get("name", ""))
        t = primitive_type_from_name(raw)
        uid = uid_by_raw.get(raw)
        pos = inst_by_uid[uid]["position"] if uid and uid in inst_by_uid else [0, 0]
        result.append({
            "name": raw,
            "type": t,
            "node1": str(comp.get("node1", "")),
            "node2": str(comp.get("node2", "")),
            "value": str(comp.get("value", "")),
            "x": pos[0],
            "y": pos[1],
        })
    return result


def build_generated_cell(name: str, source: str, probe: dict[str, Any] | None = None) -> dict[str, Any]:
    probe = probe or probe_julia_source(source)
    port_names = port_names_from_probe(probe)
    # Probe's evaluated circuitdefs: only take values that evaluate to a number
    # (Julia may return symbolic strings like "Lj / 0.29" for some keys)
    defaults: dict[str, str] = {}
    for key, value in (probe.get("circuitdefs") or {}).items():
        clean_k = clean_name(key)
        if clean_k and looks_numeric(str(value)):
            defaults[clean_k] = str(value)
    # Source-text parse fills any key the probe didn't provide a numeric value for
    source_defs = parse_circuitdefs_from_source(source)
    for key, value in source_defs.items():
        defaults.setdefault(key, value)
    # Any symbol appearing in a component value that still has no entry gets "1.0"
    for comp in probe.get("components", []) or []:
        value = comp.get("value", "")
        if looks_numeric(value):
            continue
        for symbol in symbols_in_expr(value):
            defaults.setdefault(symbol, "1.0")
    # export=False when the default is a concrete number (local override);
    # export=True when it remains symbolic (must be supplied by a parent cell)
    variables = [
        {
            "name": key,
            "default": str(value),
            "value": str(value),
            "scope": "cell",
            "export": not looks_numeric(str(value)),
        }
        for key, value in sorted(defaults.items())
    ]
    sim_defaults = simulation_defaults_from_probe(probe)
    hb = sim_defaults.get("hb", {})
    # The hbsolve call itself is stripped before the probe runs, so parse its
    # keyword args (threewavemixing, fourwavemixing, dc) from the full source.
    hbsolve_kwargs = parse_hbsolve_kwargs_from_source(source)
    for kw, hb_key in (("threewavemixing", "threewave_mixing"), ("fourwavemixing", "fourwave_mixing")):
        if kw in hbsolve_kwargs:
            hb[hb_key] = hbsolve_kwargs[kw].lower() not in ("false", "0", "no")
    display_comps = _display_components_from_probe(probe)
    return {
        "id": str(uuid.uuid4()),
        "name": clean_name(name) or "generated_hb",
        "type": "generated_hb",
        "description": "Trusted Julia-generated JosephsonCircuits HB block",
        "readOnly": False,
        "dirty": True,
        "variables": variables,
        "pins": [
            {"id": str(uuid.uuid4()), "name": port, "order": idx, "net_id": "", "position": [0, 0], "display_visible": True}
            for idx, port in enumerate(port_names, start=1)
        ],
        "labels": [],
        "instances": [],
        "nets": [],
        "generated_language": "julia",
        "generator_kind": "josephsoncircuits_circuit",
        "generated_source": source,
        "generated_summary": summary_from_probe(probe),
        "generated_display_components": display_comps,
        "simulation": sim_defaults,
        "symbol": generated_symbol(port_names),
        "symbol_port_layout": generated_symbol(port_names)["port_layout"],
        "gui": {"version": 1, "viewport": {"zoom": 1, "pan": [0, 0]}, "wire_routes": [], "last_selected": []},
    }


def parse_hbsolve_kwargs_from_source(source: str) -> dict[str, str]:
    """Extract keyword arguments from the first hbsolve call in the source."""
    kwargs: dict[str, str] = {}
    clean = strip_julia_comments(source)
    for match in re.finditer(r"\bhbsolve\s*\(", clean):
        open_idx = clean.find("(", match.start())
        close_idx = find_matching(clean, open_idx, "(", ")")
        if close_idx == -1:
            continue
        for part in split_top_level(clean[open_idx + 1:close_idx]):
            part = part.strip().lstrip(";")
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            key = clean_name(key.strip())
            value = value.strip()
            if key:
                kwargs[key] = value
        break
    return kwargs


def parse_solves_source(source: str) -> dict[str, Any]:
    """Text-parse a solveS Julia snippet to extract port count and variable definitions."""
    port_nums: set[int] = set()

    # Connections reference ports as ("port_name", port_number, ...) where port_number is an int
    # Also look for network entries named "P" or "Port"
    for m in re.finditer(r'"P(?:ort)?"\s*,\s*(\d+)', source):
        port_nums.add(int(m.group(1)))

    # Count distinct push!(networks, ("P", ...)) entries
    p_network_count = len(re.findall(r'push!\s*\(\s*networks\s*,\s*\(\s*"P(?:ort)?"', source))
    if p_network_count and not port_nums:
        port_nums = set(range(1, p_network_count + 1))

    port_count = max(port_nums) if port_nums else 2

    # Extract simple scalar variable definitions: name = number
    variables: dict[str, str] = {}
    skip_names = {"networks", "connections", "using", "import", "include"}
    for m in re.finditer(r'^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([^\n#=][^\n]*)', source, re.MULTILINE):
        name = m.group(1)
        if name in skip_names:
            continue
        value_str = m.group(2).strip().split()[0] if m.group(2).strip().split() else ""
        if looks_numeric(value_str):
            variables[name] = value_str

    return {
        "port_count": port_count,
        "variables": variables,
    }


def build_generated_s_cell(name: str, source: str) -> dict[str, Any]:
    """Build a code-based cell for a Julia solveS circuit."""
    parsed = parse_solves_source(source)
    port_count = parsed["port_count"]
    port_names = [f"P{i + 1}" for i in range(port_count)]
    variables = [
        {"name": k, "default": v, "value": v, "scope": "cell"}
        for k, v in sorted(parsed["variables"].items())
    ]
    clean = clean_name(name) or "generated_s"
    sim = default_simulation(clean)
    return {
        "id": str(uuid.uuid4()),
        "name": clean,
        "type": "generated_s",
        "description": "Trusted Julia-generated JosephsonCircuits S-parameter block",
        "readOnly": False,
        "dirty": True,
        "variables": variables,
        "pins": [
            {"id": str(uuid.uuid4()), "name": port, "order": idx, "net_id": "", "position": [0, 0], "display_visible": True}
            for idx, port in enumerate(port_names, start=1)
        ],
        "labels": [],
        "instances": [],
        "nets": [],
        "generated_language": "julia",
        "generator_kind": "josephsoncircuits_solveS",
        "generated_source": source,
        "simulation": sim,
        "symbol": generated_symbol(port_names),
        "symbol_port_layout": generated_symbol(port_names)["port_layout"],
        "gui": {"version": 1, "viewport": {"zoom": 1, "pan": [0, 0]}, "wire_routes": [], "last_selected": []},
    }


def _assign_layout_positions(
    components: list[dict],
    uid_by_raw_name: dict[str, str],
    inst_by_uid: dict[str, dict],
) -> None:
    """Assign (x, y) positions to instances based on circuit topology.

    Series elements (both nodes non-zero) go along the main horizontal chain.
    Shunt elements (one node is ground/"0") hang below their node.
    """
    CELL_W = 260
    CELL_H = 170
    PAD = 10
    HALF_W = 60
    HALF_H = 35

    # Collect non-ground nodes in order of appearance, then sort numerically if possible
    node_order: list[str] = []
    node_seen: set[str] = set()
    for comp in components:
        if primitive_type_from_name(str(comp.get("name", ""))) == "K":
            continue
        for key in ("node1", "node2"):
            node = str(comp.get(key, ""))
            if node != "0" and node not in node_seen:
                node_seen.add(node)
                node_order.append(node)
    try:
        node_order.sort(key=lambda n: float(n))
    except ValueError:
        pass

    node_x: dict[str, int] = {node: idx * CELL_W for idx, node in enumerate(node_order)}
    shunt_count: dict[str, int] = {}
    series_count: dict[tuple[str, str], int] = {}

    for comp in components:
        raw_name = str(comp.get("name", ""))
        t_name = primitive_type_from_name(raw_name)
        if t_name == "K":
            continue
        uid = uid_by_raw_name.get(raw_name)
        if not uid or uid not in inst_by_uid:
            continue
        inst = inst_by_uid[uid]
        n1 = str(comp.get("node1", ""))
        n2 = str(comp.get("node2", ""))

        if n1 == "0" or n2 == "0":
            non_zero = n2 if n1 == "0" else n1
            x = PAD + HALF_W + node_x.get(non_zero, 0)
            count = shunt_count.get(non_zero, 0)
            y = CELL_H + count * CELL_H
            shunt_count[non_zero] = count + 1
        else:
            x1 = node_x.get(n1, 0)
            x2 = node_x.get(n2, 0)
            x = PAD + HALF_W + (x1 + x2) // 2
            key = (min(n1, n2, key=str), max(n1, n2, key=str))
            count = series_count.get(key, 0)
            y = count * CELL_H
            series_count[key] = count + 1

        inst["position"] = [x, PAD + HALF_H + y]

    # Final collision-avoidance pass. The topology layout can put a shunt and a
    # series branch on nearby rows; this guarantees at least 10 px between the
    # actual symbol boxes even for dense Josephson circuits.
    placed: list[dict[str, Any]] = []
    for inst in sorted(
        (item for item in inst_by_uid.values() if item.get("type_name") != "K"),
        key=lambda item: (float(item.get("position", [0, 0])[1]), float(item.get("position", [0, 0])[0])),
    ):
        while any(_instance_boxes_overlap(inst, other, gap=10.0) for other in placed):
            pos = inst.get("position", [0, 0])
            inst["position"] = [float(pos[0]), float(pos[1]) + CELL_H]
        placed.append(inst)


def _assign_coupling_positions(instances: list[dict[str, Any]]) -> None:
    used_positions = [inst.get("position", [0, 0]) for inst in instances if inst.get("type_name") != "K"]
    min_x = min((float(pos[0]) for pos in used_positions), default=90.0)
    next_y = 45.0
    for inst in instances:
        if inst.get("type_name") != "K":
            continue
        inst["position"] = [min_x, next_y]
        next_y += 150.0


def _instance_boxes_overlap(a: dict[str, Any], b: dict[str, Any], gap: float = 10.0) -> bool:
    def bounds(inst: dict[str, Any]) -> tuple[float, float, float, float]:
        symbol = inst.get("symbol") or generated_symbol(inst.get("port_names", []) or [])
        width = float(symbol.get("width", 120) or 120)
        height = float(symbol.get("height", 70) or 70)
        pos = inst.get("position", [0, 0])
        x, y = float(pos[0]), float(pos[1])
        return (x - width / 2 - gap, y - height / 2 - gap, x + width / 2 + gap, y + height / 2 + gap)

    al, at, ar, ab = bounds(a)
    bl, bt, br, bb = bounds(b)
    return not (ar <= bl or al >= br or ab <= bt or at >= bb)


def port_names_from_probe(probe: dict[str, Any]) -> list[str]:
    ports: list[tuple[int, str]] = []
    for comp in probe.get("components", []) or []:
        if primitive_type_from_name(str(comp.get("name", ""))) != "P":
            continue
        try:
            port_num = int(float(str(comp.get("value", "0"))))
        except ValueError:
            port_num = len(ports) + 1
        ports.append((port_num, f"P{port_num}"))
    seen: set[str] = set()
    out: list[str] = []
    for _num, port in sorted(ports):
        if port not in seen:
            seen.add(port)
            out.append(port)
    return out or ["P1", "P2"]


def default_simulation(name: str) -> dict[str, Any]:
    return {
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
            "top_block": True,
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
            "pump_frequency": 7.12,
            "pump_current": "1.85e-6",
            "dc_port": "",
            "dc_current": "",
            "modulation_harmonics": 10,
            "pump_harmonics": 20,
            "threewave_mixing": True,
            "fourwave_mixing": True,
        },
    }


def simulation_defaults_from_probe(probe: dict[str, Any]) -> dict[str, Any]:
    sim = default_simulation("generated_hb")
    raw = probe.get("simulation") or {}
    if "freq_start" in raw:
        sim["freq_start"] = raw["freq_start"]
    if "freq_stop" in raw:
        sim["freq_stop"] = raw["freq_stop"]
    if "freq_points" in raw:
        sim["freq_points"] = raw["freq_points"]
    source_ports = [int(p) for p in raw.get("source_ports", []) or []]
    source_currents = [str(c) for c in raw.get("source_currents", []) or []]
    source_modes = raw.get("source_modes", []) or []
    pump_ports: list[int] = []
    pump_currents: list[str] = []
    dc_ports: list[int] = []
    dc_currents: list[str] = []
    for idx, port in enumerate(source_ports):
        mode = source_modes[idx] if idx < len(source_modes) else []
        current = source_currents[idx] if idx < len(source_currents) else "0"
        if any(int(float(x)) != 0 for x in mode):
            pump_ports.append(port)
            pump_currents.append(current)
        else:
            dc_ports.append(port)
            dc_currents.append(current)
    sim["input_ports"] = [f"P{source_ports[0]}"] if source_ports else []
    p_entries = port_names_from_probe(probe)
    sim["output_ports"] = [p_entries[-1]] if p_entries else []
    hb = sim["hb"]
    hb["pump_ports"] = pump_ports
    hb["pump_currents"] = pump_currents
    hb["dc_ports"] = dc_ports
    hb["dc_currents"] = dc_currents
    if raw.get("pump_frequencies"):
        hb["pump_frequencies"] = raw["pump_frequencies"]
    if raw.get("modulation_harmonics"):
        hb["modulation_harmonics"] = raw["modulation_harmonics"]
    if raw.get("pump_harmonics"):
        hb["pump_harmonics"] = raw["pump_harmonics"]
    return sim


def summary_from_probe(probe: dict[str, Any]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    nodes: set[str] = set()
    for comp in probe.get("components", []) or []:
        t_name = primitive_type_from_name(str(comp.get("name", "")))
        counts[t_name] = counts.get(t_name, 0) + 1
        if t_name != "K":
            nodes.add(str(comp.get("node1", "")))
            nodes.add(str(comp.get("node2", "")))
    return {
        "component_count": len(probe.get("components", []) or []),
        "node_count": len(nodes),
        "primitive_counts": counts,
        "ports": port_names_from_probe(probe),
        "warnings": probe.get("warnings", []),
    }


def extract_circuit_source(source: str) -> str:
    """Return circuit-defining setup plus the first hbsolve/solveS call.

    Strips plotting, display, CSV writing and any code that follows the first
    solve call so only the circuit definition and simulation parameters are kept.
    """
    setup = strip_runtime_section(source)
    m = re.search(r"\b(hbsolve|solveS)\s*\(", source)
    if not m:
        return setup
    name_start = m.start(1)
    open_paren = source.find("(", name_start)
    close_paren = find_matching(source, open_paren, "(", ")")
    if close_paren == -1:
        call_text = source[name_start:].strip()
    else:
        call_text = source[name_start : close_paren + 1].strip()
    return setup.rstrip() + "\n" + call_text + "\n"


def materialize_generated_hb_cell(cell: dict[str, Any], parameters: dict[str, Any] | None = None) -> dict[str, Any]:
    source = str(cell.get("generated_source", ""))
    if not source.strip():
        raise ValueError(f"Generated HB cell {cell.get('name')} has no generated_source.")
    probe = probe_julia_source(source)
    out = materialize_probe_to_pipeline_cell(cell, probe, parameters or {})
    return out


def materialize_probe_to_pipeline_cell(cell: dict[str, Any], probe: dict[str, Any], parameters: dict[str, Any]) -> dict[str, Any]:
    components = probe.get("components", []) or []
    used_uids: set[str] = set()
    instances: list[dict[str, Any]] = []
    endpoint_by_node: dict[str, list[dict[str, str]]] = {}
    uid_by_raw_name: dict[str, str] = {}

    for comp in components:
        raw_name = str(comp.get("name", ""))
        t_name = primitive_type_from_name(raw_name)
        if t_name == "K":
            continue
        if t_name not in HB_VALUE_KEYS:
            raise ValueError(f"Unsupported HB primitive {t_name!r} from component {raw_name!r}.")
        uid = make_uid(raw_name, used_uids)
        uid_by_raw_name[raw_name] = uid
        value_key = HB_VALUE_KEYS[t_name]
        value = str(comp.get("value", "1.0"))
        params = {value_key: value}
        inst = {
            "type_name": t_name,
            "uid": uid,
            "parameters": params,
            "parameter_order": [value_key],
            "parameter_kinds": {value_key: "positional"},
            "position": [0, 0],
            "port_count": 2,
            "port_names": ["p1", "p2"],
            "rotation_degrees": 0,
            "repeat_count": 1,
            "repeat_connections": [],
        }
        instances.append(inst)
        endpoint_by_node.setdefault(str(comp.get("node1", "")), []).append({"instance_uid": uid, "port": "p1"})
        endpoint_by_node.setdefault(str(comp.get("node2", "")), []).append({"instance_uid": uid, "port": "p2"})

    for comp in components:
        raw_name = str(comp.get("name", ""))
        if primitive_type_from_name(raw_name) != "K":
            continue
        uid = make_uid(raw_name if raw_name != "K" else "K", used_uids)
        branch_a = str(comp.get("node1", ""))
        branch_b = str(comp.get("node2", ""))
        instances.append(
            {
                "type_name": "K",
                "uid": uid,
                "parameters": {"inductor_a": branch_a, "inductor_b": branch_b, "K": str(comp.get("value", "1.0"))},
                "parameter_order": ["inductor_a", "inductor_b", "K"],
                "parameter_kinds": {"inductor_a": "uid", "inductor_b": "uid", "K": "positional"},
                "position": [0, 0],
                "port_count": 0,
                "port_names": [],
                "rotation_degrees": 0,
                "repeat_count": 1,
                "repeat_connections": [],
            }
        )

    # Assign topology-aware positions to non-K instances
    inst_by_uid = {inst["uid"]: inst for inst in instances}
    _assign_layout_positions(components, uid_by_raw_name, inst_by_uid)
    _assign_coupling_positions(instances)

    wires: list[dict[str, Any]] = []
    labels: list[dict[str, Any]] = []
    pins: list[dict[str, Any]] = []
    wire_idx = 1
    label_idx = 1

    for node, endpoints in sorted(endpoint_by_node.items()):
        if node == "0":
            for endpoint in endpoints:
                labels.append(
                    {
                        "name": "0",
                        "instance_uid": endpoint["instance_uid"],
                        "port": endpoint["port"],
                        "position": [0, 0],
                    }
                )
                label_idx += 1
            continue
        if len(endpoints) < 2:
            if endpoints:
                endpoint = endpoints[0]
                labels.append(
                    {
                        "name": str(node),
                        "instance_uid": endpoint["instance_uid"],
                        "port": endpoint["port"],
                        "position": [0, 0],
                    }
                )
                label_idx += 1
            continue
        hub = endpoints[0]
        labels.append(
            {
                "name": str(node),
                "instance_uid": hub["instance_uid"],
                "port": hub["port"],
                "position": [0, 0],
            }
        )
        label_idx += 1
        for endpoint in endpoints[1:]:
            wires.append(
                {
                    "source_instance_uid": hub["instance_uid"],
                    "source_port": hub["port"],
                    "target_instance_uid": endpoint["instance_uid"],
                    "target_port": endpoint["port"],
                    "name": f"n_{clean_name(node)}",
                    "gui_wire_id": f"wire_{wire_idx}",
                }
            )
            wire_idx += 1

    desired_port_names = [str(pin.get("name")) for pin in sorted(cell.get("pins", []), key=lambda p: int(p.get("order", 0) or 0)) if pin.get("name")]
    default_port_names = port_names_from_probe(probe)
    port_name_by_number = {
        idx + 1: desired_port_names[idx] if idx < len(desired_port_names) else default_name
        for idx, default_name in enumerate(default_port_names)
    }

    for comp in components:
        if primitive_type_from_name(str(comp.get("name", ""))) != "P":
            continue
        raw_name = str(comp.get("name", ""))
        uid = uid_by_raw_name.get(raw_name)
        if not uid:
            continue
        try:
            port_number = int(float(str(comp.get("value", "0"))))
        except ValueError:
            port_number = len(pins) + 1
        pin_port = "p2" if str(comp.get("node1", "")) == "0" else "p1"
        pins.append(
            {
                "name": port_name_by_number.get(port_number, f"P{port_number}"),
                "instance_uid": uid,
                "port": pin_port,
            }
        )

    variables = [
        {
            "name": str(var.get("name")),
            "default": str(parameters.get(var.get("name"), var.get("default", var.get("value", "")))),
            "value": str(parameters.get(var.get("name"), var.get("value", var.get("default", "")))),
            "scope": var.get("scope", "cell"),
            "export": var.get("export", True),
        }
        for var in cell.get("variables", []) or []
        if var.get("name")
    ]

    sim = cell.get("simulation", {}) or {}
    hb = sim.get("hb", {}) or {}
    out = {
        "name": cell.get("name", "generated_hb"),
        "type": "schematic",
        "instances": instances,
        "wires": wires,
        "pins": pins,
        "labels": labels,
        "simulation_variables": variables,
        "simulation": {
            "z0": float(sim.get("z0") or cell.get("z0", 50) or 50),
            "hb": {
                "top_block": str(hb.get("top_block", cell.get("hb_top_block", True))).lower() in ("true", "1", "yes"),
                "pump_ports": hb.get("pump_ports", cell.get("hb_pump_ports", [])),
                "pump_frequencies": hb.get("pump_frequencies", cell.get("hb_pump_frequencies", [])),
                "pump_currents": hb.get("pump_currents", cell.get("hb_pump_currents", [])),
                "dc_ports": hb.get("dc_ports", cell.get("hb_dc_ports", [])),
                "dc_currents": hb.get("dc_currents", cell.get("hb_dc_currents", [])),
                "modulation_harmonics": hb.get("modulation_harmonics", cell.get("hb_modulation_harmonics", [10])),
                "pump_harmonics": hb.get("pump_harmonics", cell.get("hb_pump_harmonics", [20])),
                "threewave_mixing": hb.get("threewave_mixing", cell.get("hb_threewave_mixing", True)),
                "fourwave_mixing": hb.get("fourwave_mixing", cell.get("hb_fourwave_mixing", True)),
            },
        },
        "simulation_input_ports": sim.get("input_ports", []),
        "simulation_output_ports": sim.get("output_ports", []),
        "simulation_freq_start": float(sim.get("freq_start", 2.0)),
        "simulation_freq_stop": float(sim.get("freq_stop", 20.0)),
        "simulation_freq_points": int(sim.get("freq_points", 200)),
        "simulation_sweep_type": sim.get("sweep_type", "linear"),
        "simulation_figure_title": sim.get("figure_title") or cell.get("name"),
        "generated_from": "trusted_julia",
        "generated_source": extract_circuit_source(str(cell.get("generated_source", ""))),
    }
    return normalize_cell_layout(out)
