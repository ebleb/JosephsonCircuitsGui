using DelimitedFiles
using LinearAlgebra
using JosephsonCircuits

Z0 = 50.0
z0 = 50.0
z_0 = 50.0
Z_0 = 50.0
C_cross = 1/1000000000000000

ws = 2 * pi * range(1.0, 20.0, length=200) * 1e9

function load_s_matrix(filepath)
    in_data = readdlm(filepath, ',', Float64)
    num_freqs = size(in_data, 1)
    num_ports = Int(sqrt((size(in_data, 2) - 1) / 2))

    S = zeros(ComplexF64, num_ports, num_ports, num_freqs)

    col = 2
    for out_p in 1:num_ports
        for in_p in 1:num_ports
            S[out_p, in_p, :] = in_data[:, col] .+ im .* in_data[:, col + 1]
            col += 2
        end
    end

    return S
end

function load_s_matrix_at(filepath, k)
    S = load_s_matrix(filepath)
    return ComplexF64.(S[:, :, k])
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

function extract_s_matrix(sol)
    if hasproperty(sol, :S)
        return sol.S
    elseif sol isa AbstractArray
        return sol
    else
        error("Cannot extract S-matrix from solveS result. Expected sol.S or array.")
    end
end

function solve_networks(networks, connections)
    if isempty(connections) && length(networks) == 1
        name, S = networks[1]
        ports = [(name, p) for p in 1:size(S, 1)]
        return reshape(S, size(S, 1), size(S, 2), 1), ports
    end

    sol = solveS(networks, connections)
    return extract_s_matrix(sol), sol.ports
end

function apply_port_order(S_k, sol_ports)
    expected_ports = [("ASS1", 1), ("ASS1", 2), ("AS1", 2), ("ASS1", 4)]

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
        println("[WARN] JSON exported port(s) not present as external solveS ports: ", missing)
        println("       solveS external ports: ", sol_ports)
    end

    if isempty(perm)
        error("None of the JSON expected ports were found in solveS output ports $(sol_ports)")
    end

    return S_k[perm, perm, :]
end

function build_networks(w, k)
    networks = Tuple{String, Matrix{ComplexF64}}[]

    push!(networks, ("AS1", load_s_matrix_at("/home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_uz__3i91/cache/airbridge_signal2signal__p_ff5ba53951_095b304fb963ba3c.csv", k)))
    push!(networks, ("ASS1", let
        Y = im*w/1000000000000000
        ComplexF64.(JosephsonCircuits.AtoS(ComplexF64.(ComplexF64.([[1 0; 0 1] [0 0; 0 0];[Y -Y; -Y Y]   [1 0; 0 1]]))))
    end))

    return networks
end

function build_connections()
    connections = Vector{Vector{Tuple{String, Int}}}()

    push!(connections, [("ASS1", 3), ("AS1", 1)])

    return connections
end

connections = build_connections()

println("Running solveS on airbridge_signal2signal_crossing__p_2a0171e07a.json...")

S_first = nothing

for k in eachindex(ws)
    w = ws[k]
    networks = build_networks(w, k)
    S_k, sol_ports = solve_networks(networks, connections)
    S_k = apply_port_order(S_k, sol_ports)
    S_k = ComplexF64.(S_k)

    if S_first === nothing
        global S_first = zeros(ComplexF64, size(S_k, 1), size(S_k, 2), length(ws))
    end

    S_first[:, :, k] .= S_k[:, :, 1]
end

save_s_matrix("/home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_uz__3i91/cache/airbridge_signal2signal_crossing__p_2a0171e07a_1f16cb6797b30de5.csv", ws, S_first)
println("Saved solveS cache to airbridge_signal2signal_crossing__p_2a0171e07a_1f16cb6797b30de5.csv")
