using Base.Threads
using DelimitedFiles
using LinearAlgebra
using JosephsonCircuits

const SIMULATION_SCRIPTS = String[
    "/home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_es3gz2g0/cache/run_airbridge_signal2signal_crossing__p_2a0171e07a_4c4c18a397136daf.jl",
    "/home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_es3gz2g0/cache/run_ring_module__p_b6f6bf3b46_cb2fb17b5704aef2.jl",
    "/home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_es3gz2g0/cache/run_ring_module__p_95892ed906_7826f247e0786ac6.jl",
    "/home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_es3gz2g0/cache/run_ring_module__p_da3be03264_bfd40ccc2b7cedee.jl"
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
