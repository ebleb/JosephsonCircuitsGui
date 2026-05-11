using DelimitedFiles
using LinearAlgebra
using JosephsonCircuits

Z0 = 50.0
z0 = 50.0
z_0 = 50.0
Z_0 = 50.0

ws = 2 * pi * range(4.5, 5.0, length=200) * 1e9
wp = (2 * pi * 4.65001 * 1e9, 2 * pi * 4.85001 * 1e9,)
Ip_1 = 0.00565e-6*1.7
Ip_2 = 0.00565e-6*1.7




circuit = Any[]

push!(circuit, ("P_JP1_P1", 0, 1, 1))
push!(circuit, ("R_JP1_R1", 0, 1, 50.0))
push!(circuit, ("C_JP1_C1", 1, 2, 100e-15))
push!(circuit, ("Lj_JP1_LJ1", 2, 0, 1e-9))
push!(circuit, ("C_JP1_C2", 2, 0, 1e-12))

sources = [(mode=(1,0,), port=1, current=Ip_1), (mode=(0,1,), port=1, current=Ip_2)]

println("Running hbsolve on double_pumped_JPA.json...")
rpm = hbsolve(
    ws,
    wp,
    sources,
    (8, 8,),
    (16, 16,),
    circuit,
    Dict();
    threewavemixing=true,
    fourwavemixing=true,
)

function save_hbsolve_result()
    num_freqs = length(ws)
    ports = (1,)
    sol_ports = [("JP1_P1", 2)]
    expected_ports = [("JP1_P1", 2), ("JP1_P1", 1), ("JP1_P1", 2), ("JP1_P1", 1)]
    num_ports = length(ports)

    S_matrix = zeros(ComplexF64, num_ports, num_ports, num_freqs)

    for out_idx in 1:num_ports
        for in_idx in 1:num_ports
            out_p = ports[out_idx]
            in_p = ports[in_idx]
            S_matrix[out_idx, in_idx, :] .= rpm.linearized.S((0,0,), out_p, (0,0,), in_p, :)
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

    writedlm("/home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_kgki9mfn/cache/double_pumped_JPA_a4a207bd1805097b.csv", out_data, ',')
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
save_nodeflux_csv("/home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_kgki9mfn/cache/double_pumped_JPA_a4a207bd1805097b_nodeflux.csv")
println("Saved HBSolve cache to double_pumped_JPA_a4a207bd1805097b.csv")
println("Saved nodeflux cache to double_pumped_JPA_a4a207bd1805097b_nodeflux.csv")
