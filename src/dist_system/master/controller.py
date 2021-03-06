import asyncio
import random
from typing import Iterable

from dist_system.information import AllocatedResource, AllocationInformation
from dist_system.library import SingletonMeta
from dist_system.logger import Logger
from dist_system.master.msg_dispatcher import SlaveMessageDispatcher
from dist_system.master.slave import SlaveManager, NotAvailableSlaveError, Slave
from dist_system.master.task import TaskManager, TaskStatus
from dist_system.master.virtualizer.config import RunConfig
from dist_system.master.virtualizer.linker import link
from dist_system.task import TaskType
from dist_system.task.data_processing_task import DataProcessingTaskMasterJob, DataProcessingTaskSlaveJob
from dist_system.task.functions import get_task_type_of_task
from dist_system.task.tensorflow_train_task import TensorflowTrainTaskMasterJob, TensorflowTrainTaskSlaveJob
from dist_system.task.tensorflow_test_task import TensorflowTestTaskMasterJob, TensorflowTestTaskSlaveJob
from dist_system.result_receiver_network import ResultReceiverCommunicator


async def run_heartbeat():
    while True:
        expired_slaves, leak_tasks = SlaveManager().purge()
        TaskManager().redo_leak_task(leak_tasks)

        for slave in expired_slaves:
            Logger().log("Expired Slave : {0}".format(str(slave)))

        for slave in SlaveManager().slaves:
            SlaveMessageDispatcher().dispatch_msg(slave, 'heart_beat_req', {})

        await asyncio.sleep(SlaveManager.HEARTBEAT_INTERVAL)


async def purge_impossible_tasks():

    PURGE_TASKS_INTERVAL = 3

    while True:
        await AsyncController.remove_impossible_tasks()
        await asyncio.sleep(PURGE_TASKS_INTERVAL)


def delete_task(task):
    TaskManager().del_task(task)
    slaves = SlaveManager().slaves
    for slave in slaves:
        if task in slave.failed_tasks:
            slave.unmark_failed_task(task)


def free_resource(alloc_info: AllocationInformation, allocated_resource: AllocatedResource):

    Logger().log("Free Resource :", allocated_resource)

    if alloc_info.alloc_cpu_count < allocated_resource.alloc_cpu_count:
        raise ValueError('Invalid allocated resource. (alloc_cpu_count)')

    alloc_info.alloc_cpu_count -= allocated_resource.alloc_cpu_count
    if allocated_resource.alloc_tf_gpu_info is not None:
        allocated_resource.alloc_tf_gpu_info.available = True



def check_system_busy():
    SYSTEM_CAPABILITY_CONSTANT = 2

    task_cnt = len(TaskManager().all_tasks)
    calc_unit_cnt = 0
    for slave in SlaveManager().slaves:
        calc_unit_cnt += 1
        if slave.slave_info is not None:
            calc_unit_cnt += len(slave.slave_info.tf_gpu_info_list)
    capability = calc_unit_cnt * SYSTEM_CAPABILITY_CONSTANT

    return capability <= calc_unit_cnt


class AsyncController(object):

    _doing_remove_impossible_tasks = False

    @staticmethod
    async def remove_impossible_tasks():
        if AsyncController._doing_remove_impossible_tasks:
            return
        AsyncController._doing_remove_impossible_tasks = True

        try:
            tasks = TaskManager().waiting_tasks
            slaves = SlaveManager().slaves

            for task in tasks:
                possible_slaves = [slave for slave in slaves if task not in slave.failed_tasks]
                if len(possible_slaves) == 0:
                    # task is impossible.
                    Logger().log("Removed impossible Task :", task)
                    TaskManager().del_task(task)
                    #TODO: blocking if resut receiver is strange.
                    header, body = await ResultReceiverCommunicator().communicate(
                        task.result_receiver_address,
                        'task_finish_req', {
                            'status': 'fail',
                            'task_token': task.task_token.to_bytes(),
                            'error_code': 'impossible'
                        })
                    # nothing to do using response message...
        finally:
            AsyncController._doing_remove_impossible_tasks = False


class Scheduler(metaclass=SingletonMeta):
    def invoke(self, invoke_log = True):
        # 현재 waiting하고 있는 task가 있는 지 보고 available한 slave 있는지 판단하여 task를 slave에 배치한다.
        if invoke_log is True:
            Logger().log('**** Scheduler is invoked.')
        self._assign_waiting_task_to_slave()

    def _assign_waiting_task_to_slave(self):
        slave_manager = SlaveManager()
        task_manager = TaskManager()

        for task in task_manager.waiting_tasks:
            try:
                slave, run_config, allocated_resource = self._schedule(slave_manager.slaves, task)
            except NotAvailableSlaveError:
                Logger().log('Not Available Slave Error!')
                continue

            Logger().log('**** GO Schedule! Task : {0}, Slave : {1}, run_config : {2}, '
                         'allocated_resource : {3}'.format(
                task, slave, run_config, allocated_resource
            ))

            task_manager.change_task_status(task, TaskStatus.STATUS_PREPROCESSING)
            self._preprocess_task(task, run_config)
            task_manager.change_task_status(task, TaskStatus.STATUS_PROCESSING)
            slave.assign_task(task)

            task.allocated_resource = allocated_resource

            SlaveMessageDispatcher().dispatch_msg(slave, 'task_register_req', {
                'result_receiver_address': task.result_receiver_address.to_dict(),
                'task_token': task.task_token.to_bytes(),
                'task_type': get_task_type_of_task(task).to_str(),
                'task': task.job.to_dict()
            })

    def _preprocess_task(self, task, run_config: RunConfig):
        task_type = get_task_type_of_task(task)

        if task_type == TaskType.TYPE_SLEEP_TASK:
            pass

        elif task_type == TaskType.TYPE_DATA_PROCESSING_TASK:
            if not isinstance(task.job, DataProcessingTaskMasterJob):
                task.job = task.prev_job
            executable_code = link(task.job.object_code, run_config)
            task.prev_job = task.job
            task.job = DataProcessingTaskSlaveJob.from_master_job(task.prev_job, executable_code)

        elif task_type == TaskType.TYPE_TENSORFLOW_TRAIN_TASK:
            if not isinstance(task.job, TensorflowTrainTaskMasterJob):
                task.job = task.prev_job
            executable_code = link(task.job.object_code, run_config)
            task.prev_job = task.job
            task.job = TensorflowTrainTaskSlaveJob(task.job.data_file_token, executable_code)

        elif task_type == TaskType.TYPE_TENSORFLOW_TEST_TASK:
            if not isinstance(task.job, TensorflowTestTaskMasterJob):
                task.job = task.prev_job
            executable_code = link(task.job.object_code, run_config)
            task.prev_job = task.job
            task.job = TensorflowTestTaskSlaveJob(task.job.data_file_token, executable_code, task.job.session_file_token)

        else:
            raise NotImplementedError

    def _schedule(self, slaves: Iterable, task) -> Slave:
        __schedule_dict = {
            TaskType.TYPE_SLEEP_TASK: self._schedule_sleep_task,
            TaskType.TYPE_DATA_PROCESSING_TASK: self._schedule_data_processing_task,
            TaskType.TYPE_TENSORFLOW_TRAIN_TASK: self._schedule_tensorflow_task,
            TaskType.TYPE_TENSORFLOW_TEST_TASK: self._schedule_tensorflow_task,
        }
        Logger().log("All Slaves :", slaves)
        slaves = [slave for slave in slaves if slave.slave_info is not None]
        Logger().log("Reported Slaves :", slaves)
        slaves = [slave for slave in slaves if task not in slave.failed_tasks]
        Logger().log("Unfailed Slaves :", slaves)
        return __schedule_dict[get_task_type_of_task(task)](slaves, task)

    def _schedule_sleep_task(self, slaves: Iterable, task) -> Slave:
        Logger().log('**** Schedule of sleep task')
        if not slaves:
            raise NotAvailableSlaveError
        return random.choice(slaves), None, AllocatedResource()

    def _schedule_data_processing_task(self, slaves: Iterable, task) -> Slave:
        Logger().log('**** Schedule of data processing task. slaves = {0}'.format(slaves))
        return self._schedule_cpu_to_task(slaves, task)


    def _schedule_tensorflow_task(self, slaves: Iterable, task) -> Slave:
        Logger().log('**** Schedule of tensorflow task')
        try:
            Logger().log('******* Try GPU')
            return self._schedule_gpu_to_task(slaves, task)
        except NotAvailableSlaveError:
            Logger().log('******* Fail. So Try CPU')
            return self._schedule_cpu_to_task(slaves, task)

    def _schedule_cpu_to_task(self, slaves: Iterable, task) -> Slave:
        best_slave = None
        best_cpu_rest = None
        best_avail_cpu_count = None
        for slave in slaves:
            slave_info = slave.slave_info
            alloc_info = slave.alloc_info
            if alloc_info is None or not alloc_info.cpu_available:
                continue

            cpu_rest = 0
            for cpu_percent in slave_info.cpu_info.cpu_percents:
                cpu_rest += 100 - cpu_percent

            if best_slave is None or best_avail_cpu_count < alloc_info.avail_cpu_count:
                best_slave = slave
                best_cpu_rest = cpu_rest
                best_avail_cpu_count = alloc_info.avail_cpu_count
            elif best_avail_cpu_count == alloc_info.avail_cpu_count and best_cpu_rest < cpu_rest:
                best_slave = slave
                best_cpu_rest = cpu_rest

        if best_slave is None:
            raise NotAvailableSlaveError

        best_slave.alloc_info.alloc_cpu_count = best_slave.alloc_info.all_cpu_count
        return best_slave, RunConfig(), AllocatedResource(alloc_cpu_count=best_slave.alloc_info.all_cpu_count)

    def _schedule_gpu_to_task(self, slaves: Iterable, task) -> Slave:
        best_slave = None
        best_alloc_tf_gpu_info = None
        best_cc_major = None
        best_cc_minor = None
        for slave in slaves:
            slave_info = slave.slave_info
            alloc_info = slave.alloc_info
            if alloc_info is None:
                continue

            for alloc_tf_gpu_info in alloc_info.alloc_tf_gpu_info_list:
                if not alloc_tf_gpu_info.available:
                    continue
                if best_slave is None or alloc_tf_gpu_info.compute_capability_major > best_cc_major or \
                        (alloc_tf_gpu_info.compute_capability_major == best_cc_major and
                                 alloc_tf_gpu_info.compute_capability_minor > best_cc_minor):
                    best_slave = slave
                    best_alloc_tf_gpu_info = alloc_tf_gpu_info
                    best_cc_major = alloc_tf_gpu_info.compute_capability_major
                    best_cc_minor = alloc_tf_gpu_info.compute_capability_minor

        if best_slave is None:
            raise NotAvailableSlaveError

        best_alloc_tf_gpu_info.available = False
        return best_slave, RunConfig(tf_device=best_alloc_tf_gpu_info.tf_device), \
               AllocatedResource(alloc_tf_gpu_info=best_alloc_tf_gpu_info)