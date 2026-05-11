using DelimitedFiles
using LinearAlgebra
using JosephsonCircuits

Z0 = 50.0
z0 = 50.0
z_0 = 50.0
Z_0 = 50.0

ws = 2 * pi * range(1.0, 20.0, length=200) * 1e9

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

println("Running built-in sSolve on ABCD_shunt_signal2signal__p_81d3729db0.json...")

S_first = nothing

for k in eachindex(ws)
    w = ws[k]
    Y = im*w/1000000000000000

    S_k = JosephsonCircuits.AtoS(ComplexF64.(ComplexF64.([[1 0; 0 1] [0 0; 0 0];[Y -Y; -Y Y]   [1 0; 0 1]])))

    if S_first === nothing
        global S_first = zeros(ComplexF64, size(S_k, 1), size(S_k, 2), length(ws))
    end

    S_first[:, :, k] .= S_k
end

save_s_matrix("/home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_uz__3i91/cache/ABCD_shunt_signal2signal__p_81d3729db0_03bbda9e404e53c8.csv", ws, S_first)
println("Saved built-in sSolve cache to ABCD_shunt_signal2signal__p_81d3729db0_03bbda9e404e53c8.csv")
