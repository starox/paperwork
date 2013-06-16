import heapq
import logging
import os
import itertools
import sys
import threading
import traceback
import time

from gi.repository import GObject

"""
Job scheduling

A major issue in Paperwork are non-thread-safe dependencies (for instance,
libpoppler). This is solved by having only one thread other than the Gtk
main-loop thread. It is the job scheduler. Any long action is run in this
thread to avoid blocking the GUI.
"""

logger = logging.getLogger(__name__)


class JobException(Exception):
    def __init__(self, reason):
        Exception.__init__(self, reason)


class JobFactory(object):
    def __init__(self, name):
        self.name = name
        self.id_generator = itertools.count()

    def make(self, *args, **kwargs):
        """Child class must override this method"""
        raise NotImplementedError()

    def __eq__(self, other):
        return self is other


class Job(GObject.GObject):  # inherits from GObject so it can send signals
    MAX_TIME_FOR_UNSTOPPABLE_JOB = 0.5  # secs
    MAX_TIME_TO_STOP = 0.5  # secs

    # some jobs can be interrupted. In that case, the job should store in
    # the instance where it stopped, so it can resume its work when do()
    # is called again.
    # If can_stop = False, the job should never last more than
    # MAX_TIME_FOR_UNSTOPPABLE_JOB
    can_stop = False

    priority = 0  # the higher priority is run first

    started_by = None  # set by the scheduler

    def __init__(self, job_factory, job_id):
        GObject.GObject.__init__(self)
        self.factory = job_factory
        self.id = job_id

        self._wait_time = None
        self._wait_cond = threading.Condition()

    def _wait(self, wait_time):
        """Convenience function to wait while being stoppable"""
        if self._wait_time is None:
            self._wait_time = wait_time

        start = time.time()
        self._wait_cond.acquire()
        try:
            self._wait_cond.wait(self._wait_time)
        finally:
            self._wait_cond.release()
            stop = time.time()
            self._wait_time -= (stop - start)

    def _stop_wait(self):
        self._wait_cond.acquire()
        try:
            self._wait_cond.notify_all()
        finally:
            self._wait_cond.release()

    def do(self):
        """Child class must override this method"""
        raise NotImplementedError()

    def stop(self, will_resume=False):
        """
        Only called if can_stop == True.
        Child class must override this method if can_stop == True.
        This function is run from the Gtk thread. It must *not* block
        """
        raise NotImplementedError()

    def __eq__(self, other):
        return self is other

    def __str__(self):
        return ("%s:%d" % (self.factory.name, self.id))


class JobScheduler(object):
    def __init__(self, name):
        self.name = name
        self._thread = None
        self.running = False

        # _job_queue_cond.acquire()/release() protect the job queue
        # _job_queue_cond.notify_all() is called each time the queue is modified
        #  (except on cancel())
        self._job_queue_cond = threading.Condition()
        self._job_queue = []
        self._active_job = None

        self._job_idx_generator = itertools.count()

    def start(self):
        """Starts the scheduler"""
        assert(not self.running)
        assert(self._thread is None)
        logger.info("[Scheduler %s] Starting" % self.name)
        self._thread = threading.Thread(target=self._run)
        self.running = True
        self._thread.start()

    def _run(self):
        logger.info("[Scheduler %s] Started" % self.name)

        while self.running:

            self._job_queue_cond.acquire()
            try:
                while len(self._job_queue) <= 0:
                    self._job_queue_cond.wait()
                    if not self.running:
                        return
                (_, _, self._active_job) = heapq.heappop(self._job_queue)
            finally:
                self._job_queue_cond.release()

            if not self.running:
                return

            # we are the only thread changing self._active_job,
            # so we can safely use it even if we didn't keep the lock
            # on self._job_queue_lock

            assert(self._active_job is not None)

            start = time.time()
            try:
                self._active_job.do()
            except Exception, exc:
                logger.error("===> Job %s raised an exception: %s: %s"
                             % (str(self._active_job),
                                type(exc), str(exc)))
                idx = 0
                for stack_el in traceback.extract_tb(sys.exc_info()[2]):
                    logger.error("%2d: %20s: L%5d: %s"
                                 % (idx, os.path.basename(stack_el[0]),
                                    stack_el[1], stack_el[2]))
                    idx += 1
                logger.error("---> Job %s was started by:"
                             % (str(self._active_job)))
                idx = 0
                for stack_el in self._active_job.started_by:
                    logger.error("%2d: %20s: L%5d: %s"
                                 % (idx, os.path.basename(stack_el[0]),
                                    stack_el[1], stack_el[2]))
                    idx += 1
            stop = time.time()

            diff = start - stop
            if (self._active_job.can_stop
                or diff <= Job.MAX_TIME_FOR_UNSTOPPABLE_JOB):
                logger.debug("Job %s took %dms"
                             % (str(self._active_job), diff * 1000))
            else:
                logger.warning("Job %s took %dms and is unstoppable !"
                               " (maximum allowed: %dms)"
                               % (str(self._active_job), diff * 1000,
                                  Job.MAX_TIME_FOR_UNSTOPPABLE_JOB * 1000))

            self._job_queue_cond.acquire()
            try:
                self._active_job = None
                self._job_queue_cond.notify_all()
            finally:
                self._job_queue_cond.release()

            if not self.running:
                return

    def _stop_active_job(self, will_resume=False):
        active_job = self._active_job

        if active_job.can_stop:
            active_job.stop(will_resume=will_resume)
        else:
            logger.warning(
                "[Scheduler %s] Tried to stop job %s, but it can't"
                " be stopped"
                % (self.name, str(active_job)))
        start = time.time()
        while active_job == self._active_job:
            self._job_queue_cond.wait()
        stop = time.time()
        diff = stop - start
        if active_job.can_stop and diff > Job.MAX_TIME_TO_STOP:
            logger.warning("[Scheduler %s] Took %dms to stop job %s !"
                           " (maximum allowed: %dms)"
                           % (self.name, diff * 1000,
                              str(active_job),
                              Job.MAX_TIME_TO_STOP * 1000))
        logger.debug("[Scheduler %s] Job %s halted"
                     % (self.name, str(active_job)))

    def schedule(self, job):
        logger.debug("[Scheduler %s] Queuing job %s"
                     % (self.name, str(job)))

        job.started_by = traceback.extract_stack()

        self._job_queue_cond.acquire()
        try:
            heapq.heappush(self._job_queue,
                           (-1 * job.priority, next(self._job_idx_generator),
                            job))

            # if a job with a lower priority is running, we try to stop
            # it and take its place
            active = self._active_job
            if active is not None and active.priority < job.priority:
                self._stop_active_job(will_resume=True)
                heapq.heappush(self._job_queue, (-1 * active.priority,
                                                 next(self._job_idx_generator),
                                                 active))
            self._job_queue_cond.notify_all()
        finally:
            self._job_queue_cond.release()

    def _cancel_matching_jobs(self, condition):
        self._job_queue_cond.acquire()
        try:
            try:
                for job in self._job_queue:
                    if condition(job[2]):
                        self._job_queue.remove(job)
                        logger.debug("[Scheduler %s] Job %s cancelled"
                                     % (self.name, str(job[1])))
            except ValueError:
                pass

            if (self._active_job is not None and condition(self._active_job)):
                self._stop_active_job(will_resume=False)
        finally:
            self._job_queue_cond.release()

    def cancel(self, target_job):
        logger.debug("[Scheduler %s] Canceling job %s"
                     % (self.name, str(job)))
        self._cancel_matching_jobs(
            lambda job: (job == target_job))

    def cancel_all(self, factory):
        logger.debug("[Scheduler %s] Canceling all jobs %s"
                     % (self.name, factory.name))
        self._cancel_matching_jobs(
            lambda job: (job.factory == factory))

    def stop(self):
        assert(self.running)
        assert(self._thread is not None)
        logger.info("[Scheduler %s] Stopping" % self.name)

        self.running = False

        self._job_queue_cond.acquire()
        try:
            self._job_queue_cond.notify_all()
        finally:
            self._job_queue_cond.release()

        self._thread.join()
        self._thread = None

        logger.info("[Scheduler %s] Stopped" % self.name)
