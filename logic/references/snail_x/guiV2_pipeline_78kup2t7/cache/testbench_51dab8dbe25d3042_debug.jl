using DelimitedFiles
using LinearAlgebra
using JosephsonCircuits

Z0 = 50.0
z0 = 50.0
z_0 = 50.0
Z_0 = 50.0

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
    conjugate_partner = l -> 0 - l,
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
    conjugate_partner = l -> 0 - l,
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
        threewavemixing=true,
        fourwavemixing=true,
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
        "l -> 0 - l",
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
        println(io, "{")
        println(io, "  \"indexing\": \"port-major saved-port order\",")
        println(io, "  \"raw_index\": \"(port_index - 1) * Nmodes + mode_index\",")
        println(io, "  \"ports\": [", join(string.(ports), ", "), "],")
        println(io, "  \"matrix_ports\": [", join(string.(ports), ", "), "],")
        println(io, "  \"raw_ports\": [", join(string.(raw_ports), ", "), "],")
        println(io, "  \"saved_signal_ports\": [", join(string.(ports), ", "), "],")
        println(io, "  \"signal_modes\": [", join([string(m[1]) for m in xp.signal_modes], ", "), "],")
        println(io, "  \"pump_modes\": [", join([string(m[1]) for m in xp.pump_modes], ", "), "],")
        println(io, "  \"pump_current\": \"", string(xp.pump_current), "\",")
        println(io, "  \"pump_phase\": \"", string(xp.P), "\",")
        println(io, "  \"pump_port\": 2,")
        println(io, "  \"pump_ports\": [2],")

        println(io, "  \"dc_enabled\": true,")
        println(io, "  \"dc_port\": 2,")
        println(io, "  \"dc_ports\": [2],")
        println(io, "  \"dc_current\": \"", string(Idc[1]), "\",")
        println(io, "  \"dc_currents\": [", join(["\"" * string(v) * "\"" for v in Idc], ", "), "],")
        println(io, "  \"conjugate_partner\": \"l -> 0 - l\",")
        println(io, "  \"x_matrix_note\": \"XS_full and XT_full are reordered to matrix_ports, matching the saved signal-only S CSV.\"")
        println(io, "}")
    end
end

ws = 2 * pi * range(7.8, 8.2, length=200) * 1e9
wp = (2 * pi * 16.0 * 1e9,)
Ip = [4.4e-6]
Idc = [0.000159]
sources = [(mode=(0,), port=2, current=Idc[1]), (mode=(1,), port=2, current=Ip[1])]



circuit = Any[]

push!(circuit, ("P_SN1_P1", 0, 1, 1))
push!(circuit, ("R_SN1_R1", 0, 1, (50.0)))
push!(circuit, ("L_SN1_L1", 0, 1, (100.0e-9)))
push!(circuit, ("C_SN1_C1", 1, 2, (0.048e-12)))
push!(circuit, ("L_SN1_L2", 2, 3, (0.4264e-9*1.25)))
push!(circuit, ("C_SN1_C2", 0, 2, (0.4e-12*1.25)))
push!(circuit, ("Lj_SN1_LJ1", 0, 3, (60e-12)/( 0.29)))
push!(circuit, ("Cj_SN1_CJ1", 0, 3, (10.0e-15)/( 0.29)))
push!(circuit, ("L_SN1_L3", 3, 4, (34e-12)))
push!(circuit, ("Lj_SN1_LJ2", 4, 5, (60e-12)))
push!(circuit, ("Cj_SN1_CJ2", 4, 5, (10.0e-15)))
push!(circuit, ("Cj_SN1_CJ3", 5, 6, (10.0e-15)))
push!(circuit, ("Lj_SN1_LJ3", 5, 6, (60e-12)))
push!(circuit, ("Lj_SN1_LJ4", 0, 6, (60e-12)))
push!(circuit, ("Cj_SN1_CJ4", 0, 6, (10.0e-15)))
push!(circuit, ("L_SN1_L4", 0, 7, ( 0.74e-12)))
push!(circuit, ("P_SN1_P2", 0, 7, 2))
push!(circuit, ("R_SN1_R2", 0, 7, 1e3))
push!(circuit, ("K", "L_SN1_L3", "L_SN1_L4", (0.999)))

circuitdefs = Dict()

println("Running X-parameter hbsolve on testbench.json...")
xp = hbsolve_xparams(
    ws,
    wp,
    Ip,
    sources,
    (8,),
    (16,),
    circuit,
    circuitdefs;
    returnS=true,
    returnSnoise=false,
    returnQE=true,
    returnCM=true,
    dc=true,
)

rpm = xp.hb
ports = (1, 2)
num_ports = length(ports)
num_freqs = length(ws)
modes = rpm.linearized.modes
Nmodes = length(modes)
sig_idx = findfirst(m -> m == (0,), modes)
sig_idx === nothing && error("Signal mode (0,) was not found in linearized modes.")

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

save_s_matrix("/home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_78kup2t7/cache/testbench_51dab8dbe25d3042.csv", ws, S_signal)
save_s_matrix("/home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_78kup2t7/cache/testbench_51dab8dbe25d3042_x_XS_full.csv", ws, XS_saved)
save_s_matrix("/home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_78kup2t7/cache/testbench_51dab8dbe25d3042_x_XT_full.csv", ws, XT_saved)
save_xfb_csv("/home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_78kup2t7/cache/testbench_51dab8dbe25d3042_x_XFB.csv", xp)
save_x_modes_json("/home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_78kup2t7/cache/testbench_51dab8dbe25d3042_x_modes.json", xp)
save_nodeflux_csv("/home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_78kup2t7/cache/testbench_51dab8dbe25d3042_nodeflux.csv", xp)

println("Saved X-compatible signal S cache to testbench_51dab8dbe25d3042.csv")
println("Saved XFB/XS/XT/nodeflux outputs next to the cache CSV")
