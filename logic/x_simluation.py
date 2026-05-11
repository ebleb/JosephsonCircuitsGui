#!/usr/bin/env python3
"""
x_simluation.py

X-parameter simulation stage.

This intentionally keeps X-parameter HB simulation separate from the ordinary
S-parameter simulation path in simulation.py. It reuses only neutral helpers
for JSON loading, caching, topology validation, HB circuit generation, and
dependency discovery.

Input:
    outputs/<project>/specialized/

Outputs:
    outputs/<project>/cache/<cell>_<hash>.csv
        Ordinary signal-mode S matrix, kept for compatibility with the existing
        cache manifest and plotting path.

    outputs/<project>/cache/<cell>_<hash>_x_XS_full.csv
    outputs/<project>/cache/<cell>_<hash>_x_XT_full.csv
        Full first-order X^S and X^T matrices over all linearized modes and
        physical ports, in port-major raw indexing.

    outputs/<project>/cache/<cell>_<hash>_x_XFB.csv
        Large-signal X^FB terms from the nonlinear pump solution.

    outputs/<project>/cache/<cell>_<hash>_x_modes.json
        Metadata describing indexing, modes, ports, and source fields.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from path_utils import resolve_source_target
from simulation import (
    build_dependency_graph,
    build_nodal_netlist,
    cache_key_for_json,
    classify_json_for_simulation,
    dependency_levels,
    extract_hbsolve_ports,
    extract_sim_variables_julia,
    frequency_settings,
    generate_hb_circuit_push_lines,
    generate_hb_repeat_node_helpers,
    load_cache_manifest,
    load_json,
    lookup_cached_csv,
    nodeflux_csv_path,
    run_julia_batch,
    stable_json_hash,
    update_cache_manifest,
    validate_numeric_topology,
)

X_SIMULATION_CACHE_VERSION = "x-signal-port-reordered-xs-xt-v5-nodeflux"


def truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def x_params_enabled(data: dict[str, Any]) -> bool:
    return truthy(data.get("x-params", data.get("x_params", False)))


def require_list(data: dict[str, Any], key: str, *, allow_empty: bool = False) -> list[Any]:
    if key not in data:
        raise ValueError(f"x-params requires JSON field {key!r}")

    value = data[key]
    if not isinstance(value, list):
        raise ValueError(f"x-params field {key!r} must be a list, got {type(value).__name__}")

    if not allow_empty and not value:
        raise ValueError(f"x-params field {key!r} may not be empty")

    return value


def require_single(value: list[Any], key: str) -> Any:
    if len(value) != 1:
        raise ValueError(
            f"x_simluation.py currently supports exactly one {key} entry. "
            f"Got {len(value)}. Please specify how multiple pumps/sources should "
            "be represented before this is generalized."
        )
    return value[0]


def julia_literal(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return value
    raise ValueError(f"Cannot convert value to Julia literal: {value!r}")


def x_cache_paths(out_csv_path: Path) -> dict[str, Path]:
    base = out_csv_path.with_suffix("")
    return {
        "xfb_csv": Path(f"{base}_x_XFB.csv"),
        "xs_full_csv": Path(f"{base}_x_XS_full.csv"),
        "xt_full_csv": Path(f"{base}_x_XT_full.csv"),
        "modes_json": Path(f"{base}_x_modes.json"),
        "nodeflux_csv": nodeflux_csv_path(out_csv_path),
    }


def unique_ints_in_order(values: list[Any]) -> list[int]:
    out: list[int] = []
    seen: set[int] = set()
    for value in values:
        item = int(value)
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def extract_x_signal_ports(data: dict[str, Any]) -> str:
    """
    Port order for the compatibility S CSV.

    The X pipeline may use JosephsonCircuits P numbers that do not sort into
    the same order as the non-X circuit. For example, adding a separate exposed
    pin can make the physical input field 3 while the output field is 2. The
    compatibility S CSV should follow the requested external X order, not sorted
    raw P numbers.
    """
    fields = unique_ints_in_order(
        list(data.get("x_input_fields", []) or [])
        + list(data.get("x_out_fields", []) or [])
    )

    if not fields:
        raise ValueError(
            "x-params requires at least one x_input_fields/x_out_fields entry "
            "to define the saved signal S-port order."
        )

    if len(fields) == 1:
        return f"({fields[0]},)"

    return "(" + ", ".join(str(field) for field in fields) + ")"


def hb_uid_group_prefix(uid: str) -> str:
    uid = str(uid)
    return uid.split("_", 1)[0] if "_" in uid else uid


def collect_x_hb_repeat_groups(data: dict[str, Any], node_map: dict[str, int]) -> dict[str, Any]:
    """
    X-local repeated HB grouping.

    The ordinary simulation helper requires exactly two boundary wires. X-merged
    flattened circuits can have several boundary wires that land on the same
    electrical boundary node, for example parallel L/C endpoints. For X
    simulation, group boundaries by compact nodal node instead.
    """
    repeated_instances = [
        inst for inst in data.get("instances", [])
        if int(inst.get("repeat_count", 1) or 1) > 1
    ]

    groups: dict[str, list[dict[str, Any]]] = {}
    for inst in repeated_instances:
        uid = inst.get("uid")
        if not uid:
            continue

        if inst.get("type_name") == "P":
            raise ValueError(
                f"Invalid repeat_count on P block {uid}. Repeated groups may not contain P blocks."
            )

        prefix = hb_uid_group_prefix(uid)
        groups.setdefault(prefix, []).append(inst)

    group_info: dict[str, Any] = {}

    for prefix, group_insts in groups.items():
        repeat_counts = sorted(
            set(int(inst.get("repeat_count", 1) or 1) for inst in group_insts)
        )
        if len(repeat_counts) != 1:
            raise ValueError(
                f"Repeated HB group {prefix} has inconsistent repeat_count values: {repeat_counts}"
            )

        repeat_count = repeat_counts[0]
        group_uids = {inst["uid"] for inst in group_insts}

        boundary_by_node: dict[int, list[dict[str, Any]]] = {}
        boundary_nodes_in_order: list[int] = []
        for wire in data.get("wires", []):
            src_uid = wire.get("source_instance_uid")
            tgt_uid = wire.get("target_instance_uid")
            src_in = src_uid in group_uids
            tgt_in = tgt_uid in group_uids

            if not (src_in ^ tgt_in):
                continue

            if src_in:
                group_uid = src_uid
                group_port = int(wire.get("source_port"))
                outside_uid = tgt_uid
                outside_port = int(wire.get("target_port"))
                group_is_source = True
            else:
                group_uid = tgt_uid
                group_port = int(wire.get("target_port"))
                outside_uid = src_uid
                outside_port = int(wire.get("source_port"))
                group_is_source = False

            local_node = node_map[f"{group_uid}:{group_port}"]
            if local_node not in boundary_by_node:
                boundary_by_node[local_node] = []
                boundary_nodes_in_order.append(local_node)
            boundary_by_node[local_node].append(
                    {
                        "group_uid": group_uid,
                        "group_port": group_port,
                        "outside_uid": outside_uid,
                        "outside_port": outside_port,
                        "group_is_source": group_is_source,
                        "local_node": local_node,
                    }
                )

        if len(boundary_by_node) != 2:
            raise ValueError(
                f"Repeated HB group {prefix} must have exactly two boundary electrical nodes, "
                f"found {len(boundary_by_node)}. Boundary nodes={boundary_by_node}"
            )

        input_nodes = [
            node for node, entries in boundary_by_node.items()
            if any(not entry["group_is_source"] for entry in entries)
            and not any(entry["group_is_source"] for entry in entries)
        ]
        output_nodes = [
            node for node, entries in boundary_by_node.items()
            if any(entry["group_is_source"] for entry in entries)
            and not any(not entry["group_is_source"] for entry in entries)
        ]

        if len(input_nodes) == 1 and len(output_nodes) == 1:
            start_node = input_nodes[0]
            end_node = output_nodes[0]
        else:
            start_node, end_node = boundary_nodes_in_order[0], boundary_nodes_in_order[1]

        group_nodes = set()
        for inst in group_insts:
            uid = inst["uid"]
            for port in range(1, int(inst.get("port_count", 2)) + 1):
                key = f"{uid}:{port}"
                if key in node_map:
                    group_nodes.add(node_map[key])

        internal_nodes = sorted(n for n in group_nodes if n not in {0, start_node, end_node})

        group_info[prefix] = {
            "prefix": prefix,
            "repeat_count": repeat_count,
            "uids": group_uids,
            "instances": group_insts,
            "start_node": start_node,
            "end_node": end_node,
            "internal_nodes": internal_nodes,
        }

    return group_info


def update_x_cache_manifest(
    cache_dir: Path,
    cell_name: str,
    cache_key: str,
    normal_csv: Path,
    x_paths: dict[str, Path],
) -> None:
    manifest_path = cache_dir / "x_cache_manifest.json"
    if manifest_path.exists():
        with open(manifest_path, "r") as f:
            manifest = json.load(f)
    else:
        manifest = {}

    manifest[cell_name] = {
        "cache_key": cache_key,
        "csv": str(normal_csv.resolve()),
        "XFB": str(x_paths["xfb_csv"].resolve()),
        "XS_full": str(x_paths["xs_full_csv"].resolve()),
        "XT_full": str(x_paths["xt_full_csv"].resolve()),
        "modes": str(x_paths["modes_json"].resolve()),
        "nodeflux": str(x_paths["nodeflux_csv"].resolve()),
    }

    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)


def validate_x_hb_data(data: dict[str, Any], json_name: str) -> None:
    if not x_params_enabled(data):
        raise ValueError(f"{json_name}: x_simluation.py requires 'x-params': true")

    pump_keys = [
        "x_pump_frequencies",
        "x_pump_currents",
        "x_pump_fields",
        "x_modulation_harmonics",
        "x_pump_harmonics",
    ]
    pump_count = max(len(require_list(data, key)) for key in pump_keys)
    for key in pump_keys:
        values = require_list(data, key)
        if pump_count > 1 and len(values) == 1:
            data[key] = values * pump_count

    pump_lengths = {
        key: len(require_list(data, key))
        for key in pump_keys
    }
    if len(set(pump_lengths.values())) != 1:
        raise ValueError(f"{json_name}: X pump setting lengths must match, got {pump_lengths}")
    if next(iter(pump_lengths.values())) == 0:
        raise ValueError(f"{json_name}: X pump settings may not be empty")

    require_single(require_list(data, "x_input_fields"), "x_input_fields")
    require_single(require_list(data, "x_input_ports"), "x_input_ports")
    require_single(require_list(data, "x_out_fields"), "x_out_fields")
    require_single(require_list(data, "x_output_ports"), "x_output_ports")

    dc_ports = data.get("x_dc_ports", [])
    dc_currents = data.get("x_dc_currents", [])
    if not isinstance(dc_ports, list) or not isinstance(dc_currents, list):
        raise ValueError(f"{json_name}: x_dc_ports and x_dc_currents must be lists")
    if len(dc_ports) != len(dc_currents):
        raise ValueError(
            f"{json_name}: len(x_dc_ports)={len(dc_ports)} does not match "
            f"len(x_dc_currents)={len(dc_currents)}"
        )
    if dc_ports:
        dc_fields = require_list(data, "x_dc_fields")
        if len(dc_fields) != len(dc_ports):
            raise ValueError(
                f"{json_name}: len(x_dc_fields)={len(dc_fields)} does not match "
                f"len(x_dc_ports)={len(dc_ports)}"
            )


def generate_hbsolve_xparams_script(
    json_path: Path,
    cache_dir: Path,
    out_jl_path: Path,
    out_csv_path: Path,
) -> dict[str, Path]:
    data = load_json(json_path)
    validate_numeric_topology(data, json_path.name)
    validate_x_hb_data(data, json_path.name)

    node_map, _ = build_nodal_netlist(data)
    group_info = collect_x_hb_repeat_groups(data, node_map)
    first_free_node = max(node_map.values(), default=0) + 1
    repeat_helpers_jl, _ = generate_hb_repeat_node_helpers(group_info, first_free_node)
    simulation_dir = json_path.parent
    builtin_dir = Path(__file__).parent.resolve() / "built-in"
    circuit_push_jl = generate_hb_circuit_push_lines(
        data,
        node_map,
        group_info,
        simulation_dir,
        builtin_dir,
    )

    f_start, f_stop, f_points = frequency_settings(data)
    sim_vars_str = extract_sim_variables_julia(data)
    ports_jl = extract_x_signal_ports(data)

    pump_frequencies = require_list(data, "x_pump_frequencies")
    pump_currents = require_list(data, "x_pump_currents")
    pump_fields = [int(value) for value in require_list(data, "x_pump_fields")]
    mod_harmonics = [int(value) for value in require_list(data, "x_modulation_harmonics")]
    pump_harmonics = [int(value) for value in require_list(data, "x_pump_harmonics")]
    pump_count = len(pump_fields)
    def julia_tuple(values):
        suffix = "," if len(values) == 1 else ""
        return "(" + ",".join(values) + suffix + ")"

    zero_mode = julia_tuple(["0"] * pump_count)
    pump_frequency = pump_frequencies[0]
    pump_current = pump_currents[0]
    pump_field = pump_fields[0]
    dc_fields = data.get("x_dc_fields", []) or []
    dc_currents = data.get("x_dc_currents", []) or []
    dc_fields = [int(value) for value in dc_fields]
    dc_enabled = bool(dc_fields)
    dc_current_terms = ", ".join(julia_literal(current) for current in dc_currents)
    dc_current_line = f"Idc = [{dc_current_terms}]" if dc_enabled else "Idc = Float64[]"
    dc_source_terms = ", ".join(
        f"(mode={zero_mode}, port={field}, current=Idc[{idx}])"
        for idx, field in enumerate(dc_fields, start=1)
    )
    dc_source_prefix = f"{dc_source_terms}, " if dc_source_terms else ""
    dc_kwarg = "dc=true," if dc_enabled else ""
    dc_fields_jl = ", ".join(str(value) for value in dc_fields)
    dc_metadata = (
        f"""
        println(io, "  \\"dc_enabled\\": true,")
        println(io, "  \\"dc_port\\": {dc_fields[0]},")
        println(io, "  \\"dc_ports\\": [{dc_fields_jl}],")
        println(io, "  \\"dc_current\\": \\"", string(Idc[1]), "\\",")
        println(io, "  \\"dc_currents\\": [", join(["\\"" * string(v) * "\\"" for v in Idc], ", "), "],")"""
        if dc_enabled
        else """
        println(io, "  \\"dc_enabled\\": false,")"""
    )
    pump_wp_terms = ", ".join(f"2 * pi * {julia_literal(freq)} * 1e9" for freq in pump_frequencies)
    pump_current_terms = ", ".join(julia_literal(current) for current in pump_currents)

    pump_mode_terms = []
    for idx in range(pump_count):
        mode_values = ["0"] * pump_count
        mode_values[idx] = "1"
        pump_mode_terms.append(julia_tuple(mode_values))
    pump_source_terms = ", ".join(
        f"(mode={mode}, port={field}, current=Ip[{idx}])"
        for idx, (field, mode) in enumerate(zip(pump_fields, pump_mode_terms), start=1)
    )
    mod_harmonics_jl = ", ".join(str(value) for value in mod_harmonics)
    pump_harmonics_jl = ", ".join(str(value) for value in pump_harmonics)
    pump_fields_jl = ", ".join(str(value) for value in pump_fields)

    threewave = truthy(data.get("x_threewave_mixing", True))
    fourwave = truthy(data.get("x_fourwave_mixing", True))
    conjugate_partner_offset = int(data.get("x_conjugate_partner_offset", 0))

    x_paths = x_cache_paths(out_csv_path)

    jl_code = f"""using DelimitedFiles
using LinearAlgebra
using JosephsonCircuits

{sim_vars_str}

struct XParamsResult
    hb
    XFB
    XS
    XT
    P
    pump_current
    pump_modes
    signal_modes
    ports
    conjugate_partner
end

raw_index(port::Integer, mode_index::Integer, Nmodes::Integer) =
    (port - 1) * Nmodes + mode_index

function pump_phase(Ip)
    abs(Ip) == 0 && error("Pump current Ip must be nonzero.")
    return Ip / abs(Ip)
end

function extract_XFB_from_nonlinear(nonlinear, sources, circuit, circuitdefs;
    P,
    sorting=:number,
    symfreqvar=nothing,
)
    psc = JosephsonCircuits.parsesortcircuit(circuit, sorting=sorting)
    cg = JosephsonCircuits.calccircuitgraph(psc)
    nm = JosephsonCircuits.numericmatrices(psc, cg, circuitdefs;
        Nmodes=nonlinear.Nmodes)

    modes = nonlinear.modes
    Nmodes = nonlinear.Nmodes
    Nports = length(nm.portindices)

    wmodes = JosephsonCircuits.calcmodefreqs(nonlinear.w, modes)

    Lmean = nm.Lmean
    if iszero(Lmean)
        Lmean = one(eltype(Lmean))
    end

    bbm = JosephsonCircuits.calcsources(
        modes,
        sources,
        nm.portindices,
        nm.portnumbers,
        psc.nodeindices,
        cg.edge2indexdict,
        Lmean,
        psc.Nnodes,
        cg.Nbranches,
        Nmodes,
    )

    bnm = transpose(nm.Rbnm) * bbm
    portimpedances = [nm.vvn[i] for i in nm.portimpedanceindices]

    inputwave = zeros(ComplexF64, Nports * Nmodes)
    outputwave = zeros(ComplexF64, Nports * Nmodes)

    JosephsonCircuits.calcinputoutput!(
        inputwave,
        outputwave,
        nonlinear.nodeflux,
        bnm / Lmean,
        nm.portimpedanceindices,
        nm.portimpedanceindices,
        portimpedances,
        portimpedances,
        psc.nodeindices,
        psc.componenttypes,
        wmodes,
        symfreqvar,
    )

    XFB = similar(outputwave)

    for (mi, mode) in enumerate(modes)
        k = mode[1]
        for pi in 1:Nports
            idx = raw_index(pi, mi, Nmodes)
            XFB[idx] = outputwave[idx] * P^(-k)
        end
    end

    return XFB, inputwave, outputwave
end

function extract_XS_XT_from_twosided_S(
    S,
    modes,
    ports,
    P;
    conjugate_partner = l -> {conjugate_partner_offset} - l,
)
    Nmodes = length(modes)
    XS = zeros(eltype(S), size(S))
    XT = zeros(eltype(S), size(S))

    mode_to_index = Dict(mode[1] => i for (i, mode) in enumerate(modes))

    for (ko, out_mode) in enumerate(modes)
        k = out_mode[1]

        for (li, in_mode) in enumerate(modes)
            l = in_mode[1]
            li_conj_mode = conjugate_partner(l)
            has_conj = haskey(mode_to_index, li_conj_mode)
            li_conj = has_conj ? mode_to_index[li_conj_mode] : nothing

            phase_S = P^(-(k - l))
            phase_T = P^(-(k + l))

            for po in eachindex(ports)
                row = raw_index(po, ko, Nmodes)

                for qi in eachindex(ports)
                    col = raw_index(qi, li, Nmodes)
                    XS[row, col, :] .= S[row, col, :] .* phase_S

                    if has_conj
                        col_conj = raw_index(qi, li_conj, Nmodes)
                        XT[row, col, :] .= S[row, col_conj, :] .* phase_T
                    end
                end
            end
        end
    end

    return XS, XT
end

function hbsolve_xparams(
    ws,
    wp,
    Ip,
    sources,
    Nmodulationharmonics,
    Npumpharmonics,
    circuit,
    circuitdefs;
    sorting=:number,
    symfreqvar=nothing,
    conjugate_partner = l -> {conjugate_partner_offset} - l,
    kwargs...
)
    wp_tuple = wp isa Tuple ? wp : (wp,)

    hb = JosephsonCircuits.hbsolve(
        ws,
        wp_tuple,
        sources,
        Nmodulationharmonics,
        Npumpharmonics,
        circuit,
        circuitdefs;
        keyedarrays=Val(false),
        symfreqvar=symfreqvar,
        sorting=sorting,
        threewavemixing={julia_literal(threewave)},
        fourwavemixing={julia_literal(fourwave)},
        kwargs...,
    )

    P = pump_phase(Ip isa AbstractVector ? first(Ip) : Ip)

    XFB, _, _ = extract_XFB_from_nonlinear(
        hb.nonlinear,
        sources,
        circuit,
        circuitdefs;
        P=P,
        sorting=sorting,
        symfreqvar=symfreqvar,
    )

    XS, XT = extract_XS_XT_from_twosided_S(
        hb.linearized.S,
        hb.linearized.modes,
        hb.linearized.portnumbers,
        P;
        conjugate_partner=conjugate_partner,
    )

    return XParamsResult(
        hb,
        XFB,
        XS,
        XT,
        P,
        Ip,
        hb.nonlinear.modes,
        hb.linearized.modes,
        hb.linearized.portnumbers,
        "l -> {conjugate_partner_offset} - l",
    )
end

function reorder_port_major_matrix_to_saved_ports(X, raw_ports, saved_ports, Nmodes)
    port_to_raw_index = Dict(raw_ports[i] => i for i in eachindex(raw_ports))

    for port in saved_ports
        haskey(port_to_raw_index, port) || error(
            "Requested saved X port $(port) was not found in raw hbsolve ports $(raw_ports)"
        )
    end

    n = length(saved_ports) * Nmodes
    X_saved = zeros(eltype(X), n, n, size(X, 3))

    for out_i in eachindex(saved_ports)
        raw_out_i = port_to_raw_index[saved_ports[out_i]]

        for ko in 1:Nmodes
            saved_row = raw_index(out_i, ko, Nmodes)
            raw_row = raw_index(raw_out_i, ko, Nmodes)

            for in_i in eachindex(saved_ports)
                raw_in_i = port_to_raw_index[saved_ports[in_i]]

                for li in 1:Nmodes
                    saved_col = raw_index(in_i, li, Nmodes)
                    raw_col = raw_index(raw_in_i, li, Nmodes)
                    X_saved[saved_row, saved_col, :] .= X[raw_row, raw_col, :]
                end
            end
        end
    end

    return X_saved
end

function save_s_matrix(filepath, ws, S)
    num_freqs = length(ws)
    num_ports = size(S, 1)

    out_data = zeros(Float64, num_freqs, 1 + 2 * (num_ports^2))
    out_data[:, 1] = ws ./ (2*pi*1e9)

    col = 2
    for out_p in 1:num_ports
        for in_p in 1:num_ports
            out_data[:, col] = real.(S[out_p, in_p, :])
            out_data[:, col + 1] = imag.(S[out_p, in_p, :])
            col += 2
        end
    end

    writedlm(filepath, out_data, ',')
end

function save_xfb_csv(filepath, xp)
    modes = xp.pump_modes
    ports = xp.ports
    Nmodes = length(modes)
    out_data = zeros(Float64, length(ports) * Nmodes, 5)

    row_out = 1
    for pi in eachindex(ports)
        for mi in eachindex(modes)
            idx = raw_index(pi, mi, Nmodes)
            out_data[row_out, 1] = ports[pi]
            out_data[row_out, 2] = modes[mi][1]
            out_data[row_out, 3] = idx
            out_data[row_out, 4] = real(xp.XFB[idx])
            out_data[row_out, 5] = imag(xp.XFB[idx])
            row_out += 1
        end
    end

    open(filepath, "w") do io
        println(io, "port,mode,raw_index,real,imag")
        writedlm(io, out_data, ',')
    end
end

function save_nodeflux_csv(filepath, xp)
    modes = xp.hb.nonlinear.modes
    freqs = JosephsonCircuits.calcmodefreqs(xp.hb.nonlinear.w, xp.hb.nonlinear.modes) ./ (2*pi*1e9)
    values = vec(xp.hb.nonlinear.nodeflux)
    Nmodes = length(modes)
    Nmodes == 0 && error("Cannot save nodeflux: nonlinear modes are empty.")
    length(values) % Nmodes == 0 || error("Cannot save nodeflux: nodeflux length is not divisible by mode count.")
    Nnodes = length(values) ÷ Nmodes
    nodeflux_matrix = reshape(values, Nmodes, Nnodes)'
    sort_idx = sortperm(freqs)
    freq_plot = freqs[sort_idx]
    nodeflux_plot = nodeflux_matrix[:, sort_idx]
    modes_plot = modes[sort_idx]

    open(filepath, "w") do io
        println(io, "mode,frequency_GHz,node,real,imag")
        for node_idx in 1:Nnodes
            for mode_idx in 1:Nmodes
                value = nodeflux_plot[node_idx, mode_idx]
                println(io, string(first(modes_plot[mode_idx]), ",", freq_plot[mode_idx], ",", node_idx, ",", real(value), ",", imag(value)))
            end
        end
    end
end

function save_x_modes_json(filepath, xp)
    open(filepath, "w") do io
        println(io, "{{")
        println(io, "  \\"indexing\\": \\"port-major saved-port order\\",")
        println(io, "  \\"raw_index\\": \\"(port_index - 1) * Nmodes + mode_index\\",")
        println(io, "  \\"ports\\": [", join(string.(ports), ", "), "],")
        println(io, "  \\"matrix_ports\\": [", join(string.(ports), ", "), "],")
        println(io, "  \\"raw_ports\\": [", join(string.(raw_ports), ", "), "],")
        println(io, "  \\"saved_signal_ports\\": [", join(string.(ports), ", "), "],")
        println(io, "  \\"signal_modes\\": [", join([string(m[1]) for m in xp.signal_modes], ", "), "],")
        println(io, "  \\"pump_modes\\": [", join([string(m[1]) for m in xp.pump_modes], ", "), "],")
        println(io, "  \\"pump_current\\": \\"", string(xp.pump_current), "\\",")
        println(io, "  \\"pump_phase\\": \\"", string(xp.P), "\\",")
        println(io, "  \\"pump_port\\": {pump_field},")
        println(io, "  \\"pump_ports\\": [{pump_fields_jl}],")
{dc_metadata}
        println(io, "  \\"conjugate_partner\\": \\"l -> {conjugate_partner_offset} - l\\",")
        println(io, "  \\"x_matrix_note\\": \\"XS_full and XT_full are reordered to matrix_ports, matching the saved signal-only S CSV.\\"")
        println(io, "}}")
    end
end

ws = 2 * pi * range({f_start}, {f_stop}, length={f_points}) * 1e9
wp = ({pump_wp_terms},)
Ip = [{pump_current_terms}]
{dc_current_line}
sources = [{dc_source_prefix}{pump_source_terms}]

{repeat_helpers_jl}

circuit = Any[]

{circuit_push_jl}

circuitdefs = Dict()

println("Running X-parameter hbsolve on {json_path.name}...")
xp = hbsolve_xparams(
    ws,
    wp,
    Ip,
    sources,
    ({mod_harmonics_jl},),
    ({pump_harmonics_jl},),
    circuit,
    circuitdefs;
    returnS=true,
    returnSnoise=false,
    returnQE=true,
    returnCM=true,
    {dc_kwarg}
)

rpm = xp.hb
ports = {ports_jl}
num_ports = length(ports)
num_freqs = length(ws)
modes = rpm.linearized.modes
Nmodes = length(modes)
sig_idx = findfirst(m -> m == {zero_mode}, modes)
sig_idx === nothing && error("Signal mode {zero_mode} was not found in linearized modes.")

raw_ports = collect(rpm.linearized.portnumbers)
port_to_raw_index = Dict(raw_ports[i] => i for i in eachindex(raw_ports))

for port in ports
    haskey(port_to_raw_index, port) || error(
        "Requested saved S port $(port) was not found in raw hbsolve ports $(raw_ports)"
    )
end

S_signal = zeros(ComplexF64, num_ports, num_ports, num_freqs)

for out_i in eachindex(ports)
    row = raw_index(port_to_raw_index[ports[out_i]], sig_idx, Nmodes)
    for in_i in eachindex(ports)
        col = raw_index(port_to_raw_index[ports[in_i]], sig_idx, Nmodes)
        S_signal[out_i, in_i, :] .= rpm.linearized.S[row, col, :]
    end
end

XS_saved = reorder_port_major_matrix_to_saved_ports(xp.XS, raw_ports, ports, Nmodes)
XT_saved = reorder_port_major_matrix_to_saved_ports(xp.XT, raw_ports, ports, Nmodes)

save_s_matrix("{out_csv_path.as_posix()}", ws, S_signal)
save_s_matrix("{x_paths['xs_full_csv'].as_posix()}", ws, XS_saved)
save_s_matrix("{x_paths['xt_full_csv'].as_posix()}", ws, XT_saved)
save_xfb_csv("{x_paths['xfb_csv'].as_posix()}", xp)
save_x_modes_json("{x_paths['modes_json'].as_posix()}", xp)
save_nodeflux_csv("{x_paths['nodeflux_csv'].as_posix()}", xp)

println("Saved X-compatible signal S cache to {out_csv_path.name}")
println("Saved XFB/XS/XT/nodeflux outputs next to the cache CSV")
"""

    with open(out_jl_path, "w") as f:
        f.write(jl_code)

    return x_paths


def orchestrate_x_simulation(target: str, script_dir: Path) -> None:
    target_path = Path(target)
    project_name = target_path.parent.name if target_path.parent.name else "default_project"

    project_dir = script_dir / "outputs" / project_name
    simulation_dir = project_dir / "specialized"
    cache_dir = project_dir / "cache"

    cache_dir.mkdir(parents=True, exist_ok=True)

    memo_path = project_dir / "classification_memo.json"
    if not memo_path.exists():
        raise FileNotFoundError(f"Memo not found: {memo_path}. Run classifier first.")

    if not simulation_dir.exists():
        raise FileNotFoundError(
            f"Specialized directory not found: {simulation_dir}. "
            "Run specialization before X simulation."
        )

    with open(memo_path, "r") as f:
        memo = json.load(f)

    original_target = resolve_source_target(script_dir, target)
    specialized_target = simulation_dir / original_target.name
    if not specialized_target.exists():
        raise FileNotFoundError(f"Specialized target JSON not found: {specialized_target}")

    graph = build_dependency_graph(simulation_dir)
    if str(specialized_target.resolve()) not in graph:
        graph[str(specialized_target.resolve())] = set()

    levels = dependency_levels(graph)

    print("==================================================")
    print(" Running X-Parameter Simulation Orchestrator      ")
    print("==================================================")
    print(f" Simulation input: {simulation_dir}")
    print("==================================================\n")

    for level_idx, level in enumerate(levels, start=1):
        jobs = []

        for file_str in level:
            target_file = Path(file_str)

            # This file deliberately does not simulate ordinary S-param children.
            # If an X target still depends on such a child after merge/specialize,
            # the X rewrite/merge stage has not produced a single HB/X circuit.
            data = load_json(target_file)
            if not x_params_enabled(data):
                continue

            node_class = classify_json_for_simulation(target_file, memo)
            if node_class == "hbsolve_primitive":
                print(
                    f"[X-SKIP] {target_file.name} is an HB primitive; it is simulated as part of its parent HB/X block."
                )
                continue
            if node_class not in {"hbsolve_block", "hbsolve_primitive"}:
                raise ValueError(
                    f"{target_file.name} has x-params=true but is classified as {node_class!r}. "
                    "x_simluation.py only simulates HB/X JSONs. If this should be "
                    "supported, please specify how S-param dependencies should be "
                    "converted before X extraction."
                )

            cache_key = stable_json_hash(
                {
                    "json": cache_key_for_json(target_file),
                    "x_simulation_cache_version": X_SIMULATION_CACHE_VERSION,
                }
            )
            cell_name = target_file.stem

            cached_csv = lookup_cached_csv(cache_dir, cell_name)
            if cached_csv is not None:
                manifest = load_cache_manifest(cache_dir)
                entry = manifest.get(cell_name, {})
                if entry.get("cache_key") == cache_key:
                    print(f"[CACHE HIT] {target_file.name} -> {cached_csv.name}")
                    continue

            csv_cache = cache_dir / f"{cell_name}_{cache_key}.csv"
            jl_script = cache_dir / f"run_x_{cell_name}_{cache_key}.jl"

            print(f"[X-SIMULATE] Queued {target_file.name} ({node_class})...")
            x_paths = generate_hbsolve_xparams_script(
                target_file,
                cache_dir,
                jl_script,
                csv_cache,
            )

            debug_jl_script = cache_dir / f"{cell_name}_{cache_key}_debug.jl"
            with open(jl_script, "r") as f:
                jl_source = f.read()
            with open(debug_jl_script, "w") as f:
                f.write(jl_source)
            print(f"      -> Saved Julia debug script to: {debug_jl_script}")

            jobs.append(
                {
                    "target_file": target_file,
                    "node_class": node_class,
                    "cache_key": cache_key,
                    "cell_name": cell_name,
                    "csv_cache": csv_cache,
                    "jl_script": jl_script,
                    "x_paths": x_paths,
                }
            )

        if not jobs:
            continue

        batch_script = cache_dir / f"run_x_batch_level_{level_idx:03d}.jl"
        run_julia_batch(jobs, cache_dir, batch_script)

        for job in jobs:
            update_cache_manifest(
                cache_dir,
                job["cell_name"],
                job["cache_key"],
                job["csv_cache"],
            )
            update_x_cache_manifest(
                cache_dir,
                job["cell_name"],
                job["cache_key"],
                job["csv_cache"],
                job["x_paths"],
            )

    print("\nX-parameter simulation pipeline complete.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run X-parameter simulation for a single target JSON file."
    )
    parser.add_argument(
        "target",
        help="Target JSON file, e.g. example_two_twpa_x/two_twpas_series.json",
    )

    args = parser.parse_args()
    current_dir = Path(__file__).parent.resolve() if "__file__" in globals() else Path.cwd().resolve()
    orchestrate_x_simulation(args.target, current_dir)
