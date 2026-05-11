using Base.Threads
using DelimitedFiles
using LinearAlgebra
using JosephsonCircuits

const SIMULATION_SCRIPTS = String[
    "/home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_es3gz2g0/cache/run_twpa_72b05a9c56a8a2b2.jl",
    "/home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_es3gz2g0/cache/run_ABCD_coupled_tline__p_ebd67d6b56_1728d0267f3c90bc.jl",
    "/home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_es3gz2g0/cache/run_ABCD_coupled_tline__p_80fd5fb8af_7a78263dd9286396.jl",
    "/home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_es3gz2g0/cache/run_ABCD_seriesZ__p_39ba025f57_2bd28d2ae4a1038d.jl",
    "/home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_es3gz2g0/cache/run_ABCD_shuntY__p_56b6ac47db_97c996e5c5d9cc9a.jl",
    "/home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_es3gz2g0/cache/run_ABCD_shunt_signal2signal__p_81d3729db0_afe4557a671ce957.jl",
    "/home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_es3gz2g0/cache/run_ABCD_tline__p_f2cb0b60e9_105c7247900a800d.jl",
    "/home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_es3gz2g0/cache/run_ABCD_tline__p_7fa62ffb17_bafad1fc9059407e.jl",
    "/home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_es3gz2g0/cache/run_ABCD_tline__p_f519705b72_ee46ab82bb5d9ff4.jl",
    "/home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_es3gz2g0/cache/run_S_termination__p_822fb3419d_05a56df9ec75082f.jl",
    "/home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_es3gz2g0/cache/run_ABCD_coupled_tline__p_6819c7f6a2_352d1bd8d5210c3a.jl",
    "/home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_es3gz2g0/cache/run_ABCD_coupled_tline__p_8ab82b3e98_ec6002aabcc258b6.jl",
    "/home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_es3gz2g0/cache/run_ABCD_coupled_tline__p_475b1f7ed6_0e4a063a0ee80d60.jl",
    "/home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_es3gz2g0/cache/run_ABCD_tline__p_e0c8d8ba48_0a3d49031590a0f9.jl",
    "/home/benedikte/userdata/urop/app_v2_pub/logic/outputs/guiV2_pipeline_es3gz2g0/cache/run_ABCD_coupled_tline__p_974c9b45f2_d2141d1aa56d9b16.jl"
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
