import os
import dask
from dask_jobqueue.slurm import SLURMJob, SLURMCluster

# Port settings
DEFAULT_SCHEDULER_PORT = 8786
DEFAULT_DASHBOARD_PORT = 8787
USER = os.environ["USER"]
NAMESPACE = os.environ["NAMESPACE"]

class PurdueSLURMJob(SLURMJob):
    config_name = "purdue-slurm"

class PurdueSLURMCluster(SLURMCluster):
    """
    This is a  subclass expanding settings for launch Dask via SLURMCluster
    in Purdue Analysis Facility.
    """
    job_cls = PurdueSLURMJob
    config_name = "purdue-slurm"
    kernel_name = ""
    kernel_display_name = ""

    def __init__(
        self,
        *,
        scheduler_port=DEFAULT_SCHEDULER_PORT,
        dashboard_port=DEFAULT_DASHBOARD_PORT,
        **job_kwargs
    ):

        job_kwargs = self._modify_job_kwargs(
            job_kwargs,
            scheduler_port=scheduler_port,
            dashboard_port=dashboard_port
        )
        self.kernel_name = job_kwargs.pop("kernel_name", "")
        self.kernel_display_name = job_kwargs.pop("kernel_display_name", "")

        super().__init__(**job_kwargs)

    @classmethod
    def _modify_job_kwargs(
        cls,
        job_kwargs,
        *,
        scheduler_port=DEFAULT_SCHEDULER_PORT,
        dashboard_port=DEFAULT_DASHBOARD_PORT
    ):

        job_config = job_kwargs.copy()

        contact_address = f"{USER}-dask-slurm-scheduler.{NAMESPACE}.geddes.rcac.purdue.edu:{scheduler_port}"
        job_config["scheduler_options"] = {
            "port": scheduler_port,
            "dashboard_address": f":{dashboard_port}",
            "contact_address": contact_address
        }

        return job_config