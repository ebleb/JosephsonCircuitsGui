using DelimitedFiles
using LinearAlgebra
using JosephsonCircuits

Z0 = 50.0
z0 = 50.0
z_0 = 50.0
Z_0 = 50.0

ws = 2 * pi * range(1.0, 20.0, length=500) * 1e9

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
    expected_ports = [("AD3", 3), ("AD1", 1), ("AD2", 2)]

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

    push!(networks, ("AD1", load_s_matrix_at("/home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_es3gz2g0/cache/add_drop__override_second_half_AD1_b95fd040f5c8cb82.csv", k)))
    push!(networks, ("AD2", load_s_matrix_at("/home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_es3gz2g0/cache/add_drop__override_second_half_AD2_f50e711227230a5e.csv", k)))
    push!(networks, ("TW1", load_s_matrix_at("/home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_es3gz2g0/cache/twpa_72b05a9c56a8a2b2.csv", k)))
    push!(networks, ("AD3", load_s_matrix_at("/home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_es3gz2g0/cache/add_drop__override_second_half_AD3_d779f343ed9f58ec.csv", k)))
    push!(networks, ("ST1", let
        R_shunt = 50
        ComplexF64.(reshape(ComplexF64[ComplexF64((R_shunt - Z0) / (R_shunt + Z0))], 1, 1))
    end))
    push!(networks, ("ST2", let
        R_shunt = 50
        ComplexF64.(reshape(ComplexF64[ComplexF64((R_shunt - Z0) / (R_shunt + Z0))], 1, 1))
    end))
    push!(networks, ("ST4", let
        R_shunt = 50
        ComplexF64.(reshape(ComplexF64[ComplexF64((R_shunt - Z0) / (R_shunt + Z0))], 1, 1))
    end))
    push!(networks, ("ST5", let
        R_shunt = 50
        ComplexF64.(reshape(ComplexF64[ComplexF64((R_shunt - Z0) / (R_shunt + Z0))], 1, 1))
    end))
    push!(networks, ("ST3", let
        R_shunt = 50
        ComplexF64.(reshape(ComplexF64[ComplexF64((R_shunt - Z0) / (R_shunt + Z0))], 1, 1))
    end))

    return networks
end

function build_connections()
    connections = Vector{Vector{Tuple{String, Int}}}()

    push!(connections, [("AD1", 2), ("ST2", 1)])
    push!(connections, [("ST5", 1), ("AD3", 4)])
    push!(connections, [("AD1", 3), ("AD2", 1)])
    push!(connections, [("ST1", 1), ("AD1", 4)])
    push!(connections, [("AD2", 3), ("TW1", 2)])
    push!(connections, [("AD2", 4), ("ST3", 1)])
    push!(connections, [("TW1", 1), ("AD3", 1)])
    push!(connections, [("AD3", 2), ("ST4", 1)])

    return connections
end

connections = build_connections()

println("Running solveS on second_half.json...")

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

save_s_matrix("/home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_es3gz2g0/cache/second_half_167835c3e8ed3f5c.csv", ws, S_first)
println("Saved solveS cache to second_half_167835c3e8ed3f5c.csv")
