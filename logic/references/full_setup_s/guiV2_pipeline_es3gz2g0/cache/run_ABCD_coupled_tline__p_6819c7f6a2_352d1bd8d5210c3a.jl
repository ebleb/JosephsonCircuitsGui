using DelimitedFiles
using LinearAlgebra
using JosephsonCircuits

Z0 = 50.0
z0 = 50.0
z_0 = 50.0
Z_0 = 50.0

ws = 2 * pi * range(1.0, 20.0, length=500) * 1e9

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

println("Running built-in sSolve on ABCD_coupled_tline__p_6819c7f6a2.json...")

S_first = nothing

for k in eachindex(ws)
    w = ws[k]
    Z0e = 1995976205172263/25000000000000
    Z0o = 31268051050244907/1000000000000000
    thetae = 3258756162624490679*w/107068735000000000000000000000
    thetao = 573949341867093761*w/18737028625000000000000000000

    S_k = JosephsonCircuits.AtoS(ComplexF64.(JosephsonCircuits.ABCD_coupled_tline(Z0e, Z0o, thetae, thetao)))

    if S_first === nothing
        global S_first = zeros(ComplexF64, size(S_k, 1), size(S_k, 2), length(ws))
    end

    S_first[:, :, k] .= S_k
end

save_s_matrix("/home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_es3gz2g0/cache/ABCD_coupled_tline__p_6819c7f6a2_352d1bd8d5210c3a.csv", ws, S_first)
println("Saved built-in sSolve cache to ABCD_coupled_tline__p_6819c7f6a2_352d1bd8d5210c3a.csv")
