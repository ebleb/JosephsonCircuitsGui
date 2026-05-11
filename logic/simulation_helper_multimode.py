"""
simulation_helper_multimode.py

Helper functions for generating multimode HBSolve Julia output.

Use this from the existing simulation generator when the flattened HB top-block JSON
contains:

    "multimode": true

This helper appends Julia code that:
  - auto-detects available HBSolve modes
  - builds the full multimode S-matrix
  - saves:
      1. a full multimode CSV
      2. a mode metadata JSON
      3. power statistics CSV, summed over physical ports
      4. targeted input->output conversion CSV using hb_input_field / hb_output_field
      5. symplectic / unitary / Bogoliubov diagnostics CSV
  - prints whether the symplectic condition is satisfied

Important:
  - This helper does not generate Julia plots.
  - Plotting should happen in the Python plotter stage.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def json_bool(data: dict[str, Any], key: str, default: bool = False) -> bool:
    value = data.get(key, default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes", "y", "on"}


def is_multimode_enabled(data: dict[str, Any]) -> bool:
    return json_bool(data, "multimode", default=False)


def multimode_cache_paths(out_csv_path: Path) -> dict[str, Path]:
    """
    Derive additional output paths from the normal HBSolve CSV path.

    Example:
        twpa_abcd.csv
        twpa_abcd_multimode_full.csv
        twpa_abcd_multimode_modes.json
        twpa_abcd_multimode_power.csv
        twpa_abcd_multimode_conversions.csv
        twpa_abcd_multimode_diagnostics.csv
    """
    base = out_csv_path.with_suffix("")
    return {
        "full_csv": Path(f"{base}_multimode_full.csv"),
        "modes_json": Path(f"{base}_multimode_modes.json"),
        "power_csv": Path(f"{base}_multimode_power.csv"),
        "conversion_csv": Path(f"{base}_multimode_conversions.csv"),
        "diagnostics_csv": Path(f"{base}_multimode_diagnostics.csv"),
    }


def _jl_path(path: Path) -> str:
    return path.as_posix()


def _julia_int_or_nothing(value: Any) -> str:
    if value is None:
        return "nothing"
    return str(int(value))


def generate_multimode_julia_block(
    *,
    data: dict[str, Any],
    out_csv_path: Path,
    ports_jl: str,
) -> str:
    """
    Return Julia code to append after `rpm = hbsolve(...)`.

    Required Julia variables already defined by the main generator:
        ws, wp, rpm

    Required argument:
        ports_jl: Julia tuple of physical JosephsonCircuits ports, e.g. "(3, 4)"

    JSON fields used:
        multimode: bool

    Optional JSON fields:
        multimode_mode_min: int
        multimode_mode_max: int
        multimode_symplectic_tolerance: float

        multimode_reference_input_mode: int
            Default: 0

        multimode_reference_input_port: int
            Overrides validator-derived hb_input_field.

        multimode_reference_output_port: int
            Overrides validator-derived hb_output_field.

        hb_input_field: int
            Validator-derived JosephsonCircuits P-number for the simulation input pin.

        hb_output_field: int
            Validator-derived JosephsonCircuits P-number for the simulation output pin.

        hb_input_pin_name: str
        hb_output_pin_name: str

        multimode_conversion_output_modes: list[int]
            Which output modes to save in the targeted conversion CSV.
            Default: all detected modes.

    Notes:
        - ref_input_port is used as the source column.
        - ref_output_port is used as the target output row for conversion plots.
        - If either port is absent, Julia falls back to ports[1] for input and
          ports[end] for output.
    """
    paths = multimode_cache_paths(out_csv_path)

    mod_harmonics = int(data.get("hb_modulation_harmonics", [10])[0])
    default_span = 2 * mod_harmonics

    mode_min = int(data.get("multimode_mode_min", -default_span))
    mode_max = int(data.get("multimode_mode_max", default_span))
    symplectic_tol = float(data.get("multimode_symplectic_tolerance", 1e-6))

    ref_input_mode = int(data.get("multimode_reference_input_mode", 0))

    # Priority for input:
    #   1. explicit multimode_reference_input_port
    #   2. validator-derived hb_input_field
    #   3. first actual HBSolve P-port, in Julia at runtime
    ref_input_port_json = data.get(
        "multimode_reference_input_port",
        data.get("hb_input_field", None),
    )

    # Priority for output:
    #   1. explicit multimode_reference_output_port
    #   2. explicit multimode_output_port, for compatibility with earlier naming
    #   3. validator-derived hb_output_field
    #   4. last actual HBSolve P-port, in Julia at runtime
    ref_output_port_json = data.get(
        "multimode_reference_output_port",
        data.get("multimode_output_port", data.get("hb_output_field", None)),
    )

    requested_ref_input_port_jl = _julia_int_or_nothing(ref_input_port_json)
    requested_ref_output_port_jl = _julia_int_or_nothing(ref_output_port_json)

    hb_input_pin_name = data.get("hb_input_pin_name", "")
    hb_output_pin_name = data.get("hb_output_pin_name", "")

    # Optional list of output modes to export in targeted conversion CSV.
    conversion_modes = data.get("multimode_conversion_output_modes", None)
    if conversion_modes is None:
        conversion_modes_jl = "nothing"
    else:
        conversion_modes_jl = "[" + ", ".join(str(int(m)) for m in conversion_modes) + "]"

    return f"""
# ============================================================
# Multimode HBSolve post-processing
# ============================================================

function save_multimode_hbsolve_result()
    ports = {ports_jl}
    num_ports = length(ports)
    num_freqs = length(ws)

    println("Running multimode HBSolve post-processing...")

    # Auto-detect available modes.
    modes_detected = Tuple{{Int}}[]

    for n in {mode_min}:{mode_max}
        found = false

        for out_p in ports
            for in_p in ports
                try
                    rpm.linearized.S((n,), out_p, ({ref_input_mode},), in_p, 1)
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

    ref_mode_idx = findfirst(m -> m == ({ref_input_mode},), modes_detected)
    if ref_mode_idx === nothing
        error("Reference input mode ({ref_input_mode},) was not detected. Detected modes: $(modes_detected)")
    end

    requested_ref_input_port = {requested_ref_input_port_jl}
    ref_input_port = requested_ref_input_port === nothing ? ports[1] : requested_ref_input_port

    ref_input_port_idx = findfirst(p -> p == ref_input_port, ports)
    if ref_input_port_idx === nothing
        error("Reference input port $(ref_input_port) not found in HBSolve ports $(ports). Check hb_input_field or multimode_reference_input_port.")
    end

    requested_ref_output_port = {requested_ref_output_port_jl}
    ref_output_port = requested_ref_output_port === nothing ? ports[end] : requested_ref_output_port

    ref_output_port_idx = findfirst(p -> p == ref_output_port, ports)
    if ref_output_port_idx === nothing
        error("Reference output port $(ref_output_port) not found in HBSolve ports $(ports). Check hb_output_field or multimode_reference_output_port.")
    end

    num_modes = length(modes_detected)
    full_size = num_modes * num_ports

    println("Detected $(num_modes) modes: $(modes_detected)")
    println("Full multimode S-matrix size: $(full_size)×$(full_size)×$(num_freqs)")
    println("Reference input:  mode ({ref_input_mode},), port $(ref_input_port)")
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
    open("{_jl_path(paths["modes_json"])}", "w") do io
        println(io, "{{")
        println(io, "  \\"ports\\": ", collect(ports), ",")
        println(io, "  \\"modes\\": ", [m[1] for m in modes_detected], ",")
        println(io, "  \\"indexing\\": \\"row_or_col = (mode_index - 1) * num_ports + port_index; port_index indexes the listed JosephsonCircuits ports\\",")
        println(io, "  \\"reference_input_mode\\": {ref_input_mode},")
        println(io, "  \\"reference_input_port\\": ", ref_input_port, ",")
        println(io, "  \\"reference_output_port\\": ", ref_output_port, ",")
        println(io, "  \\"hb_input_pin_name\\": \\"{hb_input_pin_name}\\",")
        println(io, "  \\"hb_output_pin_name\\": \\"{hb_output_pin_name}\\"")
        println(io, "}}")
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

    writedlm("{_jl_path(paths["full_csv"])}", full_out, ',')

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
        "{_jl_path(paths["power_csv"])}",
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
    requested_conversion_modes = {conversion_modes_jl}

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
        "{_jl_path(paths["conversion_csv"])}",
        vcat(reshape(conversion_header, 1, length(conversion_header)), conversion_out),
        ','
    )

    println("At peak signal gain: f = $(round(f_peak_GHz, digits=6)) GHz")
    println("Power distribution from input mode ({ref_input_mode},), port $(ref_input_port):")
    println("  total power over all output ports = $(round(total_power, digits=8))")
    println("  signed power sum                  = $(round(signed_total_power, digits=8))")
    println("  saved power stats                 = {_jl_path(paths["power_csv"])}")
    println("  saved targeted conversions        = {_jl_path(paths["conversion_csv"])}")

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
        "{_jl_path(paths["diagnostics_csv"])}",
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

    if max_symplectic_err <= {symplectic_tol}
        println("Symplectic condition satisfied: true")
    else
        println("Symplectic condition satisfied: false")
        println("  tolerance = {symplectic_tol}")
    end

    println("Saved multimode full CSV: {_jl_path(paths["full_csv"])}")
    println("Saved multimode metadata: {_jl_path(paths["modes_json"])}")
    println("Saved multimode diagnostics: {_jl_path(paths["diagnostics_csv"])}")
end

save_multimode_hbsolve_result()
"""


def append_multimode_block_if_enabled(
    *,
    data: dict[str, Any],
    jl_code: str,
    out_csv_path: Path,
    ports_jl: str,
) -> str:
    """
    Convenience wrapper for the existing generator.

    Example inside generate_hbsolve_script after building jl_code:

        from simulation_helper_multimode import append_multimode_block_if_enabled

        jl_code = append_multimode_block_if_enabled(
            data=data,
            jl_code=jl_code,
            out_csv_path=out_csv_path,
            ports_jl=ports_jl,
        )
    """
    if not is_multimode_enabled(data):
        return jl_code

    return jl_code + generate_multimode_julia_block(
        data=data,
        out_csv_path=out_csv_path,
        ports_jl=ports_jl,
    )
