#
#

# Copyright (C) 2006, 2007, 2008, 2009, 2010, 2011, 2012 Google Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA
# 02110-1301, USA.


"""Module implementing the job queue handling using disk as backend
storage type.

"""

# pylint: disable=W0212
# W0212: Access to a protected member %s of a client class

import logging
import errno
import time
import itertools

try:
  # pylint: disable=E0611
  from pyinotify import pyinotify
except ImportError:
  import pyinotify

from ganeti import asyncnotifier
from ganeti import constants
from ganeti import serializer
from ganeti import locking
from ganeti import errors
from ganeti import utils
from ganeti import jstore
from ganeti import runtime
from ganeti import compat
from ganeti import ht
from ganeti import pathutils
from ganeti import vcluster

from ganeti.jqueue import base

# Function and constants needed from base file
_LOCK = base._LOCK
_QueuedJob = base._QueuedJob
_RequireOpenQueue = base._RequireOpenQueue
_JobQueueWorkerPool = base._JobQueueWorkerPool
_JobDependencyManager = base._JobDependencyManager


def _CallJqUpdate(runner, names, file_name, content):
  """Updates job queue file after virtualizing filename.

  """
  virt_file_name = vcluster.MakeVirtualPath(file_name)
  return runner.call_jobqueue_update(names, virt_file_name, content)


class _DiskJobFileChangesWaiter(base._BaseJobFileChangesWaiter):
  def __init__(self, filename, _inotify_wm_cls=pyinotify.WatchManager):
    """Initializes this class.

    @type filename: string
    @param filename: Path to job file
    @raises errors.InotifyError: if the notifier cannot be setup

    """
    self._wm = _inotify_wm_cls()
    self._inotify_handler = \
      asyncnotifier.SingleFileEventHandler(self._wm, self._OnInotify, filename)
    self._notifier = \
      pyinotify.Notifier(self._wm, default_proc_fun=self._inotify_handler)
    try:
      self._inotify_handler.enable()
    except Exception:
      # pyinotify doesn't close file descriptors automatically
      self._notifier.stop()
      raise

  def _OnInotify(self, notifier_enabled):
    """Callback for inotify.

    """
    if not notifier_enabled:
      self._inotify_handler.enable()

  def Wait(self, timeout):
    """Waits for the job file to change.

    @type timeout: float
    @param timeout: Timeout in seconds
    @return: Whether there have been events

    """
    assert timeout >= 0
    have_events = self._notifier.check_events(timeout * 1000)
    if have_events:
      self._notifier.read_events()
    self._notifier.process_events()
    return have_events

  def Close(self):
    """Closes underlying notifier and its file descriptor.

    """
    self._notifier.stop()


class _JobChangesWaiter(object):
  def __init__(self, filename, _waiter_cls=_DiskJobFileChangesWaiter):
    """Initializes this class.

    @type filename: string
    @param filename: Path to job file

    """
    self._filewaiter = None
    self._filename = filename
    self._waiter_cls = _waiter_cls

  def Wait(self, timeout):
    """Waits for a job to change.

    @type timeout: float
    @param timeout: Timeout in seconds
    @return: Whether there have been events

    """
    if self._filewaiter:
      return self._filewaiter.Wait(timeout)

    # Lazy setup: Avoid inotify setup cost when job file has already changed.
    # If this point is reached, return immediately and let caller check the job
    # file again in case there were changes since the last check. This avoids a
    # race condition.
    self._filewaiter = self._waiter_cls(self._filename)

    return True

  def Close(self):
    """Closes underlying waiter.

    """
    if self._filewaiter:
      self._filewaiter.Close()


class _DiskWaitForJobChangesHelper(base._BaseWaitForJobChangesHelper):
  """Helper class using inotify to wait for changes in a job file.

  This class takes a previous job status and serial, and alerts the client when
  the current job status has changed.

  """
  @staticmethod
  def _CheckForChanges(counter, job_load_fn, check_fn):
    if counter.next() > 0:
      # If this isn't the first check the job is given some more time to change
      # again. This gives better performance for jobs generating many
      # changes/messages.
      time.sleep(0.1)

    job = job_load_fn()
    if not job:
      raise errors.JobLost()

    result = check_fn(job)
    if result is None:
      raise utils.RetryAgain()

    return result

  def __call__(self, filename, job_load_fn,
             fields, prev_job_info, prev_log_serial, timeout,
             _waiter_cls=_JobChangesWaiter):
    """Waits for changes on a job.

    @type fields: list of strings
    @param fields: Which fields to check for changes
    @type prev_job_info: list or None
    @param prev_job_info: Last job information returned
    @type prev_log_serial: int
    @param prev_log_serial: Last job message serial number
    @type timeout: float
    @param timeout: maximum time to wait in seconds
    @type filename: string
    @param filename: File on which to wait for changes
    @type job_load_fn: callable
    @param job_load_fn: Function to load job

    """
    counter = itertools.count()
    try:
      check_fn = base._JobChangesChecker(fields, prev_job_info, prev_log_serial)
      waiter = _waiter_cls(filename)
      try:
        return utils.Retry(compat.partial(self._CheckForChanges,
                                          counter, job_load_fn, check_fn),
                           utils.RETRY_REMAINING_TIME, timeout,
                           wait_fn=waiter.Wait)
      finally:
        waiter.Close()
    except errors.JobLost:
      return None
    except utils.RetryTimeout:
      return constants.JOB_NOTCHANGED


class DiskJobQueue(base.BaseJobQueue):
  """Queue used to manage the jobs.

  """
  def __init__(self, context):
    """Constructor for JobQueue.

    The constructor will initialize the job queue object and then
    start loading the current jobs from disk, either for starting them
    (if they were queue) or for aborting them (if they were already
    running).

    @type context: GanetiContext
    @param context: the context object for access to the configuration
        data and other ganeti objects

    """
    super(DiskJobQueue, self).__init__(context)
    # Initialize the queue, and acquire the filelock.
    # This ensures no other process is working on the job queue.
    self._queue_filelock = jstore.InitAndVerifyQueue(must_lock=True)

    # Read serial file
    self._last_serial = jstore.ReadSerial()
    assert self._last_serial is not None, ("Serial file was modified between"
                                           " check in jstore and here")

    # Get initial list of nodes
    self._nodes = dict((n.name, n.primary_ip)
                       for n in self.context.cfg.GetAllNodesInfo().values()
                       if n.master_candidate)

    # Remove master node
    self._nodes.pop(self._my_hostname, None)

    # TODO: Check consistency across nodes

    self._queue_size = None
    self._UpdateQueueSizeUnlocked()
    assert ht.TInt(self._queue_size)
    self._drained = jstore.CheckDrainFlag()

    # Job dependencies
    self.depmgr = _JobDependencyManager(self._GetJobStatusForDependencies,
                                        self._EnqueueJobs)
    self.context.glm.AddToLockMonitor(self.depmgr)

    # Setup worker pool
    self._wpool = _JobQueueWorkerPool(self)
    try:
      self._InspectQueue()
    except:
      self._wpool.TerminateWorkers()
      raise

  @locking.ssynchronized(_LOCK)
  @_RequireOpenQueue
  def _InspectQueue(self):
    """Loads the whole job queue and resumes unfinished jobs.

    This function needs the lock here because WorkerPool.AddTask() may start a
    job while we're still doing our work.

    """
    logging.info("Inspecting job queue")

    restartjobs = []

    all_job_ids = self._GetJobIDsUnlocked()
    jobs_count = len(all_job_ids)
    lastinfo = time.time()
    for idx, job_id in enumerate(all_job_ids):
      # Give an update every 1000 jobs or 10 seconds
      if (idx % 1000 == 0 or time.time() >= (lastinfo + 10.0) or
          idx == (jobs_count - 1)):
        logging.info("Job queue inspection: %d/%d (%0.1f %%)",
                     idx, jobs_count - 1, 100.0 * (idx + 1) / jobs_count)
        lastinfo = time.time()

      job = self._LoadJobUnlocked(job_id)

      # a failure in loading the job can cause 'None' to be returned
      if job is None:
        continue

      status = job.CalcStatus()

      if status == constants.JOB_STATUS_QUEUED:
        restartjobs.append(job)

      elif status in (constants.JOB_STATUS_RUNNING,
                      constants.JOB_STATUS_WAITING,
                      constants.JOB_STATUS_CANCELING):
        logging.warning("Unfinished job %s found: %s", job.id, job)

        if status == constants.JOB_STATUS_WAITING:
          # Restart job
          job.MarkUnfinishedOps(constants.OP_STATUS_QUEUED, None)
          restartjobs.append(job)
        else:
          job.MarkUnfinishedOps(constants.OP_STATUS_ERROR,
                                "Unclean master daemon shutdown")
          job.Finalize()

        self.UpdateJobUnlocked(job)

    if restartjobs:
      logging.info("Restarting %s jobs", len(restartjobs))
      self._EnqueueJobsUnlocked(restartjobs)

    logging.info("Job queue inspection finished")

  @locking.ssynchronized(_LOCK)
  @_RequireOpenQueue
  def AddNode(self, node):
    """Register a new node with the queue.

    @type node: L{objects.Node}
    @param node: the node object to be added

    """
    node_name = node.name
    assert node_name != self._my_hostname

    # Clean queue directory on added node
    result = self._GetRpc(None).call_jobqueue_purge(node_name)
    msg = result.fail_msg
    if msg:
      logging.warning("Cannot cleanup queue directory on node %s: %s",
                      node_name, msg)

    if not node.master_candidate:
      # remove if existing, ignoring errors
      self._nodes.pop(node_name, None)
      # and skip the replication of the job ids
      return

    # Upload the whole queue excluding archived jobs
    files = [self._GetJobPath(job_id) for job_id in self._GetJobIDsUnlocked()]

    # Upload current serial file
    files.append(pathutils.JOB_QUEUE_SERIAL_FILE)

    # Static address list
    addrs = [node.primary_ip]

    for file_name in files:
      # Read file content
      content = utils.ReadFile(file_name)

      result = _CallJqUpdate(self._GetRpc(addrs), [node_name],
                             file_name, content)
      msg = result[node_name].fail_msg
      if msg:
        logging.error("Failed to upload file %s to node %s: %s",
                      file_name, node_name, msg)

    # Set queue drained flag
    result = \
      self._GetRpc(addrs).call_jobqueue_set_drain_flag([node_name],
                                                       self._drained)
    msg = result[node_name].fail_msg
    if msg:
      logging.error("Failed to set queue drained flag on node %s: %s",
                    node_name, msg)

    self._nodes[node_name] = node.primary_ip

  @locking.ssynchronized(_LOCK)
  @_RequireOpenQueue
  def RemoveNode(self, node):
    """Callback called when removing nodes from the cluster.

    @type node: L{objects.Node}
    @param node: the node object to remove

    """
    self._nodes.pop(node.name, None)

  def _UpdateJobQueueFile(self, file_name, data, replicate):
    """Writes a file locally and then replicates it to all nodes.

    This function will replace the contents of a file on the local
    node and then replicate it to all the other nodes we have.

    @type file_name: str
    @param file_name: the path of the file to be replicated
    @type data: str
    @param data: the new contents of the file
    @type replicate: boolean
    @param replicate: whether to spread the changes to the remote nodes

    """
    getents = runtime.GetEnts()
    utils.WriteFile(file_name, data=data, uid=getents.masterd_uid,
                    gid=getents.daemons_gid,
                    mode=constants.JOB_QUEUE_FILES_PERMS)

    if replicate:
      names, addrs = self._GetNodeIp()
      result = _CallJqUpdate(self._GetRpc(addrs), names, file_name, data)
      self._CheckRpcResult(result, self._nodes, "Updating %s" % file_name)

  def _RenameFilesUnlocked(self, rename):
    """Renames a file locally and then replicate the change.

    This function will rename a file in the local queue directory
    and then replicate this rename to all the other nodes we have.

    @type rename: list of (old, new)
    @param rename: List containing tuples mapping old to new names

    """
    # Rename them locally
    for old, new in rename:
      utils.RenameFile(old, new, mkdir=True)

    # ... and on all nodes
    names, addrs = self._GetNodeIp()
    result = self._GetRpc(addrs).call_jobqueue_rename(names, rename)
    self._CheckRpcResult(result, self._nodes, "Renaming files (%r)" % rename)

  def _NewSerialsUnlocked(self, count):
    """Generates a new job identifier.

    Job identifiers are unique during the lifetime of a cluster.

    @type count: integer
    @param count: how many serials to return
    @rtype: list of int
    @return: a list of job identifiers.

    """
    assert ht.TNonNegativeInt(count)

    # New number
    serial = self._last_serial + count

    # Write to file
    self._UpdateJobQueueFile(pathutils.JOB_QUEUE_SERIAL_FILE,
                             "%s\n" % serial, True)

    result = [jstore.FormatJobID(v)
              for v in range(self._last_serial + 1, serial + 1)]

    # Keep it only if we were able to write the file
    self._last_serial = serial

    assert len(result) == count

    return result

  @staticmethod
  def _GetJobPath(job_id):
    """Returns the job file for a given job id.

    @type job_id: str
    @param job_id: the job identifier
    @rtype: str
    @return: the path to the job file

    """
    return utils.PathJoin(pathutils.QUEUE_DIR, "job-%s" % job_id)

  @staticmethod
  def _GetArchivedJobPath(job_id):
    """Returns the archived job file for a give job id.

    @type job_id: str
    @param job_id: the job identifier
    @rtype: str
    @return: the path to the archived job file

    """
    return utils.PathJoin(pathutils.JOB_QUEUE_ARCHIVE_DIR,
                          jstore.GetArchiveDirectory(job_id),
                          "job-%s" % job_id)

  @staticmethod
  def _DetermineJobDirectories(archived):
    """Build list of directories containing job files.

    @type archived: bool
    @param archived: Whether to include directories for archived jobs
    @rtype: list

    """
    result = [pathutils.QUEUE_DIR]

    if archived:
      archive_path = pathutils.JOB_QUEUE_ARCHIVE_DIR
      result.extend(map(compat.partial(utils.PathJoin, archive_path),
                        utils.ListVisibleFiles(archive_path)))

    return result

  @classmethod
  def _GetJobIDsUnlocked(cls, sort=True, archived=False):
    """Return all known job IDs.

    The method only looks at disk because it's a requirement that all
    jobs are present on disk (so in the _memcache we don't have any
    extra IDs).

    @type sort: boolean
    @param sort: perform sorting on the returned job ids
    @rtype: list
    @return: the list of job IDs

    """
    jlist = []

    for path in cls._DetermineJobDirectories(archived):
      for filename in utils.ListVisibleFiles(path):
        m = constants.JOB_FILE_RE.match(filename)
        if m:
          jlist.append(int(m.group(1)))

    if sort:
      jlist.sort()
    return jlist

  def _LoadJobUnlocked(self, job_id):
    """Loads a job from the disk or memory.

    Given a job id, this will return the cached job object if
    existing, or try to load the job from the disk. If loading from
    disk, it will also add the job to the cache.

    @type job_id: int
    @param job_id: the job id
    @rtype: L{_QueuedJob} or None
    @return: either None or the job object

    """
    job = self._memcache.get(job_id, None)
    if job:
      logging.debug("Found job %s in memcache", job_id)
      assert job.writable, "Found read-only job in memcache"
      return job

    try:
      job = self._LoadJobFromDisk(job_id, False)
      if job is None:
        return job
    except errors.JobFileCorrupted:
      old_path = self._GetJobPath(job_id)
      new_path = self._GetArchivedJobPath(job_id)
      if old_path == new_path:
        # job already archived (future case)
        logging.exception("Can't parse job %s", job_id)
      else:
        # non-archived case
        logging.exception("Can't parse job %s, will archive.", job_id)
        self._RenameFilesUnlocked([(old_path, new_path)])
      return None

    assert job.writable, "Job just loaded is not writable"

    self._memcache[job_id] = job
    logging.debug("Added job %s to the cache", job_id)
    return job

  def _LoadJobFromDisk(self, job_id, try_archived, writable=None):
    """Load the given job file from disk.

    Given a job file, read, load and restore it in a _QueuedJob format.

    @type job_id: int
    @param job_id: job identifier
    @type try_archived: bool
    @param try_archived: Whether to try loading an archived job
    @rtype: L{_QueuedJob} or None
    @return: either None or the job object

    """
    path_functions = [(self._GetJobPath, False)]

    if try_archived:
      path_functions.append((self._GetArchivedJobPath, True))

    raw_data = None
    archived = None

    for (fn, archived) in path_functions:
      filepath = fn(job_id)
      logging.debug("Loading job from %s", filepath)
      try:
        raw_data = utils.ReadFile(filepath)
      except EnvironmentError, err:
        if err.errno != errno.ENOENT:
          raise
      else:
        break

    if not raw_data:
      return None

    if writable is None:
      writable = not archived

    try:
      data = serializer.LoadJson(raw_data)
      job = _QueuedJob.Restore(self, data, writable, archived)
    except Exception, err: # pylint: disable=W0703
      raise errors.JobFileCorrupted(err)

    return job

  def SafeLoadJobFromDisk(self, job_id, try_archived, writable=None):
    """Load the given job file from disk.

    Given a job file, read, load and restore it in a _QueuedJob format.
    In case of error reading the job, it gets returned as None, and the
    exception is logged.

    @type job_id: int
    @param job_id: job identifier
    @type try_archived: bool
    @param try_archived: Whether to try loading an archived job
    @rtype: L{_QueuedJob} or None
    @return: either None or the job object

    """
    try:
      return self._LoadJobFromDisk(job_id, try_archived, writable=writable)
    except (errors.JobFileCorrupted, EnvironmentError):
      logging.exception("Can't load/parse job %s", job_id)
      return None

  def _UpdateQueueSizeUnlocked(self):
    """Update the queue size.

    """
    self._queue_size = len(self._GetJobIDsUnlocked(sort=False))

  @locking.ssynchronized(_LOCK)
  @_RequireOpenQueue
  def SetDrainFlag(self, drain_flag):
    """Sets the drain flag for the queue.

    @type drain_flag: boolean
    @param drain_flag: Whether to set or unset the drain flag

    """
    # Change flag locally
    jstore.SetDrainFlag(drain_flag)

    self._drained = drain_flag

    # ... and on all nodes
    (names, addrs) = self._GetNodeIp()
    result = \
      self._GetRpc(addrs).call_jobqueue_set_drain_flag(names, drain_flag)
    self._CheckRpcResult(result, self._nodes,
                         "Setting queue drain flag to %s" % drain_flag)

    return True

  @_RequireOpenQueue
  def UpdateJobUnlocked(self, job, replicate=True):
    """Update a job's on disk storage.

    After a job has been modified, this function needs to be called in
    order to write the changes to disk and replicate them to the other
    nodes.

    @type job: L{_QueuedJob}
    @param job: the changed job
    @type replicate: boolean
    @param replicate: whether to replicate the change to remote nodes

    """
    if __debug__:
      finalized = job.CalcStatus() in constants.JOBS_FINALIZED
      assert (finalized ^ (job.end_timestamp is None))
      assert job.writable, "Can't update read-only job"
      assert not job.archived, "Can't update archived job"

    filename = self._GetJobPath(job.id)
    data = serializer.DumpJson(job.Serialize())
    logging.debug("Writing job %s to %s", job.id, filename)
    self._UpdateJobQueueFile(filename, data, replicate)

  def WaitForJobChanges(self, job_id, fields, prev_job_info, prev_log_serial,
                        timeout):
    """Waits for changes in a job.

    @type job_id: int
    @param job_id: Job identifier
    @type fields: list of strings
    @param fields: Which fields to check for changes
    @type prev_job_info: list or None
    @param prev_job_info: Last job information returned
    @type prev_log_serial: int
    @param prev_log_serial: Last job message serial number
    @type timeout: float
    @param timeout: maximum time to wait in seconds
    @rtype: tuple (job info, log entries)
    @return: a tuple of the job information as required via
        the fields parameter, and the log entries as a list

        if the job has not changed and the timeout has expired,
        we instead return a special value,
        L{constants.JOB_NOTCHANGED}, which should be interpreted
        as such by the clients

    """
    load_fn = compat.partial(self.SafeLoadJobFromDisk, job_id, True,
                             writable=False)

    helper = _DiskWaitForJobChangesHelper()

    return helper(self._GetJobPath(job_id), load_fn,
                  fields, prev_job_info, prev_log_serial, timeout)

  @_RequireOpenQueue
  def _ArchiveJobsUnlocked(self, jobs):
    """Archives jobs.

    @type jobs: list of L{_QueuedJob}
    @param jobs: Job objects
    @rtype: int
    @return: Number of archived jobs

    """
    archive_jobs = []
    rename_files = []
    for job in jobs:
      assert job.writable, "Can't archive read-only job"
      assert not job.archived, "Can't cancel archived job"

      if job.CalcStatus() not in constants.JOBS_FINALIZED:
        logging.debug("Job %s is not yet done", job.id)
        continue

      archive_jobs.append(job)

      old = self._GetJobPath(job.id)
      new = self._GetArchivedJobPath(job.id)
      rename_files.append((old, new))

    # TODO: What if 1..n files fail to rename?
    self._RenameFilesUnlocked(rename_files)

    logging.debug("Successfully archived job(s) %s",
                  utils.CommaJoin(job.id for job in archive_jobs))

    # Since we haven't quite checked, above, if we succeeded or failed renaming
    # the files, we update the cached queue size from the filesystem. When we
    # get around to fix the TODO: above, we can use the number of actually
    # archived jobs to fix this.
    self._UpdateQueueSizeUnlocked()
    return len(archive_jobs)

  @locking.ssynchronized(_LOCK)
  @_RequireOpenQueue
  def ArchiveJob(self, job_id):
    """Archives a job.

    This is just a wrapper over L{_ArchiveJobsUnlocked}.

    @type job_id: int
    @param job_id: Job ID of job to be archived.
    @rtype: bool
    @return: Whether job was archived

    """
    logging.info("Archiving job %s", job_id)

    job = self._LoadJobUnlocked(job_id)
    if not job:
      logging.debug("Job %s not found", job_id)
      return False

    return self._ArchiveJobsUnlocked([job]) == 1

  @locking.ssynchronized(_LOCK)
  @_RequireOpenQueue
  def AutoArchiveJobs(self, age, timeout):
    """Archives all jobs based on age.

    The method will archive all jobs which are older than the age
    parameter. For jobs that don't have an end timestamp, the start
    timestamp will be considered. The special '-1' age will cause
    archival of all jobs (that are not running or queued).

    @type age: int
    @param age: the minimum age in seconds

    """
    logging.info("Archiving jobs with age more than %s seconds", age)

    now = time.time()
    end_time = now + timeout
    archived_count = 0
    last_touched = 0

    all_job_ids = self._GetJobIDsUnlocked()
    pending = []
    for idx, job_id in enumerate(all_job_ids):
      last_touched = idx + 1

      # Not optimal because jobs could be pending
      # TODO: Measure average duration for job archival and take number of
      # pending jobs into account.
      if time.time() > end_time:
        break

      # Returns None if the job failed to load
      job = self._LoadJobUnlocked(job_id)
      if job:
        if job.end_timestamp is None:
          if job.start_timestamp is None:
            job_age = job.received_timestamp
          else:
            job_age = job.start_timestamp
        else:
          job_age = job.end_timestamp

        if age == -1 or now - job_age[0] > age:
          pending.append(job)

          # Archive 10 jobs at a time
          if len(pending) >= 10:
            archived_count += self._ArchiveJobsUnlocked(pending)
            pending = []

    if pending:
      archived_count += self._ArchiveJobsUnlocked(pending)

    return (archived_count, len(all_job_ids) - last_touched)
