function log_resources (job_desc, verbose)
    local markers = {}
        markers [0xff] = "INFINITE8"
        markers [0xffff] = "INFINITE16"
        markers [0xffffffff] = "INFINITE"
        markers [0xffffffffffffffff] = "INFINITE64"
        markers [0xfe] = "NO_VAL8"
        markers [0xfffe] = "NO_VAL16"
        markers [0xfffffffe] = "NO_VAL"
        markers [0xfffffffffffffffe] = "NO_VAL64"
        markers [0xfffffffffffffffd] = "NO_CONSUME_VAL64"

    function translate (val)
        return (markers [val]) or tostring (val)
    end

    function log1 (res, label)
        local str
        if (label == nil) then 
            label = res 
        end

        if isempty (job_desc[res]) then 
            str = "NONE" 
        elseif type (job_desc[res]) == "table" then
            str = "" 
            for i,k in pairs (job_desc[res]) do 
                str = str .. " [" .. i .. "]" .. k
            end
        else 
            str = translate (job_desc[res]) 
        end

        if verbose or str ~= "NONE" then
            slurm.log_user (label..": %s", str)
        end
    end
    
    log1 ("account")
    log1 ("alloc_node")
    log1 ("argc", "number of script args")
    log1 ("argv", "script args") 
    log1 ("array_inx", "job array index values")
    log1 ("batch_features")
    log1 ("begin_time")
    log1 ("bitflags")
    log1 ("burst_buffer")
    log1 ("cluster_features")
    log1 ("comment")
    log1 ("contiguous", "job requires contiguous nodes")
    log1 ("core_spec", "specialized core/thread count")
    log1 ("cpu_bind", "CPU binding map")
    log1 ("cpu_bind_type")
    log1 ("cpu_freq_min")
    log1 ("cpu_freq_max")
    log1 ("cpu_freq_gov")
    log1 ("cpus_per_tres")
    log1 ("deadline")
    log1 ("delay_boot")
    log1 ("dependency")
    log1 ("end_time")
    log1 ("environment")
    log1 ("env_size")
    log1 ("exc_nodes", "excluded nodes")
    log1 ("features")
    log1 ("group_id")
    log1 ("immediate")
--    log1 ("job_id")
--    log1 ("job_id_str")
    log1 ("kill_on_node_fail")
    log1 ("licenses")
    log1 ("mail_type")
    log1 ("mail_user")
    log1 ("mcs_label")
    log1 ("mem_bind")
    log1 ("mem_bind_type")
    log1 ("mem_per_tres")
    log1 ("name", "job name")
    log1 ("network")
    log1 ("nice")
    log1 ("num_tasks")
    log1 ("open_mode")
    log1 ("origin_cluster")
    log1 ("overcommit")
    log1 ("partition")
    log1 ("plane_size")
    log1 ("power_flags")
    log1 ("priority")
    log1 ("profile", "acct_gather_profile level")
    log1 ("qos")
    log1 ("reboot")
    log1 ("resp_host")
    log1 ("restart_cnt")
    log1 ("req_nodes", "required nodes")
    log1 ("requeue")
    log1 ("reservation")
    log1 ("shared")
    log1 ("site_factor", "priority site factor")
    log1 ("spank_job_env")
    log1 ("task_dist")
    log1 ("time_limit")
    log1 ("time_min")
    log1 ("tres_bind")
    log1 ("tres_freq")
    log1 ("tres_per_job")
    log1 ("tres_per_node")
    log1 ("tres_per_socket")
    log1 ("tres_per_task")
    log1 ("user_id")
    log1 ("wait_all_nodes")
    log1 ("warn_flags")
    log1 ("warn_signal")
    log1 ("warn_time")
    log1 ("work_dir")
    log1 ("cpus_per_task")
    log1 ("min_cpus")
    log1 ("max_cpus")
    log1 ("min_mem_per_cpu")
    log1 ("min_mem_per_node")
    log1 ("min_nodes")
    log1 ("max_nodes")
    log1 ("boards_per_node")
    log1 ("sockets_per_node")
    log1 ("cores_per_socket")
    log1 ("ntasks_per_node")
    log1 ("ntasks_per_socket")
    log1 ("ntasks_per_core")
    log1 ("ntasks_per_board")
    log1 ("pn_min_cpus")
    log1 ("pn_min_memory")
    log1 ("pn_min_tmp_disk")
    log1 ("req_switch")
    log1 ("std_err")
    log1 ("std_in")
    log1 ("std_out")
    log1 ("tres_req_cnt")
    log1 ("wait4switch")
    log1 ("wckey")
    log1 ("x11")
    log1 ("x11_magic_cookie")
    log1 ("x11_target")
    log1 ("x11_target_port")

end
