using Base.Threads
using DelimitedFiles
using LinearAlgebra
using JosephsonCircuits

const SIMULATION_SCRIPTS = String[
    "/home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_uz__3i91/cache/run_ABCD_shuntY__p_56b6ac47db_e18aae578b1d06ee.jl",
    "/home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_uz__3i91/cache/run_ABCD_seriesZ__p_39ba025f57_db1d45e33d62e24b.jl",
    "/home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_uz__3i91/cache/run_ABCD_shunt_signal2signal__p_81d3729db0_03bbda9e404e53c8.jl",
    "/home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_uz__3i91/cache/run_ABCD_tline__p_f2cb0b60e9_866d91b00a396d66.jl",
    "/home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_uz__3i91/cache/run_ABCD_tline__p_7fa62ffb17_30453e00291600b7.jl",
    "/home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_uz__3i91/cache/run_ABCD_coupled_tline__p_475b1f7ed6_39ff2185c9fbdac3.jl",
    "/home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_uz__3i91/cache/run_ABCD_coupled_tline__p_974c9b45f2_d9bc5b2d9155eab4.jl"
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
