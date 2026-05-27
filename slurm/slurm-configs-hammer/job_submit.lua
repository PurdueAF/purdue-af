-- convert Slurm's ISO 8601-ish date to a timestamp
function date_to_timestamp(iso8601_date)
    local pattern = "(%d+)%-(%d+)%-(%d+)%a(%d+)%:(%d+)%:([%d%.]+)"
    local year, month, day, hour, minute, seconds = iso8601_date:match(pattern)
    return os.time({year = year, month = month, day = day, hour = hour, min = minute, sec = seconds})
end

-- get the timestamp of the next maintenance reservation
function get_next_maint_res()
    -- get the earliest maintenance reservation from scontrol, "handle" is a filehandle to stdout
    local handle = io.popen("/usr/bin/scontrol show res --oneliner | /usr/bin/grep -i maint | /usr/bin/sort -k 2 | \
        /usr/bin/head -1 | /usr/bin/awk '{print $2}' | /usr/bin/cut -d= -f2")
    -- read the contents of stdout
    local maint_datestr = handle:read("*a")
    handle:close()
    -- return the string we got, using gsub to strip the trailing newline
    return string.gsub(maint_datestr, "\n$", "")
end

function isempty(v)
    return v == nil or v == ''
end



--
-- main job_submit function
--
function slurm_job_submit(job_desc, part_list, submit_uid)
    slurm.log_info("slurm_job_submit: job from uid %ui with requested walltime %d", submit_uid, job_desc.time_limit)

    -- dump some partition information to see what fields are valid.
    local inx = 0
    for name, part in pairs(part_list) do
        slurm.log_info("part name[%d]: %s", inx, part.name)
        if isempty(part.max_cpus_per_node) then
            slurm.log_info("max_cpus_per_node: undefined")
        else
            slurm.log_info("max_cpus_per_node: %d", part.max_cpus_per_node)
        end

        if isempty(part.max_mem_per_cpu) then
            slurm.log_info("max_mem_per_cpu: undefined")
        else
            slurm.log_info("max_mem_per_cpu: %d", part.max_mem_per_cpu)
        end

        if isempty(part.def_mem_per_cpu) then
            slurm.log_info("def_mem_per_cpu: undefined")
        else
            slurm.log_info("def_mem_per_cpu: %d", part.def_mem_per_cpu)
        end

        if isempty(part.default_time) then
            slurm.log_info("default_time: undefined")
        else
            slurm.log_info("default_time: %d", part.default_time)
        end

    end

    -- get current time, time of next maintenance, and how long between now and then
    local curr_time = os.time()
    local next_maint_res = date_to_timestamp(get_next_maint_res())
    local sec_until_next_maint = next_maint_res - curr_time
    -- if the number is negative, beginning of maintenance already happened
    if sec_until_next_maint < 0 then
        slurm.log_user("The cluster is currently undergoing maintenance.  Your job will be queued and will not run until the cluster is back online.")
        return slurm.SUCCESS
    end
    -- if time until next maintenance is less than time requested, print a message saying so.
    if sec_until_next_maint < job_desc.time_limit * 60 then
        slurm.log_user("Your job's requested walltime is longer than the time to the next maintenance period.")
        slurm.log_user("Therefore, it will not run until after the next maintenance period ends.")
        slurm.log_user("If you want your job to be able to run before the maintenance period, please specify")
        slurm.log_user("a walltime less than %d day(s), %d hour(s), and %d minute(s).", 
            sec_until_next_maint / 86400, (sec_until_next_maint % 86400) / 3600, (sec_until_next_maint % 3600) / 60)
        return slurm.SUCCESS
    end
    return slurm.SUCCESS
end


function slurm_job_modify(job_desc, job_rec, part_list, modify_uid)
    slurm.log_info("slurm_job_modify: for job %u from uid %u", job_rec.job_id, modify_uid)
    return slurm.SUCCESS
end

slurm.log_info("initialized")
return slurm.SUCCESS
