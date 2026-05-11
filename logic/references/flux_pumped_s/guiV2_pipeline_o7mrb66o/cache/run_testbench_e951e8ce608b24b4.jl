using DelimitedFiles
using LinearAlgebra
using JosephsonCircuits

Z0 = 50.0
z0 = 50.0
z_0 = 50.0
Z_0 = 50.0

ws = 2 * pi * range(9.7, 9.8, length=2000) * 1e9
wp = (2 * pi * 19.5 * 1e9,)
Ip_1 = 0.7e-6
Idc_1 = 140.3e-6



circuit = Any[]

push!(circuit, ("P_FL1_P1", 0, 1, 1))
push!(circuit, ("R_FL1_R1", 0, 1, (50)))
push!(circuit, ("L_FL1_L1", 0, 1, (100.0e-9)))
push!(circuit, ("C_FL1_C1", 1, 2, (16.0e-15)))
push!(circuit, ("L_FL1_L2", 2, 3, (0.4264e-9)))
push!(circuit, ("C_FL1_C2", 0, 2, (0.4e-12)))
push!(circuit, ("Lj_FL1_LJ1", 0, 3, (219.63e-12)))
push!(circuit, ("Cj_FL1_CJ1", 0, 3, (10.0e-15)))
push!(circuit, ("L_FL1_L3", 3, 4, (34e-12)))
push!(circuit, ("Lj_FL1_LJ2", 0, 4, (219.63e-12)))
push!(circuit, ("Cj_FL1_CJ2", 0, 4, (10.0e-15)))
push!(circuit, ("L_FL1_L4", 0, 5, (0.74e-12)))
push!(circuit, ("P_FL1_P2", 0, 5, 2))
push!(circuit, ("R_FL1_R2", 0, 5, 1e3))
push!(circuit, ("K", "L_FL1_L3", "L_FL1_L4", (0.999)))

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
    sol_ports = [("FL1_P1", 2), ("FL1_P2", 2)]
    expected_ports = [("FL1_P1", 2), ("FL1_P2", 2)]
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

    writedlm("/home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_o7mrb66o/cache/testbench_e951e8ce608b24b4.csv", out_data, ',')
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
save_nodeflux_csv("/home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_o7mrb66o/cache/testbench_e951e8ce608b24b4_nodeflux.csv")
println("Saved HBSolve cache to testbench_e951e8ce608b24b4.csv")
println("Saved nodeflux cache to testbench_e951e8ce608b24b4_nodeflux.csv")
