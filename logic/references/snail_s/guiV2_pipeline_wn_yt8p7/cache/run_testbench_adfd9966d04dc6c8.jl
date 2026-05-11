using DelimitedFiles
using LinearAlgebra
using JosephsonCircuits

Z0 = 50.0
z0 = 50.0
z_0 = 50.0
Z_0 = 50.0

ws = 2 * pi * range(7.8, 8.2, length=200) * 1e9
wp = (2 * pi * 16.0 * 1e9,)
Ip_1 = 4.4e-6
Idc_1 = 0.000159



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

sources = [(mode=(1,), port=2, current=Ip_1), (mode=(0,), port=2, current=Idc_1)]

println("Running hbsolve on testbench.json...")
rpm = hbsolve(
    ws,
    wp,
    sources,
    (8,),
    (16,),
    circuit,
    Dict();
    threewavemixing=true,
    fourwavemixing=true,
    dc=true,
)

function save_hbsolve_result()
    num_freqs = length(ws)
    ports = (1, 2)
    sol_ports = [("SN1_P1", 2), ("SN1_P2", 2)]
    expected_ports = [("SN1_P1", 2), ("SN1_P2", 2)]
    num_ports = length(ports)

    S_matrix = zeros(ComplexF64, num_ports, num_ports, num_freqs)

    for out_idx in 1:num_ports
        for in_idx in 1:num_ports
            out_p = ports[out_idx]
            in_p = ports[in_idx]
            S_matrix[out_idx, in_idx, :] .= rpm.linearized.S((0,), out_p, (0,), in_p, :)
        end
    end

    S_matrix = apply_port_order(S_matrix, sol_ports, expected_ports)
    num_ports = size(S_matrix, 1)

    out_data = zeros(Float64, num_freqs, 1 + 2 * (num_ports^2))
    out_data[:, 1] = ws ./ (2*pi*1e9)

    col = 2
    for out_idx in 1:num_ports
        for in_idx in 1:num_ports
            out_data[:, col] = real.(S_matrix[out_idx, in_idx, :])
            out_data[:, col + 1] = imag.(S_matrix[out_idx, in_idx, :])
            col += 2
        end
    end

    writedlm("/home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_wn_yt8p7/cache/testbench_adfd9966d04dc6c8.csv", out_data, ',')
end

function apply_port_order(S_k, sol_ports, expected_ports)
    if expected_ports === nothing
        return S_k
    end

    perm = Int[]
    missing = Tuple{String, Int}[]

    for ep in expected_ports
        idx = findfirst(p -> p == ep, sol_ports)
        if idx === nothing
            push!(missing, ep)
            continue
        end
        push!(perm, idx)
    end

    if !isempty(missing)
        println("[WARN] JSON exported port(s) not present as HB ports: ", missing)
        println("       HB ports: ", sol_ports)
    end

    if isempty(perm)
        error("None of the JSON expected ports were found in HB output ports $(sol_ports)")
    end

    return S_k[perm, perm, :]
end

function save_nodeflux_csv(filepath)
    modes = rpm.nonlinear.modes
    freqs = JosephsonCircuits.calcmodefreqs(rpm.nonlinear.w, rpm.nonlinear.modes) ./ (2*pi*1e9)
    values = vec(rpm.nonlinear.nodeflux)
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

save_hbsolve_result()
save_nodeflux_csv("/home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_wn_yt8p7/cache/testbench_adfd9966d04dc6c8_nodeflux.csv")
println("Saved HBSolve cache to testbench_adfd9966d04dc6c8.csv")
println("Saved nodeflux cache to testbench_adfd9966d04dc6c8_nodeflux.csv")
