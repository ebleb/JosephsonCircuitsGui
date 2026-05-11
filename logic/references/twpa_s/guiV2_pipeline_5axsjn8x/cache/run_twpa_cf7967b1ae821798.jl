using DelimitedFiles
using LinearAlgebra
using JosephsonCircuits

Z0 = 50.0
z0 = 50.0
z_0 = 50.0
Z_0 = 50.0
z0 = 50.0

ws = 2 * pi * range(1.0, 14.0, length=200) * 1e9
wp = (2 * pi * 7.12 * 1e9,)
Ip_1 = 1.85e-06



function S1_link_node(rep_idx)
    return 8 + rep_idx - 1
end

function S1_node(local_node, rep_idx)
    if local_node == 0
        return 0
    elseif local_node == 2
        return rep_idx == 1 ? 2 : S1_link_node(rep_idx - 1)
    elseif local_node == 1
        return rep_idx == 512 ? 1 : S1_link_node(rep_idx)
    elseif local_node == 3
        return 519 + (rep_idx - 1) * 4 + 0
    elseif local_node == 4
        return 519 + (rep_idx - 1) * 4 + 1
    elseif local_node == 5
        return 519 + (rep_idx - 1) * 4 + 2
    elseif local_node == 6
        return 519 + (rep_idx - 1) * 4 + 3
    else
        error("Unknown local node $(local_node) in repeated HB group S1")
    end
end


circuit = Any[]

push!(circuit, ("C_OT1_C1", 1, 0, (45.0e-15)/2))
push!(circuit, ("R_OT1_R1", 1, 0, (50)))
push!(circuit, ("P_OT1_P2", 1, 0, 2))
push!(circuit, ("P_IT1_P1", 0, 7, 1))
push!(circuit, ("R_IT1_R1", 0, 7, ((50.0))))
push!(circuit, ("C_IT1_C1", 0, 7, (45.0e-15)/2))
push!(circuit, ("Lj_IT1_L1", 7, 2, (IctoLj(3.4e-6))))
push!(circuit, ("C_IT1_C2", 7, 2, (55e-15)))
for rep_idx in 1:512
    push!(circuit, ("C_S1_TCM1_C1_$(rep_idx)", S1_node(2, rep_idx), S1_node(0, rep_idx), (45.0e-15) - (30.0e-15)))
    push!(circuit, ("C_S1_TCM1_C2_$(rep_idx)", S1_node(2, rep_idx), S1_node(3, rep_idx), (30.0e-15)))
    push!(circuit, ("Lj_S1_TCM1_L1_$(rep_idx)", S1_node(2, rep_idx), S1_node(4, rep_idx), (IctoLj(3.4e-6))))
    push!(circuit, ("C_S1_TCM1_C3_$(rep_idx)", S1_node(2, rep_idx), S1_node(4, rep_idx), (55e-15)))
    push!(circuit, ("C_S1_TCM1_C4_$(rep_idx)", S1_node(3, rep_idx), S1_node(0, rep_idx), (2.8153e-12)))
    push!(circuit, ("L_S1_TCM1_L2_$(rep_idx)", S1_node(3, rep_idx), S1_node(0, rep_idx), (1.70e-10)))
    push!(circuit, ("C_S1_TC1_C1_$(rep_idx)", S1_node(4, rep_idx), S1_node(0, rep_idx), (45.0e-15)))
    push!(circuit, ("Lj_S1_TC1_L1_$(rep_idx)", S1_node(4, rep_idx), S1_node(5, rep_idx), (IctoLj(3.4e-6))))
    push!(circuit, ("C_S1_TC1_C2_$(rep_idx)", S1_node(4, rep_idx), S1_node(5, rep_idx), (55e-15)))
    push!(circuit, ("C_S1_TC2_C1_$(rep_idx)", S1_node(5, rep_idx), S1_node(0, rep_idx), (45.0e-15)))
    push!(circuit, ("Lj_S1_TC2_L1_$(rep_idx)", S1_node(5, rep_idx), S1_node(6, rep_idx), (IctoLj(3.4e-6))))
    push!(circuit, ("C_S1_TC2_C2_$(rep_idx)", S1_node(5, rep_idx), S1_node(6, rep_idx), (55e-15)))
    push!(circuit, ("C_S1_TC3_C1_$(rep_idx)", S1_node(6, rep_idx), S1_node(0, rep_idx), (45.0e-15)))
    push!(circuit, ("Lj_S1_TC3_L1_$(rep_idx)", S1_node(6, rep_idx), S1_node(1, rep_idx), (IctoLj(3.4e-6))))
    push!(circuit, ("C_S1_TC3_C2_$(rep_idx)", S1_node(6, rep_idx), S1_node(1, rep_idx), (55e-15)))
end

sources = [(mode=(1,), port=1, current=Ip_1)]

println("Running hbsolve on twpa.json...")
rpm = hbsolve(
    ws,
    wp,
    sources,
    (10,),
    (20,),
    circuit,
    Dict();
    threewavemixing=true,
    fourwavemixing=true,
)

function save_hbsolve_result()
    num_freqs = length(ws)
    ports = (1, 2)
    sol_ports = [("IT1_P1", 2), ("OT1_P2", 1)]
    expected_ports = [("OT1_P2", 1), ("IT1_P1", 2)]
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

    writedlm("/home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_5axsjn8x/cache/twpa_cf7967b1ae821798.csv", out_data, ',')
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
save_nodeflux_csv("/home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_5axsjn8x/cache/twpa_cf7967b1ae821798_nodeflux.csv")
println("Saved HBSolve cache to twpa_cf7967b1ae821798.csv")
println("Saved nodeflux cache to twpa_cf7967b1ae821798_nodeflux.csv")

# ============================================================
# Multimode HBSolve post-processing
# ============================================================

function save_multimode_hbsolve_result()
    ports = (1, 2)
    num_ports = length(ports)
    num_freqs = length(ws)

    println("Running multimode HBSolve post-processing...")

    # Auto-detect available modes.
    modes_detected = Tuple{Int}[]

    for n in -20:20
        found = false

        for out_p in ports
            for in_p in ports
                try
                    rpm.linearized.S((n,), out_p, (0,), in_p, 1)
                    found = true
                    break
                catch
                    # Not available for this port pair.
                end
            end

            if found
                break
            end
        end

        if found
            push!(modes_detected, (n,))
        end
    end

    sort!(modes_detected, by = m -> m[1])

    if isempty(modes_detected)
        error("Multimode requested, but no HBSolve modes were detected.")
    end

    ref_mode_idx = findfirst(m -> m == (0,), modes_detected)
    if ref_mode_idx === nothing
        error("Reference input mode (0,) was not detected. Detected modes: $(modes_detected)")
    end

    requested_ref_input_port = 1
    ref_input_port = requested_ref_input_port === nothing ? ports[1] : requested_ref_input_port

    ref_input_port_idx = findfirst(p -> p == ref_input_port, ports)
    if ref_input_port_idx === nothing
        error("Reference input port $(ref_input_port) not found in HBSolve ports $(ports). Check hb_input_field or multimode_reference_input_port.")
    end

    requested_ref_output_port = 2
    ref_output_port = requested_ref_output_port === nothing ? ports[end] : requested_ref_output_port

    ref_output_port_idx = findfirst(p -> p == ref_output_port, ports)
    if ref_output_port_idx === nothing
        error("Reference output port $(ref_output_port) not found in HBSolve ports $(ports). Check hb_output_field or multimode_reference_output_port.")
    end

    num_modes = length(modes_detected)
    full_size = num_modes * num_ports

    println("Detected $(num_modes) modes: $(modes_detected)")
    println("Full multimode S-matrix size: $(full_size)×$(full_size)×$(num_freqs)")
    println("Reference input:  mode (0,), port $(ref_input_port)")
    println("Reference output: port $(ref_output_port)")

    S_full = zeros(ComplexF64, full_size, full_size, num_freqs)

    for (oi, out_mode) in enumerate(modes_detected)
        for (out_port_idx, out_port) in enumerate(ports)
            row = (oi - 1) * num_ports + out_port_idx

            for (ii, in_mode) in enumerate(modes_detected)
                for (in_port_idx, in_port) in enumerate(ports)
                    col = (ii - 1) * num_ports + in_port_idx

                    try
                        S_full[row, col, :] .= rpm.linearized.S(
                            out_mode,
                            out_port,
                            in_mode,
                            in_port,
                            :
                        )
                    catch
                        # Missing combinations are left as zero.
                    end
                end
            end
        end
    end

    # Save metadata.
    open("/home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_5axsjn8x/cache/twpa_cf7967b1ae821798_multimode_modes.json", "w") do io
        println(io, "{")
        println(io, "  \"ports\": ", collect(ports), ",")
        println(io, "  \"modes\": ", [m[1] for m in modes_detected], ",")
        println(io, "  \"indexing\": \"row_or_col = (mode_index - 1) * num_ports + port_index; port_index indexes the listed JosephsonCircuits ports\",")
        println(io, "  \"reference_input_mode\": 0,")
        println(io, "  \"reference_input_port\": ", ref_input_port, ",")
        println(io, "  \"reference_output_port\": ", ref_output_port, ",")
        println(io, "  \"hb_input_pin_name\": \"P_in\",")
        println(io, "  \"hb_output_pin_name\": \"P_out\"")
        println(io, "}")
    end

    # Full multimode CSV format:
    # freq_GHz, then for each matrix element row-major:
    # real(S[row,col]), imag(S[row,col])
    full_out = zeros(Float64, num_freqs, 1 + 2 * full_size^2)
    full_out[:, 1] = ws ./ (2*pi*1e9)

    col = 2
    for row in 1:full_size
        for colidx in 1:full_size
            full_out[:, col] = real.(S_full[row, colidx, :])
            full_out[:, col + 1] = imag.(S_full[row, colidx, :])
            col += 2
        end
    end

    writedlm("/home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_5axsjn8x/cache/twpa_cf7967b1ae821798_multimode_full.csv", full_out, ',')

    # Reference column for statistics:
    # input mode = ref_input_mode, input port = ref_input_port.
    ref_col = (ref_mode_idx - 1) * num_ports + ref_input_port_idx

    # Targeted forward row for same-mode signal transmission:
    # output mode = ref_input_mode, output port = ref_output_port.
    ref_out_row = (ref_mode_idx - 1) * num_ports + ref_output_port_idx

    k_peak = argmax(abs.(S_full[ref_out_row, ref_col, :]))
    f_peak_GHz = ws[k_peak] / (2*pi*1e9)

    # ------------------------------------------------------------
    # Power by mode at peak, summed over all physical output ports.
    # ------------------------------------------------------------
    power_by_mode = zeros(Float64, num_modes)
    signed_power_by_mode = zeros(Float64, num_modes)

    for (mi, mode) in enumerate(modes_detected)
        row_start = (mi - 1) * num_ports + 1
        row_stop = mi * num_ports
        power_by_mode[mi] = sum(abs.(S_full[row_start:row_stop, ref_col, k_peak]).^2)

        sigma = mode[1] >= 0 ? 1.0 : -1.0
        signed_power_by_mode[mi] = sigma * power_by_mode[mi]
    end

    total_power = sum(power_by_mode)
    signed_total_power = sum(signed_power_by_mode)

    power_out = zeros(Float64, num_modes, 6)
    # columns:
    # mode_n, physical_frequency_GHz_at_peak, power, percent_total, sigma, signed_power
    for (mi, mode) in enumerate(modes_detected)
        mode_n = mode[1]
        physical_freq_GHz = f_peak_GHz + mode_n * (wp[1] / (2*pi*1e9))
        pct = total_power == 0 ? 0.0 : 100.0 * power_by_mode[mi] / total_power
        sigma = mode_n >= 0 ? 1.0 : -1.0

        power_out[mi, 1] = mode_n
        power_out[mi, 2] = physical_freq_GHz
        power_out[mi, 3] = power_by_mode[mi]
        power_out[mi, 4] = pct
        power_out[mi, 5] = sigma
        power_out[mi, 6] = signed_power_by_mode[mi]
    end

    writedlm(
        "/home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_5axsjn8x/cache/twpa_cf7967b1ae821798_multimode_power.csv",
        vcat(
            reshape([
                "mode_n",
                "physical_frequency_GHz_at_peak",
                "power_all_output_ports",
                "percent_total",
                "sigma",
                "signed_power"
            ], 1, 6),
            power_out
        ),
        ','
    )

    # ------------------------------------------------------------
    # Targeted conversion data:
    # fixed input = (ref_input_mode, ref_input_port)
    # fixed output port = ref_output_port
    # output mode varies.
    #
    # This is the clean data for plotting:
    #   signal gain:       output mode 0
    #   idler conversion:  output mode e.g. -2 for 4WM convention
    # ------------------------------------------------------------
    requested_conversion_modes = nothing

    conversion_mode_indices = Int[]
    for (mi, mode) in enumerate(modes_detected)
        if requested_conversion_modes === nothing || mode[1] in requested_conversion_modes
            push!(conversion_mode_indices, mi)
        end
    end

    conversion_out = zeros(Float64, num_freqs, 1 + 4 * length(conversion_mode_indices))
    conversion_out[:, 1] = ws ./ (2*pi*1e9)

    conversion_header = ["frequency_GHz"]

    conv_col = 2
    for mi in conversion_mode_indices
        mode_n = modes_detected[mi][1]
        row = (mi - 1) * num_ports + ref_output_port_idx
        amp = S_full[row, ref_col, :]

        conversion_out[:, conv_col] = real.(amp)
        conversion_out[:, conv_col + 1] = imag.(amp)
        conversion_out[:, conv_col + 2] = abs.(amp)
        conversion_out[:, conv_col + 3] = abs.(amp).^2

        push!(conversion_header, "mode_$(mode_n)_real")
        push!(conversion_header, "mode_$(mode_n)_imag")
        push!(conversion_header, "mode_$(mode_n)_abs")
        push!(conversion_header, "mode_$(mode_n)_power")

        conv_col += 4
    end

    writedlm(
        "/home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_5axsjn8x/cache/twpa_cf7967b1ae821798_multimode_conversions.csv",
        vcat(reshape(conversion_header, 1, length(conversion_header)), conversion_out),
        ','
    )

    println("At peak signal gain: f = $(round(f_peak_GHz, digits=6)) GHz")
    println("Power distribution from input mode (0,), port $(ref_input_port):")
    println("  total power over all output ports = $(round(total_power, digits=8))")
    println("  signed power sum                  = $(round(signed_total_power, digits=8))")
    println("  saved power stats                 = /home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_5axsjn8x/cache/twpa_cf7967b1ae821798_multimode_power.csv")
    println("  saved targeted conversions        = /home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_5axsjn8x/cache/twpa_cf7967b1ae821798_multimode_conversions.csv")

    # ------------------------------------------------------------
    # Symplectic / Bogoliubov diagnostics.
    # ------------------------------------------------------------
    sigma_diag = Float64[]

    for mode in modes_detected
        sigma = mode[1] >= 0 ? 1.0 : -1.0
        for _ in 1:num_ports
            push!(sigma_diag, sigma)
        end
    end

    Σ = diagm(sigma_diag)

    symplectic_errs = zeros(Float64, num_freqs)
    unitary_errs = zeros(Float64, num_freqs)
    bogoliubov_sum = zeros(Float64, num_freqs)

    for k in 1:num_freqs
        S_k = S_full[:, :, k]

        symplectic_errs[k] = maximum(abs.(S_k * Σ * S_k' - Σ))
        unitary_errs[k] = maximum(abs.(S_k' * S_k - I(full_size)))

        val = 0.0
        for (mi, mode) in enumerate(modes_detected)
            sigma = mode[1] >= 0 ? 1.0 : -1.0

            for port_idx in 1:num_ports
                row = (mi - 1) * num_ports + port_idx
                val += sigma * abs(S_k[row, ref_col])^2
            end
        end

        bogoliubov_sum[k] = val
    end

    max_symplectic_err = maximum(symplectic_errs)
    max_unitary_err = maximum(unitary_errs)
    max_bogoliubov_dev = maximum(abs.(bogoliubov_sum .- 1.0))

    diagnostics_out = zeros(Float64, num_freqs, 4)
    diagnostics_out[:, 1] = ws ./ (2*pi*1e9)
    diagnostics_out[:, 2] = symplectic_errs
    diagnostics_out[:, 3] = unitary_errs
    diagnostics_out[:, 4] = bogoliubov_sum

    writedlm(
        "/home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_5axsjn8x/cache/twpa_cf7967b1ae821798_multimode_diagnostics.csv",
        vcat(
            reshape([
                "frequency_GHz",
                "max_abs_SSigmaSdagger_minus_Sigma",
                "max_abs_SdaggerS_minus_I",
                "bogoliubov_sum"
            ], 1, 4),
            diagnostics_out
        ),
        ','
    )

    println()
    println("=== Multimode Bogoliubov / symplectic verification ===")
    println("Max |S Σ S† - Σ| = $(round(max_symplectic_err, digits=10))")
    println("Max |S†S - I|    = $(round(max_unitary_err, digits=6))")
    println("Max |Bogoliubov sum - 1| = $(round(max_bogoliubov_dev, digits=10))")

    if max_symplectic_err <= 1e-06
        println("Symplectic condition satisfied: true")
    else
        println("Symplectic condition satisfied: false")
        println("  tolerance = 1e-06")
    end

    println("Saved multimode full CSV: /home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_5axsjn8x/cache/twpa_cf7967b1ae821798_multimode_full.csv")
    println("Saved multimode metadata: /home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_5axsjn8x/cache/twpa_cf7967b1ae821798_multimode_modes.json")
    println("Saved multimode diagnostics: /home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_5axsjn8x/cache/twpa_cf7967b1ae821798_multimode_diagnostics.csv")
end

save_multimode_hbsolve_result()
