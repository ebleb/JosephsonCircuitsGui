using Base.Threads
using DelimitedFiles
using LinearAlgebra
using JosephsonCircuits

const SIMULATION_SCRIPTS = String[
    "/home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_bnpa1xom/cache/run_ABCD_shuntY__p_56b6ac47db_1b7e530cb7159ff0.jl",
    "/home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_bnpa1xom/cache/run_ABCD_seriesZ__p_39ba025f57_4983edefa395a0b6.jl",
    "/home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_bnpa1xom/cache/run_ABCD_shunt_signal2signal__p_81d3729db0_8c830e406885c984.jl",
    "/home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_bnpa1xom/cache/run_ABCD_tline__p_f2cb0b60e9_260a6a55333e31dd.jl",
    "/home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_bnpa1xom/cache/run_ABCD_tline__p_7fa62ffb17_19093ba09a518e51.jl",
    "/home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_bnpa1xom/cache/run_ABCD_coupled_tline__p_475b1f7ed6_0723027ef7d60b23.jl",
    "/home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_bnpa1xom/cache/run_ABCD_coupled_tline__p_974c9b45f2_7b6d647af1f66982.jl",
    "/home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_bnpa1xom/cache/run_S_termination__p_822fb3419d_c5549d4847790fdb.jl"
]

function include_isolated(path::String, idx::Int)
    module_name = Symbol("SimulationBatch_", idx)
    mod = Module(module_name)
    Core.eval(mod, :(using DelimitedFiles))
    Core.eval(mod, :(using LinearAlgebra))
    Core.eval(mod, :(using JosephsonCircuits))
    println("[BATCH] starting $(basename(path))")
    Base.include(mod, path)
    println("[BATCH] finished $(basename(path))")
    return nothing
end

tasks = Task[]

for (idx, path) in enumerate(SIMULATION_SCRIPTS)
    push!(tasks, Threads.@spawn include_isolated(path, idx))
end

for task in tasks
    fetch(task)
end
