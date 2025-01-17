from typing import List, Dict, Optional, Union
import os
import yaml
import time
from pathlib import Path
import shutil
import multiprocessing
from ..common._api_request import _compute_resource_get_api_request, _compute_resource_put_api_request
from .register_compute_resource import env_var_keys
from ..sdk.App import App
from ..sdk._run_job import _set_job_status
from .PubsubClient import PubsubClient
from ..common.dendro_types import DendroComputeResourceApp, DendroJob
from ..mock import using_mock


max_simultaneous_local_jobs = 2

class ComputeResourceException(Exception):
    pass

class Daemon:
    def __init__(self):
        self._compute_resource_id = os.getenv('COMPUTE_RESOURCE_ID', None)
        self._compute_resource_private_key = os.getenv('COMPUTE_RESOURCE_PRIVATE_KEY', None)
        self._node_id = os.getenv('NODE_ID', None)
        self._node_name = os.getenv('NODE_NAME', None)
        if self._compute_resource_id is None:
            raise ValueError('Compute resource has not been initialized in this directory, and the environment variable COMPUTE_RESOURCE_ID is not set.')
        if self._compute_resource_private_key is None:
            raise ValueError('Compute resource has not been initialized in this directory, and the environment variable COMPUTE_RESOURCE_PRIVATE_KEY is not set.')
        self._apps: List[App] = _load_apps(
            compute_resource_id=self._compute_resource_id,
            compute_resource_private_key=self._compute_resource_private_key,
            compute_resource_node_name=self._node_name,
            compute_resource_node_id=self._node_id
        )

        # important to keep track of which jobs we attempted to start
        # so that we don't attempt multiple times in the case where starting failed
        self._attempted_to_start_job_ids = set()

        print(f'Loaded apps: {", ".join([app._name for app in self._apps])}')

        from .SlurmJobHandler import SlurmJobHandler # we don't want a circular import
        self._slurm_job_handlers_by_processor: Dict[str, SlurmJobHandler] = {}
        for app in self._apps:
            for processor in app._processors:
                if app._slurm_opts is not None:
                    self._slurm_job_handlers_by_processor[processor._name] = SlurmJobHandler(self, app._slurm_opts)

        spec_apps = []
        for app in self._apps:
            spec_apps.append(app.get_spec())

        # Report the compute resource spec
        print('Reporting the compute resource spec')
        url_path = f'/api/compute_resource/compute_resources/{self._compute_resource_id}/spec'
        spec = {
            'apps': spec_apps
        }
        _compute_resource_put_api_request(
            url_path=url_path,
            compute_resource_id=self._compute_resource_id,
            compute_resource_private_key=self._compute_resource_private_key,
            data={
                'spec': spec
            }
        )
        print('Getting pubsub info')
        pubsub_subscription = get_pubsub_subscription(
            compute_resource_id=self._compute_resource_id,
            compute_resource_private_key=self._compute_resource_private_key,
            compute_resource_node_name=self._node_name,
            compute_resource_node_id=self._node_id
        )
        pubnub_subscribe_key = pubsub_subscription['pubnubSubscribeKey']
        if pubnub_subscribe_key != 'mock-subscribe-key':
            self._pubsub_client = PubsubClient(
                pubnub_subscribe_key=pubnub_subscribe_key,
                pubnub_channel=pubsub_subscription['pubnubChannel'],
                pubnub_user=pubsub_subscription['pubnubUser'],
                compute_resource_id=self._compute_resource_id
            )
        else:
            self._pubsub_client = None

    def start(self, *, timeout: Optional[float] = None, cleanup_old_jobs=True): # timeout is used for testing
        timer_handle_jobs = 0

        time_scale_factor = 1 if not using_mock() else 10000

        # Start cleaning up old job directories
        # It's important to do this in a separate process
        # because it can take a long time to delete all the files in the tmp directories (remfile is the culprit)
        # and we don't want to block the main process from handling jobs
        if cleanup_old_jobs:
            multiprocessing.Process(target=_cleanup_old_job_working_directories, args=(os.getcwd() + '/jobs',)).start()

        print('Starting compute resource')
        overall_timer = time.time()
        while True:
            elapsed_handle_jobs = time.time() - timer_handle_jobs
            need_to_handle_jobs = elapsed_handle_jobs > (60 * 10) / time_scale_factor # normally we will get pubsub messages for updates, but if we don't, we should check every 10 minutes
            messages = self._pubsub_client.take_messages() if self._pubsub_client is not None else []
            for msg in messages:
                if msg['type'] == 'newPendingJob':
                    need_to_handle_jobs = True
                if msg['type'] == 'jobStatusChaged':
                    need_to_handle_jobs = True
            if need_to_handle_jobs:
                timer_handle_jobs = time.time()
                self._handle_jobs()

            for slurm_job_handler in self._slurm_job_handlers_by_processor.values():
                slurm_job_handler.do_work()

            overall_elapsed = time.time() - overall_timer
            if timeout is not None and overall_elapsed > timeout:
                print(f'Compute resource timed out after {timeout} seconds')
                return
            if overall_elapsed < 5 / time_scale_factor:
                time.sleep(0.01 / time_scale_factor) # for the first few seconds we can sleep for a short time (useful for testing)
            else:
                time.sleep(2 / time_scale_factor)

    def _handle_jobs(self):
        url_path = f'/api/compute_resource/compute_resources/{self._compute_resource_id}/unfinished_jobs'
        if not self._compute_resource_id:
            return
        if not self._compute_resource_private_key:
            return
        resp = _compute_resource_get_api_request(
            url_path=url_path,
            compute_resource_id=self._compute_resource_id,
            compute_resource_private_key=self._compute_resource_private_key,
            compute_resource_node_name=self._node_name,
            compute_resource_node_id=self._node_id
        )
        jobs = resp['jobs']
        jobs = [DendroJob(**job) for job in jobs] # validation

        # Local jobs
        local_jobs = [job for job in jobs if self._is_local_job(job)]
        num_non_pending_local_jobs = len([job for job in local_jobs if job.status != 'pending'])
        if num_non_pending_local_jobs < max_simultaneous_local_jobs:
            pending_local_jobs = [job for job in local_jobs if job.status == 'pending']
            pending_local_jobs = _sort_jobs_by_timestamp_created(pending_local_jobs)
            num_to_start = min(max_simultaneous_local_jobs - num_non_pending_local_jobs, len(pending_local_jobs))
            local_jobs_to_start = pending_local_jobs[:num_to_start]
            for job in local_jobs_to_start:
                self._start_job(job)

        # AWS Batch jobs
        aws_batch_jobs = [job for job in jobs if self._is_aws_batch_job(job)]
        for job in aws_batch_jobs:
            self._start_job(job)

        # SLURM jobs
        slurm_jobs = [job for job in jobs if self._is_slurm_job(job) and self._job_is_pending(job)]
        for job in slurm_jobs:
            processor_name = job.processorName
            if processor_name not in self._slurm_job_handlers_by_processor:
                raise ComputeResourceException(f'Unexpected: Could not find slurm job handler for processor {processor_name}')
            self._slurm_job_handlers_by_processor[processor_name].add_job(job)

    def _get_job_resource_type(self, job: DendroJob) -> Union[str, None]:
        processor_name = job.processorName
        app: Union[App, None] = self._find_app_with_processor(processor_name)
        if app is None:
            return None
        if app._aws_batch_job_queue is not None:
            return 'aws_batch'
        if app._slurm_opts is not None:
            return 'slurm'
        return 'local'

    def _is_local_job(self, job: DendroJob) -> bool:
        return self._get_job_resource_type(job) == 'local'

    def _is_aws_batch_job(self, job: DendroJob) -> bool:
        return self._get_job_resource_type(job) == 'aws_batch'

    def _is_slurm_job(self, job: DendroJob) -> bool:
        return self._get_job_resource_type(job) == 'slurm'

    def _job_is_pending(self, job: DendroJob) -> bool:
        return job.status == 'pending'

    def _start_job(self, job: DendroJob, run_process: bool = True, return_shell_command: bool = False):
        job_id = job.jobId
        if job_id in self._attempted_to_start_job_ids:
            return '' # see above comment about why this is necessary
        self._attempted_to_start_job_ids.add(job_id)
        job_private_key = job.jobPrivateKey
        processor_name = job.processorName
        app = self._find_app_with_processor(processor_name)
        if app is None:
            msg = f'Could not find app with processor name {processor_name}'
            print(msg)
            _set_job_status(job_id=job_id, job_private_key=job_private_key, status='failed', error=msg)
            return ''
        try:
            print(f'Starting job {job_id} {processor_name}')
            from ._start_job import _start_job
            return _start_job(
                job_id=job_id,
                job_private_key=job_private_key,
                processor_name=processor_name,
                app=app,
                run_process=run_process,
                return_shell_command=return_shell_command
            )
        except Exception as e: # pylint: disable=broad-except
            # do a traceback
            import traceback
            traceback.print_exc()
            msg = f'Failed to start job: {str(e)}'
            print(msg)
            _set_job_status(job_id=job_id, job_private_key=job_private_key, status='failed', error=msg)
            return ''

    def _find_app_with_processor(self, processor_name: str) -> Union[App, None]:
        for app in self._apps:
            for p in app._processors:
                if p._name == processor_name:
                    return app
        return None

def _load_apps(*, compute_resource_id: str, compute_resource_private_key: str, compute_resource_node_name: Optional[str] = None, compute_resource_node_id: Optional[str] = None) -> List[App]:
    url_path = f'/api/compute_resource/compute_resources/{compute_resource_id}/apps'
    resp = _compute_resource_get_api_request(
        url_path=url_path,
        compute_resource_id=compute_resource_id,
        compute_resource_private_key=compute_resource_private_key,
        compute_resource_node_name=compute_resource_node_name,
        compute_resource_node_id=compute_resource_node_id
    )

    # It would be nice to do it this way, but we can't because we don't want to import stuff from the api here
    # from ..api_helpers.routers.compute_resource.router import GetAppsResponse
    # resp = GetAppsResponse(**resp)
    # compute_resource_apps = resp.apps

    # instead:
    compute_resource_apps = resp['apps']
    compute_resource_apps = [DendroComputeResourceApp(**app) for app in compute_resource_apps]

    return _load_apps_from_compute_resource_apps(compute_resource_apps)

def _load_apps_from_compute_resource_apps(compute_resource_apps: List[DendroComputeResourceApp]) -> List[App]:
    ret = []
    for a in compute_resource_apps:
        container = a.container
        aws_batch_opts = a.awsBatch
        slurm_opts = a.slurm
        s = []
        if container is not None:
            s.append(f'container: {container}')
        if aws_batch_opts is not None:
            if slurm_opts is not None:
                raise ComputeResourceException('App has awsBatch opts but also has slurm opts')
            aws_batch_job_queue = aws_batch_opts.jobQueue
            aws_batch_job_definition = aws_batch_opts.jobDefinition
            s.append(f'awsBatchJobQueue: {aws_batch_job_queue}')
            s.append(f'awsBatchJobDefinition: {aws_batch_job_definition}')
        else:
            aws_batch_job_queue = None
            aws_batch_job_definition = None
        if slurm_opts is not None:
            slurm_cpus_per_task = slurm_opts.cpusPerTask
            slurm_partition = slurm_opts.partition
            slurm_time = slurm_opts.time
            slurm_other_opts = slurm_opts.otherOpts
            s.append(f'slurmCpusPerTask: {slurm_cpus_per_task}')
            s.append(f'slurmPartition: {slurm_partition}')
            s.append(f'slurmTime: {slurm_time}')
            s.append(f'slurmOtherOpts: {slurm_other_opts}')
        else:
            slurm_cpus_per_task = None
            slurm_partition = None
            slurm_time = None
            slurm_other_opts = None
        print(f'Loading app {a.specUri} | {" | ".join(s)}')
        app = App.from_spec_uri(
            spec_uri=a.specUri,
            aws_batch_job_queue=aws_batch_job_queue,
            aws_batch_job_definition=aws_batch_job_definition,
            slurm_opts=slurm_opts
        )
        print(f'  {len(app._processors)} processors')
        ret.append(app)
    return ret

def start_compute_resource(dir: str, *, timeout: Optional[float] = None, cleanup_old_jobs=True): # timeout is used for testing
    config_fname = os.path.join(dir, '.dendro-compute-resource-node.yaml')
    if os.path.exists(config_fname):
        with open(config_fname, 'r', encoding='utf8') as f:
            the_config = yaml.safe_load(f)
    else:
        the_config = {}
    for k in env_var_keys:
        if k in the_config:
            os.environ[k] = the_config[k]
    daemon = Daemon()
    daemon.start(timeout=timeout, cleanup_old_jobs=cleanup_old_jobs)

def get_pubsub_subscription(*, compute_resource_id: str, compute_resource_private_key: str, compute_resource_node_name: Optional[str] = None, compute_resource_node_id: Optional[str] = None):
    url_path = f'/api/compute_resource/compute_resources/{compute_resource_id}/pubsub_subscription'
    resp = _compute_resource_get_api_request(
        url_path=url_path,
        compute_resource_id=compute_resource_id,
        compute_resource_private_key=compute_resource_private_key,
        compute_resource_node_name=compute_resource_node_name,
        compute_resource_node_id=compute_resource_node_id
    )
    return resp['subscription']

def _sort_jobs_by_timestamp_created(jobs: List[DendroJob]) -> List[DendroJob]:
    return sorted(jobs, key=lambda job: job.timestampCreated)

def _cleanup_old_job_working_directories(dir: str):
    """Delete working dirs that are more than 24 hours old"""
    jobs_dir = Path(dir)
    while True:
        if not jobs_dir.exists():
            continue
        for job_dir in jobs_dir.iterdir():
            if job_dir.is_dir():
                elapsed = time.time() - job_dir.stat().st_mtime
                if elapsed > 24 * 60 * 60:
                    print(f'Removing old working dir {job_dir}')
                    shutil.rmtree(job_dir)
        time.sleep(60)
