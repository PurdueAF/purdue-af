-- convert Slurm's ISO 8601-ish date to a timestamp
function date_to_timestamp(iso8601_date)
    local pattern = "(%d+)%-(%d+)%-(%d+)%a(%d+)%:(%d+)%:([%d%.]+)"
    local year, month, day, hour, minute, seconds = iso8601_date:match(pattern)
    return os.time({year = year, month = month, day = day, hour = hour, min = minute, sec = seconds})
end

-- Check for the ALL_NODES flag -
-- earlier versions of lua don't have bit ops
-- so mask all higher bits with modulo, and see if the resulting number
-- is higher or equal to the number corresponding to our bit flag.
--
function is_all_nodes(flags)
    local ALL_NODES=0x80000
    local NEXT_BIT=0x100000

    return ((flags % NEXT_BIT) >= ALL_NODES)
end

function get_next_maint_res()
    local res
    for v, _ in pairs (slurm.reservations) do
        r = slurm.reservations[v]
        maint = ((r.flags % 2) == 1)
        if maint and is_all_nodes(r.flags) and not res then
            res = r.start_time
        end
        if maint and is_all_nodes(r.flags) and (r.start_time < res) then
            res = r.start_time
        end
    end
    return res
end

-- Get the default partition name
function get_def_part(p_list)
    local def_part = nil
    for n, p in pairs(p_list) do
        if p.flag_default == 1 then
            def_part = p.name
        end
    end
    return def_part
end

function isempty(v)
    return v == nil or v == ''
end

function no_gpu(v)
    return (isempty(v) or (not string.find(v, "gpu:")) or (string.find(v, "gpu:0")))
end

function file_exists (name)
    local f = io.open (name, "r")
    if f ~= nil then
        io.close (f)
        return true
    else
        return false
    end
end

function list_params (desc, verbose)
    if file_exists ("/etc/slurm/dump_parameters.lua") then
        dofile ("/etc/slurm/dump_parameters.lua")
        log_resources (desc, verbose)
    end
end

--
-- we will exempt CMS from the 'no --mem=0' rule
-- making this a function simplifies the code later
--
function is_cms(job_desc)
    return (job_desc.account == 'cms' or job_desc.account == 'cms-a')
end

function is_cpu_owner_queue(job_desc)
    return (job_desc.partition == 'cpu' and
    (job_desc.qos ~= 'standby' and job_desc.qos ~= 'preemptible'))
end

--
-- main job_submit function
--
function slurm_job_submit(job_desc, part_list, submit_uid)

-- Give user resource request for debugging
    if (job_desc.comment == ".debug") then
        list_params (job_desc, false)
    elseif (job_desc.comment == ".debugall") then
        list_params (job_desc, true)
    end
--
    if (isempty(job_desc.partition)) then
        slurm.log_user("Gautschi requires you to explicitly list the partition you are submitting to.")
        slurm.log_user("For more information, see our user guide page: https://docs.rcac.purdue.edu/userguides/gautschi/run_jobs/queues/.")
        slurm.log_info("slurm_job_submit: Job rejected for not specifying partition.")
        return slurm.ERROR
    else
        p_req = job_desc.partition
    end

    slurm.log_info("slurm_job_submit: job from uid %u with requested walltime %d and partition %s", submit_uid, job_desc.time_limit, p_req)
    slurm.log_info("slurm_job_submit: requested nodes: %u, requested CPUs per node: %u, min_cpus: %u", job_desc.min_nodes, job_desc.pn_min_cpus, job_desc.min_cpus)

    if (not isempty(job_desc.tres_per_task)) then
        slurm.log_info("slurm_job_submit: gpus_per_task: %s nodes: %u", job_desc.tres_per_task, job_desc.min_nodes)
    end

-- don't allow --mem=0
-- except for CMS
    if (job_desc.pn_min_memory == 0) and not (is_cms(job_desc)) then
        slurm.log_user("The '--mem=0' option is disabled on Gautschi. Please specify an explicit memory size or use '--exclusive'.")
        return slurm.ESLURM_ACCOUNTING_POLICY
    end


-- Intercept jobs with '-G x' or '--gpus-per-job=x' because they hang mpirun
    if (not isempty(job_desc.tres_per_job)) then
        if (string.find(job_desc.tres_per_job, "gpu:")) then
            slurm.log_user("Gautschi does not currently support GPU-per-job resource requests")
            slurm.log_user("due to an MPI incompatibility.  Please resubmit using")
            slurm.log_user("'--gres=gpu:<count>' syntax (GPU-per-node)")
            slurm.log_info("slurm_job_submit: Job rejected for requesting GPU-per-job")
            return slurm.ERROR
        end
    end 


-- Don't allow jobs in the GPU queue if they aren't requesting GPUs
    if ((job_desc.partition == 'ai') or (job_desc.partition == 'smallgpu'))
    and (no_gpu(job_desc.tres_per_job))
    and (no_gpu(job_desc.tres_per_node))
    and (no_gpu(job_desc.tres_per_socket))
    and (no_gpu(job_desc.tres_per_task)) then
        slurm.log_info("No GPUs requested in a queue containing GPUs. Rejecting job.")
	slurm.log_user("Job rejected: The partitions containing GPUs are reserved for jobs requesting GPUs.")
	return slurm.ESLURM_JOB_MISSING_SIZE_SPECIFICATION
    end

-- Transform request for CPUs to high priority CPUs if in owner queue without standby qos
    if (is_cpu_owner_queue(job_desc)) then
        job_desc.tres_per_job = ("gres/hp_cpu=" .. (math.floor(job_desc.min_cpus + 0.5)) )
    end

-- Explain min submission requirements for relevant partitions
    if (job_desc.partition == 'highmem') and (job_desc.min_cpus < 96) then
        slurm.log_user("The highmem partition is reserved for jobs requiring more memory than would fit into a standard compute node.")
        slurm.log_user("Because memory is allocated proportional to the number of CPUs, you must request at least 96 cores in this partition.")
        slurm.log_info("slurm_job_submit: Job rejected for not meeting MinTRES requirements in highmem.")
        return slurm.ERROR
    end

-- Explain min submission requirements for relevant partitions
    if (job_desc.partition == 'smallgpu') and (job_desc.min_cpus < 64) then
        slurm.log_user("Nodes within the smallgpu partition have 128 cores and 2 GPUs.")
        slurm.log_user("Because memory is allocated proportional to the number of CPUs requested, you should request 64 CPUs per GPU you request")
        slurm.log_info("slurm_job_submit: Job rejected for not meeting MinTRES requirements in smallgpu.")
        return slurm.ERROR
    end

-- Explain CPU to GPU ratio on AI partition
    if (job_desc.partition == 'ai') then
        local nodemempercpu = 9200
        local nodemempergpu = 128800
        local gres = job_desc.tres_per_node or ""
        -- if job_desc.min_nodes is not set, set to 1.  If it is, set minnodes to job_desc.min_nodes
        local minnodes = job_desc.min_nodes < slurm.NO_VAL and job_desc.min_nodes or 1
        -- Matches gpu:h100:1 and gpu:1.
        local gpus = tonumber(string.match(job_desc.tres_per_node, "gpu:[^:]*:?(%d+)"))
        local reqdcpus = (gpus * 14) * minnodes
        if (job_desc.partition == 'ai') and (job_desc.min_cpus ~= reqdcpus) then
            slurm.log_user("Nodes in the AI partition have 14 CPUs per GPU..")
            slurm.log_user("To ensure nodes are shared fairly and evenly, please request 14 CPUs per GPU you request..")
            slurm.log_user("If you do not specify an explicit number of cores, the default allocation will be 14 CPUs per GPU..")
            slurm.log_user("Job rejected for not conforming to proportional GPU/CPU allocation..")
            slurm.log_info("slurm_job_submit: Job rejected for not requesting a multiple of 14 CPUs in the AI partition..")
            return slurm.ERROR
        end
        -- If we've requested --mem-per-cpu, we just need to make sure that too much has not been requested.
        if (job_desc.min_mem_per_cpu) and (job_desc.min_mem_per_cpu > nodemempercpu) then
            local requestedmem = job_desc.min_cpus * job_desc.min_mem_per_cpu
            local reqdgpus = math.ceil(requestedmem / nodemempergpu)
            slurm.log_user("To maintain porportional GPU/CPU/MEM allocations, please request ".. math.tointeger(reqdgpus) .. " GPUs in total instead of utilizing --mem arguments.")
            slurm.log_user("In doing so, you will automatically be allocated ".. math.tointeger(nodemempercpu) .. "MB of memory per CPU.")
            slurm.log_info("slurm_job_submit: Job rejected for not requesting a multiple of 9200MB of memory in the AI partition..")
            return slurm.ERROR
        end
        -- If we've requested memory with --mem, it should line up with numgpus(14*nodemempercpu), or
        -- the number of cpus for each gpu requested * MaxMemPerCPU.
        local reqdmem = reqdcpus * nodemempercpu
        local reqdgpus = math.ceil(job_desc.pn_min_memory / nodemempergpu)
        if (job_desc.partition == 'ai') and ((job_desc.pn_min_memory ~= reqdmem) and (job_desc.pn_min_memory ~= slurm.NO_VAL64) and (job_desc.min_mem_per_cpu == nil))  then
            slurm.log_user("To maintain porportional GPU/CPU/MEM allocations, please request ".. math.tointeger(reqdgpus) .. " GPUs in total instead of utilizing --mem arguments.")
            slurm.log_user("In doing so, you will automatically be allocated ".. math.tointeger(nodemempercpu) .. "MB of memory per CPU.")
            slurm.log_info("slurm_job_submit: Job rejected for not requesting a multiple of 9200MB of memory in the AI partition..")
            return slurm.ERROR
        end
        -- If we've requested memory with --mem-per-gpu, it should line up with numgpus(14*nodemempercpu)
        if (job_desc.mem_per_tres) then
            local mempertres = tonumber(string.match(job_desc.mem_per_tres, "gres/gpu:(%d+)"))
            if (mempertres > nodemempergpu) then
                local reqdgpus = math.ceil(mempertres / nodemempergpu)
                slurm.log_user("To maintain porportional GPU/CPU/MEM allocations, please request ".. math.tointeger(reqdgpus) .. " GPUs in total instead of utilizing --mem arguments.")
                slurm.log_user("In doing so, you will automatically be allocated ".. math.tointeger(nodemempercpu) .. "MB of memory per CPU.")
                slurm.log_info("slurm_job_submit: Job rejected for not requesting a multiple of 9200MB of memory in the AI partition..")
                return slurm.ERROR
            end
        end
    end

-- Explain CPU to GPU ratio on smallgpu partition
    if (job_desc.partition == 'smallgpu') then
        local nodemempercpu = 3000
        local nodemempergpu = 192000
        local gres = job_desc.tres_per_node or ""
        -- if job_desc.min_nodes is not set, set to 1.  If it is, set minnodes to job_desc.min_nodes
        local minnodes = job_desc.min_nodes < slurm.NO_VAL and job_desc.min_nodes or 1
        -- Matches gpu:l40:1 and gpu:1.
        local gpus = tonumber(string.match(job_desc.tres_per_node, "gpu:[^:]*:?(%d+)"))
        local reqdcpus = (gpus * 64) * minnodes
        if (job_desc.partition == 'smallgpu') and (job_desc.min_cpus ~= reqdcpus) then
            slurm.log_user("Nodes in the smallgpu partition have 14 CPUs per GPU..")
            slurm.log_user("To ensure nodes are shared fairly and evenly, please request 64 CPUs per GPU you request..")
            slurm.log_user("If you do not specify an explicit number of cores, the default allocation will be 64 CPUs per GPU..")
            slurm.log_user("Job rejected for not conforming to proportional GPU/CPU allocation..")
            slurm.log_info("slurm_job_submit: Job rejected for not requesting a multiple of 64 CPUs in the smallgpu partition..")
            return slurm.ERROR
        end
        -- If we've requested --mem-per-cpu, we just need to make sure that too much has not been requested.
        if (job_desc.min_mem_per_cpu) and (job_desc.min_mem_per_cpu > nodemempercpu) then
            local requestedmem = job_desc.min_cpus * job_desc.min_mem_per_cpu
            local reqdgpus = math.ceil(requestedmem / nodemempergpu)
            slurm.log_user("To maintain porportional GPU/CPU/MEM allocations, please request ".. math.tointeger(reqdgpus) .. " GPUs in total instead of utilizing --mem arguments.")
            slurm.log_user("In doing so, you will automatically be allocated ".. math.tointeger(nodemempercpu) .. "MB of memory per CPU.")
            slurm.log_info("slurm_job_submit: Job rejected for not requesting a multiple of 3000MB of memory in the smallgpu partition..")
            return slurm.ERROR
        end
        -- If we've requested memory with --mem, it should line up with numgpus(14*nodemempercpu), or
        -- the number of cpus for each gpu requested * MaxMemPerCPU.
        local reqdmem = reqdcpus * nodemempercpu
        local reqdgpus = math.ceil(job_desc.pn_min_memory / nodemempergpu)
        if (job_desc.partition == 'ai') and ((job_desc.pn_min_memory ~= reqdmem) and (job_desc.pn_min_memory ~= slurm.NO_VAL64) and (job_desc.min_mem_per_cpu == nil))  then
            slurm.log_user("To maintain porportional GPU/CPU/MEM allocations, please request ".. math.tointeger(reqdgpus) .. " GPUs in total instead of utilizing --mem arguments.")
            slurm.log_user("In doing so, you will automatically be allocated ".. math.tointeger(nodemempercpu) .. "MB of memory per CPU.")
            slurm.log_info("slurm_job_submit: Job rejected for not requesting a multiple of 3000MB of memory in the smallgpu partition..")
            return slurm.ERROR
        end
        -- If we've requested memory with --mem-per-gpu, it should line up with numgpus(14*nodemempercpu)
        if (job_desc.mem_per_tres) then
            local mempertres = tonumber(string.match(job_desc.mem_per_tres, "gres/gpu:(%d+)"))
            if (mempertres > nodemempergpu) then
                local reqdgpus = math.ceil(mempertres / nodemempergpu)
                slurm.log_user("To maintain porportional GPU/CPU/MEM allocations, please request ".. math.tointeger(reqdgpus) .. " GPUs in total instead of utilizing --mem arguments.")
                slurm.log_user("In doing so, you will automatically be allocated ".. math.tointeger(nodemempercpu) .. "MB of memory per CPU.")
                slurm.log_info("slurm_job_submit: Job rejected for not requesting a multiple of 3000MB of memory in the smallgpu partition..")
                return slurm.ERROR
            end
        end
    end

--
-- Note: partition assignments need to come before the time_limit assignment, since
-- we look for the assigned partition's default time limit.
--
-- NO_VAL from slurm.h, used to mark no time_limit specified on sbatch command line
-- Default account is set in Halcyon, should be "standby"
--
    local NO_VAL = 0xfffffffe

-- You must specify an account on Gautschi.
--
    if (isempty(job_desc.account)) then
        slurm.log_user("Gautschi requires you to explicitly list the account you are submitting to.")
        slurm.log_user("For more information, see our user guide page: https://docs.rcac.purdue.edu/userguides/gautschi/run_jobs/queues/.")
        slurm.log_info("slurm_job_submit: Job rejected for not specifying account.")
        return slurm.ERROR
    end

    if job_desc.time_limit == NO_VAL then
        local j_part = job_desc.partition
        local d_time = part_list[j_part].default_time
        job_desc.time_limit = d_time
--        slurm.log_user("partition and time limit set to %s %d", job_desc.partition, job_desc.time_limit)
    end

    -- get current time, time of next maintenance, and how long between now and then
    local curr_time = os.time()
    local next_maint_res = get_next_maint_res()

    -- only warn about maintenance if there's a maintenance reservation found
    if (not isempty(next_maint_res)) then

        local sec_until_next_maint = next_maint_res - curr_time
        -- if the number is negative, beginning of maintenance already happened
        if sec_until_next_maint < 0 then
            slurm.log_user("The cluster is currently undergoing maintenance.  Your job will be queued and will not run until the cluster is back online.")
            return slurm.SUCCESS
        end
        -- if time until next maintenance is less than time requested, print a message saying so.
        -- '//' to force values to integer.
        if sec_until_next_maint < job_desc.time_limit * 60 then
            slurm.log_user("Your job's requested walltime is longer than the time to the next maintenance period.")
            slurm.log_user("Therefore, it will not run until after the next maintenance period ends.")
            slurm.log_user("If you want your job to be able to run before the maintenance period, please specify")
            slurm.log_user("a walltime less than %d day(s), %d hour(s), and %d minute(s).", sec_until_next_maint // 86400, (sec_until_next_maint % 86400) // 3600, (sec_until_next_maint % 3600) // 60)
            return slurm.SUCCESS
        end
    end
    return slurm.SUCCESS
end


function slurm_job_modify(job_desc, job_rec, part_list, modify_uid)
    slurm.log_info("slurm_job_modify: for job %u from uid %u", job_rec.job_id, modify_uid)
    return slurm.SUCCESS
end

slurm.log_info("initialized")
return slurm.SUCCESS
