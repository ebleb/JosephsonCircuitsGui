using Base.Threads
using DelimitedFiles
using LinearAlgebra
using JosephsonCircuits

const SIMULATION_SCRIPTS = String[
    "/home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_bnpa1xom/cache/run_airbridge_signal2signal_crossing__p_2a0171e07a_2f384b1c4738c895.jl",
    "/home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_bnpa1xom/cache/run_ring_module__p_65c28c0c3b_c03c9042440c1f19.jl"
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
