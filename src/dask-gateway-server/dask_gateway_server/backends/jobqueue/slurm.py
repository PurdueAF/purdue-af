import math
import os
import shutil
import subprocess
import pwd
import json
from ldap3 import Server, Connection, SUBTREE

from traitlets import Unicode, default

from ...traitlets import Type
from .base import JobQueueBackend, JobQueueClusterConfig

__all__ = ("SlurmBackend", "SlurmClusterConfig")


def ldap_lookup(username):
    url = "geddes-aux.rcac.purdue.edu"
    baseDN = "ou=People,dc=rcac,dc=purdue,dc=edu"
    search_filter = "(uid={0}*)"
    attrs = ['uidNumber','gidNumber']
    s = Server(host=url, use_ssl=True, get_info='ALL')
    conn = Connection(s, version = 3, authentication = "ANONYMOUS")
    conn.start_tls()
    print(conn.result)
    print(conn)
    conn.search(search_base = baseDN, search_filter = search_filter.format(username), search_scope = SUBTREE, attributes = attrs)
    ldap_result_id = json.loads(conn.response_to_json())
    print(ldap_result_id)
    result = ldap_result_id[u'entries'][0][u'attributes']
    uid_number = result[u'uidNumber']
    gid_number = result [u'gidNumber']
    return uid_number, gid_number


def slurm_format_memory(n):
    """Format memory in bytes for use with slurm."""
    if n >= 10 * (1024**3):
        return "%dG" % math.ceil(n / (1024**3))
    if n >= 10 * (1024**2):
        return "%dM" % math.ceil(n / (1024**2))
    if n >= 10 * 1024:
        return "%dK" % math.ceil(n / 1024)
    return "1K"


class SlurmClusterConfig(JobQueueClusterConfig):
    """Dask cluster configuration options when running on SLURM"""

    scheduler_partition = Unicode("", help="Slurm partition to submit the scheduler.", config=True)

    scheduler_reservation = Unicode("", help="Slurm reservation to submit the scheduler.", config=True)

    worker_partition = Unicode("", help="Slurm partition to submit the workers.", config=True)

    qos = Unicode("", help="QOS string associated with each job.", config=True)

    account = Unicode("", help="Account string associated with each job.", config=True)

    reservation = Unicode("", help="Node reservation for job submission.", config=True)

    time = Unicode("", help="Max. time for scheduler and workers", config=True)

class SlurmBackend(JobQueueBackend):
    """A backend for deploying Dask on a Slurm cluster."""

    cluster_config_class = Type(
        "dask_gateway_server.backends.jobqueue.slurm.SlurmClusterConfig",
        klass="dask_gateway_server.backends.base.ClusterConfig",
        help="The cluster config class to use",
        config=True,
    )

    @default("submit_command")
    def _default_submit_command(self):
        return shutil.which("sbatch") or "sbatch"

    @default("cancel_command")
    def _default_cancel_command(self):
        return shutil.which("scancel") or "scancel"

    @default("status_command")
    def _default_status_command(self):
        return shutil.which("squeue") or "squeue"

    def create_user(self, username):
        uid, gid = ldap_lookup(username)

        command = [
            '/bin/sh', '-c',
            f"id -u {username} &>/dev/null || "
            +f" useradd {username} -u {uid} -d /depot/cms/users/{username} -M"
        ]  
        try:
            subprocess.run(command, check=True)
            print(f"User '{username}' with UID {uid} has been created successfully.")
        except subprocess.CalledProcessError as e:
            raise Exception(f"Error creating user: {e}")

    def get_submit_cmd_env_stdin(self, cluster, worker=None):
        self.create_user(cluster.username)
        cmd = [self.submit_command, "--parsable"]
        cmd.append("--job-name=dask-gateway")
        if cluster.config.account:
            cmd.append("--account=" + cluster.config.account)
        if cluster.config.qos:
            cmd.extend("--qos=" + cluster.config.qos)
        if cluster.config.reservation:
            cmd.extend(["--reservation=" + cluster.config.reservation])
        if cluster.config.time:
            cmd.extend(["--time=" + cluster.config.time])

        if worker:
            if cluster.config.worker_partition:
                cmd.append("--partition=" + cluster.config.worker_partition)
            cpus = cluster.config.worker_cores
            mem = slurm_format_memory(cluster.config.worker_memory)
            log_file = "dask-worker-%s.log" % worker.name
            script = "\n".join(
                [
                    "#!/bin/sh",
                    cluster.config.worker_setup,
                    " ".join(self.get_worker_command(cluster, worker.name)),
                ]
            )
            env = self.get_worker_env(cluster)
        else:
            if cluster.config.scheduler_partition:
                cmd.append("--partition=" + cluster.config.scheduler_partition)
            if cluster.config.scheduler_reservation:
                cmd.append("--reservation=" + cluster.config.scheduler_reservation)
            cpus = cluster.config.scheduler_cores
            mem = slurm_format_memory(cluster.config.scheduler_memory)
            log_file = "dask-scheduler-%s.log" % cluster.name
            script = "\n".join(
                [
                    "#!/bin/sh",
                    cluster.config.scheduler_setup,
                    " ".join(self.get_scheduler_command(cluster)),
                ]
            )
            env = self.get_scheduler_env(cluster)

        staging_dir = self.get_staging_directory(cluster)

        cmd.extend(
            [
                "--chdir=" + staging_dir,
                "--output=" + os.path.join(staging_dir, log_file),
                "--cpus-per-task=%d" % cpus,
                "--mem=%s" % mem,
                "--export=%s" % (",".join(sorted(env))),
            ]
        )

        return cmd, env, script

    def get_stop_cmd_env(self, job_id):
        return [self.cancel_command, job_id], {}

    def get_status_cmd_env(self, job_ids):
        cmd = [self.status_command, "-h", "--job=%s" % ",".join(job_ids), "-o", "%i %t"]
        return cmd, {}

    def parse_job_states(self, stdout):
        states = {}
        for l in stdout.splitlines():
            job_id, state = l.split()
            states[job_id] = state in ("R", "CG", "PD", "CF")
        return states

    def parse_job_id(self, stdout):
        return stdout.strip()
