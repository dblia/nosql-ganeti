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


"""CouchDB driver for the job queue handling"""

# pylint: disable=W0212
# W0212: Access to a protected member %s of a client class

# pylint: disable=W0221
# W0221: Arguments number differs from overridden method

# pylint: disable=W0232
# W0232: Class has no __init__ method

# pylint: disable=W0703
# W0703: Catching too general exception Exception

import copy
import logging
import time
import itertools

try:
  import simplejson as json
except ImportError:
  import json

from ganeti import constants
from ganeti import serializer
from ganeti import locking
from ganeti import errors
from ganeti import utils
from ganeti import jstore
from ganeti import netutils
from ganeti import compat
from ganeti import ht

from ganeti.jqueue.base import \
    _BaseJobFileChangesWaiter, \
    _BaseWaitForJobChangesHelper, \
    _BaseJobQueue, \
    _RequireOpenQueue, \
    _JobDependencyManager, \
    _JobQueueWorkerPool, \
    _JobChangesChecker, \
    _QueuedJob, \
    _LOCK


class _CouchDBJobFileChangesWaiter(_BaseJobFileChangesWaiter):
  def __init__(self, db_name, job_id, since=None):
    """Initializes this class.

    @type db_name: L{couchdb.client.Database}
    @param db_name: Database name
    @type job_id: int
    @param job_id: Job id to poll for changes
    @type since: int
    @param since: Search for changes immediately after the given
                  sequence number.

    """
    super(_CouchDBJobFileChangesWaiter, self).__init__()
    self.db_name = db_name
    self.job_id = str(job_id)
    if since:
      self.since = since
    else:
      self.since = self.db_name.changes()["last_seq"]

  def Wait(self, timeout):
    """Waits for the job to change.

    @type timeout: float
    @param timeout: Timeout in seconds
    @rtype: tuple of '("Polling", result)' format.
    @return: Whether have bee events. If timeout expires result is False,
             otherwise a new _QueuedJob object with the new data is returned.
             "Polling" used to distinguish this case in utils.Retry function.

    """
    assert timeout >= 0
    result = False
    # Convert timeout to int, because '_changes' feed timeout option accepts
    # integer values only, and increase it by one for rounding reasons.
    timeout = int(timeout) + 1
    have_events = self.db_name.changes(filter="filter/job_id", id=self.job_id,
                                       feed="longpoll", include_docs=True,
                                       since=self.since, timeout=timeout * 1000)
    if have_events["results"]:
      try:
        data = have_events["results"][0]["doc"]
        raw = json.loads(data["info"])
        result = _QueuedJob.Restore(self, raw, writable=False, archived=None)
      # FIXME: Improve error handling for that case
      except Exception, err:
        raise errors.JobFileCorrupted(err)

    self.since = have_events["last_seq"]

    return ("Polling", result)


class _CouchDBWaitForJobChangesHelper(_BaseWaitForJobChangesHelper):
  """Helper class to wait for changes in a job document.

  This class takes a previous job status and serial, and alerts the client when
  the current job status has changed.

  """
  @staticmethod
  def _CheckForChanges(counter, check_fn, job):
    if counter.next() > 0:
      # If this isn't the first check the job is given some more time to
      # change again. This gives better performance for jobs generating
      # many changes/messages.
      time.sleep(0.1)

    if not job:
      raise errors.JobLost()

    result = check_fn(job)
    if result is None:
      raise utils.RetryAgain()

    return result

  def __call__(self, fields, prev_job_info, prev_log_serial, timeout,
               db_name, job, _waiter_cls=_CouchDBJobFileChangesWaiter):
    """Waits for changes on a job.

    @type fields: list of strings
    @param fields: Which fields to check for changes
    @type prev_job_info: list or None
    @param prev_job_info: Last job information returned
    @type prev_log_serial: int
    @param prev_log_serial: Last job message serial number
    @type timeout: float
    @param timeout: maximum time to wait in seconds
    @type db_name: L{couchdb.client.Database}
    @param db_name: Database name
    @type job: L{_QueuedJob}
    @param job: Job object to poll for changes

    """
    counter = itertools.count()
    try:
      check_fn = _JobChangesChecker(fields, prev_job_info, prev_log_serial)
      waiter = _waiter_cls(db_name, job.id)
      return utils.Retry(compat.partial(self._CheckForChanges, counter,
                                        check_fn),
                         utils.RETRY_REMAINING_TIME, timeout, args=[job],
                         wait_fn=waiter.Wait)
    except errors.JobLost:
      return None
    except utils.RetryTimeout:
      return constants.JOB_NOTCHANGED


class CouchDBJobQueue(_BaseJobQueue):
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
    super(CouchDBJobQueue, self).__init__(context)
    # Get the db connections
    self._hostip = netutils.Hostname.GetIP(self._my_hostname)
    self._queue_db = utils.GetDBInstance(constants.QUEUE_DB, self._hostip,
                                         constants.DEFAULT_COUCHDB_PORT)
    self._archive = utils.GetDBInstance(constants.ARCHIVE_DB, self._hostip,
                                        constants.DEFAULT_COUCHDB_PORT)

    # Initialize the queue, and acquire the filelock.
    # This ensures no other process is working on the job queue.
    self._jstore = jstore.GetJStore("couchdb", queue=self._queue_db,
                                    archive=self._archive)
    self._queue_filelock = self._jstore.InitAndVerifyQueue(must_lock=True)

    # Read serial file
    (self._last_serial, self._serial_doc_rev) = self._jstore.ReadSerial()
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

    self._drained = self._jstore.CheckDrainFlag()
    self._drain_rev = self._jstore.ReadDrain()

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

    all_jobs = self._GetJobsUnlocked(archived=False)
    jobs_count = len(all_jobs)
    lastinfo = time.time()
    for idx, jdoc in enumerate(all_jobs):
      # Give an update every 1000 jobs or 10 seconds
      if (idx % 1000 == 0 or time.time() >= (lastinfo + 10.0) or
          idx == (jobs_count - 1)):
        logging.info("Job queue inspection: %d/%d (%0.1f %%)",
                     idx, jobs_count - 1, 100.0 * (idx + 1) / jobs_count)
        lastinfo = time.time()

      try:
        data = json.loads(jdoc["info"])
        job = _QueuedJob.Restore(self, data, True, None)
      # a failure in loading the job can cause an exception here.
      except Exception:
        continue

      status = job.CalcStatus()

      if status == constants.JOB_STATUS_QUEUED:
        restartjobs.append(job)

      elif status in (constants.JOB_STATUS_RUNNING,
                      constants.JOB_STATUS_WAITING,
                      constants.JOB_STATUS_CANCELING):
        logging.warning("Unfinished job %s, %s found: %s", job.id, job.rev, job)

        if status == constants.JOB_STATUS_WAITING:
          # Restart job.
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

    if not node.master_candidate:
      # remove if existing, ignoring errors
      self._nodes.pop(node_name, None)
      # and skip the replication of the job ids
      return
    else:
      queue = "".join(("/", constants.QUEUE_DB, "/"))
      arch = "".join(("/", constants.ARCHIVE_DB, "/"))
      utils.UnlockedReplicateSetup(self._hostip, node.primary_ip, queue, False)
      utils.UnlockedReplicateSetup(self._hostip, node.primary_ip, arch, False)

    self._nodes[node_name] = node.primary_ip

  @locking.ssynchronized(_LOCK)
  @_RequireOpenQueue
  def RemoveNode(self, node):
    """Callback called when removing nodes from the cluster.

    @type node: L{objects.Node}
    @param node: the node object to be added

    """
    self._nodes.pop(node.name, None)

    if node.master_candidate:
      queue = "".join(("/", constants.QUEUE_DB, "/"))
      archive = "".join(("/", constants.ARCHIVE_DB, "/"))
      utils.UnlockedReplicateSetup(self._hostip, node.primary_ip, queue, True)
      utils.UnlockedReplicateSetup(self._hostip, node.primary_ip, archive, True)

  def _UpdateJobQueueFile(self, data, job):
    """Writes a file in local db and then auto replicate it to all nodes.

    This function will replace the contents of a file on the local node
    and then couchdb server will automatic replicate it to all the other
    candidate nodes of the cluster.

    @type data: str
    @param data: the new contents of the file
    @type job: L{_QueuedJob}
    @param job: the job to be updated

    """
    doc = {"_id": str(job.id), "info": data}
    if job.rev:
      doc["_rev"] = job.rev

    job.rev = utils.WriteDocument(self._queue_db, doc)

  def _RenameFilesUnlocked(self, arch_jobs, del_jobs):
    """Rename a job list from queue to archive db.

    This function will bulk delete the del_jobs given from jqueue db and
    then will bulk update arch_jobs to the archive db

    @type arch_jobs: list of job documents
    @param arch_jobs: List containing jobs to be archived
    @type del_jobs: list of job documents
    @param del_jobs: List containing jobs to be deleted

    """
    # FIXME: Currently we do not make any check to the output of the
    # BulkUpdateDocs function.
    try:
      utils.BulkUpdateDocs(self._queue_db, del_jobs)
      utils.BulkUpdateDocs(self._archive, arch_jobs)
    except Exception, err:
      raise errors.JobQueueError("Error while renaming a job file from queue to"
                                 " archive db: ", err)

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

    # Write to database
    data = {"_id": "serial", "_rev": self._serial_doc_rev, "value": serial}
    self._serial_doc_rev = utils.WriteDocument(self._queue_db, data)

    result = [jstore.FormatJobID(v)
              for v in range(self._last_serial + 1, serial + 1)]

    # Keep it only if we were able to write the file
    self._last_serial = serial

    assert len(result) == count

    return result

  def _GetJobIDsUnlocked(self, archived=False):
    """Return all known job IDs.

    The method only looks at the queue database because it's a requirement
    that all jobs are present on the database (so in the _memcache we don't
    have any extra IDs).

    @rtype: list of L{couchdb.client.Document}
    @return: the list of job IDs

    """
    job_ids = []

    if archived:
      arch_view_res = utils.ViewExec(self._archive, "queue_view/jobs")
      for row in arch_view_res.rows:
        job_ids.append(int(row["id"]))

    view_res = utils.ViewExec(self._queue_db, "queue_view/jobs")
    for row in view_res.rows:
      job_ids.append(int(row["id"]))

    return job_ids

  def _GetJobsUnlocked(self, archived=False):
    """Return all known jobs.

    This method is the same as _GetJobIDsUnlocked but returns a list
    with all job docs from the db.

    @rtype: list of L{couchdb.client.Document}
    @return: the list of job documents

    """
    job_ids = []

    if archived:
      arch_view_res = \
          utils.ViewExec(self._archive, "queue_view/jobs", include_docs=True)
      for row in arch_view_res.rows:
        job_ids.append(row["doc"])

    view_res = \
        utils.ViewExec(self._queue_db, "queue_view/jobs", include_docs=True)
    for row in view_res.rows:
      job_ids.append(row["doc"])

    return job_ids

  def _LoadJobUnlocked(self, job_id):
    """Loads a job from the database or memory.

    Given a job id, this will return the cached job object if
    existing, or try to load the job from the db. If loading from
    db, it will also add the job to the cache.

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
      new_job = self._LoadJobFromDisk(job_id, True)
      if new_job is None:
        # job already archived (future case)
        logging.exception("Can't parse job %s", job_id)
      else:
        # non-archived case
        logging.exception("Can't parse job %s, will archive.", job_id)
        old_job = copy.deepcopy(job)
        old_job["_deleted"] = True
        self._RenameFilesUnlocked([job], [old_job])
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
    db_names = [self._queue_db]

    if try_archived:
      db_names.append(self._archive)

    raw_data = None
    archived = None

    for db in db_names:
      try:
        doc = utils.GetDocument(db, str(job_id))
        raw_data = doc["info"]
      except TypeError, err:
        msg = ("The job with id '%s', haven't found on db '%s'. %s" %
               (job_id, self._queue_db, errors.ECODE_NOENT))
        raise errors.OpPrereqError(msg)
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

  def _UpdateQueueSizeUnlocked(self):
    """Update the queue size.

    """
    # The queue size is the length of the queue database minus the
    # serial, version, filter and queue_view documents.
    self._queue_size = len(self._queue_db) - 4

  @locking.ssynchronized(_LOCK)
  @_RequireOpenQueue
  def SetDrainFlag(self, drain_flag):
    """Sets the drain flag for the queue.

    @type drain_flag: boolean
    @param drain_flag: Whether to set or unset the drain flag

    """
    self._drain_rev = \
      self._jstore.SetDrainFlag(drain_flag, self._drain_rev)

    self._drained = drain_flag

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

    data = serializer.DumpJson(job.Serialize())
    logging.debug("Writing job %s, %s to %s", job.id, job.rev, self._queue_db)
    self._UpdateJobQueueFile(data, job)

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
    job_obj = self.SafeLoadJobFromDisk(job_id, True, writable=False)

    helper = _CouchDBWaitForJobChangesHelper()

    return helper(fields, prev_job_info, prev_log_serial, timeout,
                  self._queue_db, job_obj)

  @_RequireOpenQueue
  def _ArchiveJobsUnlocked(self, job_list):
    """Archives jobs.

    @type job_list: list of L{_QueuedJob}
    @param job_list: Job objects
    @rtype: int
    @return: Number of archived jobs

    """
    archive_jobs = []
    deleted_jobs = []
    for arch_job, del_job in job_list:
      # This function is called from 'ArchiveJob' and 'AutoArchiveJobs'
      # only. The 'writable' and 'archived' fields can only be True and None
      # repsectively due to the call to the '_LoadJobUnlocked(job_id)' method
      # that produces those values.
      data = json.loads(arch_job["info"])
      job = _QueuedJob.Restore(self, data, True, None)
      # The assert checks will always be true due to the call made above.
      assert job.writable, "Can't archive read-only job"
      assert not job.archived, "Can't cancel archived job"

      if job.CalcStatus() not in constants.JOBS_FINALIZED:
        logging.debug("Job %s is not yet done", job.id)
        continue

      arch_job["archive_index"] = jstore.GetArchiveDirectory(arch_job["_id"])
      del_job["_deleted"] = True
      archive_jobs.append(arch_job)
      deleted_jobs.append(del_job)

    # TODO: What if 1..n files fail to rename?
    self._RenameFilesUnlocked(archive_jobs, deleted_jobs)

    logging.debug("Successfully archived job(s) %s",
                  utils.CommaJoin(job["_id"] for job in archive_jobs))

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

    job = utils.GetDocument(self._queue_db, str(job_id))
    if not job:
      logging.debug("Job %s not found", job_id)
      return False

    dict_job = dict(job)
    dict_job2 = copy.deepcopy(dict_job)
    return self._ArchiveJobsUnlocked([(dict_job, dict_job2)]) == 1

  @locking.ssynchronized(_LOCK)
  @_RequireOpenQueue
  def AutoArchiveJobs(self, age, timeout):
    """Archives all jobs based on age.

    The method will archive all jobs which are older than the age
    parameter. For jobs that don't have an end timestamp, the start
    timestamp will be considered. The special "-1" age will cause
    archival of all jobs (that are not running or queued).

    @type age: int
    @param age: the minimum age in seconds

    """
    logging.info("Archiving jobs with age more than %s seconds", age)

    now = time.time()
    end_time = now + timeout
    archived_count = 0
    last_touched = 0

    all_jobs = self._GetJobsUnlocked()
    pending = []
    for idx, jdoc in enumerate(all_jobs):
      last_touched = idx + 1

      # Not optimal because jobs could be pending
      # TODO: Measure average duration for job archival and take number of
      # pending jobs into account.
      if time.time() > end_time:
        break

      # Returns None if the job failed to load
      data = json.loads(jdoc["info"])
      job = _QueuedJob.Restore(self, data, True, None)
      if job:
        if job.end_timestamp is None:
          if job.start_timestamp is None:
            job_age = job.received_timestamp
          else:
            job_age = job.start_timestamp
        else:
          job_age = job.end_timestamp

        if age == -1 or now - job_age[0] > age:
          jdoc2 = copy.deepcopy(jdoc)
          pending.append((jdoc, jdoc2))

          # Archive 10 jobs at a time
          if len(pending) >= 10:
            archived_count += self._ArchiveJobsUnlocked(pending)
            pending = []

    if pending:
      archived_count += self._ArchiveJobsUnlocked(pending)

    return (archived_count, len(all_jobs) - last_touched)
