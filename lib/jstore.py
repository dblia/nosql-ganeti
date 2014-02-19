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


"""Abstraction module implementing the job queue handling.

"""

# pylint: disable=W0221
# W0221: Arguments number differs from overridden method

# pylint: disable=W0232
# W0232: Class has no __init__ method

import errno
import os
import copy

from ganeti import constants
from ganeti import errors
from ganeti import runtime
from ganeti import utils
from ganeti import pathutils


JOBS_PER_ARCHIVE_DIRECTORY = 10000


class _Base:
  """Base class for job queue handling abstraction

  """
  def _ReadNumericFile(self, doc_name):
    """Reads a file containing a number.

    @rtype: None or int
    @return: None if file is not found, otherwise number

    """
    raise NotImplementedError()

  def ReadSerial(self):
    """Read the serial file.

    The queue should be locked while this function is called.

    """
    raise NotImplementedError()

  def ReadVersion(self):
    """Read the queue version.

    The queue should be locked while this function is called.

    """
    raise NotImplementedError()

  def InitAndVerifyQueue(self, must_lock):
    """Open and lock job queue.

    If necessary, the queue is automatically initialized.

    @type must_lock: bool
    @param must_lock: Whether an exclusive lock must be held.
    @rtype: utils.FileLock
    @return: Lock object for the queue. This can be used to change the
             locking mode.

    """
    raise NotImplementedError()

  def CheckDrainFlag(self):
    """Check if the queue is marked to be drained.

    This currently uses the queue drain file, which makes it a per-node flag.
    In the future this can be moved to the config file.

    @rtype: boolean
    @return: True if the job queue is marked drained

    """
    raise NotImplementedError()

  def SetDrainFlag(self, drain_flag, *args):
    """Sets the drain flag for the queue.

    @type drain_flag: boolean
    @param drain_flag: Whether to set or unset the drain flag
    @attention: This function should only called the current holder of the queue
      lock

    """
    raise NotImplementedError()


class FileStorage(_Base):
  """Disk queue handling unit.

  """
  def _ReadNumericFile(self, doc_name):
    """Reads a file containing a number.

    See L{_Base._ReadNumericFile}

    """
    try:
      contents = utils.ReadFile(doc_name)
    except EnvironmentError, err:
      if err.errno in (errno.ENOENT, ):
        return None
      raise

    try:
      return int(contents)
    except (ValueError, TypeError), err:
      # Couldn't convert to int
      raise errors.JobQueueError("Content of file '%s' is not numeric: %s" %
                                 (doc_name, err))

  def ReadSerial(self):
    """Read the serial file.

    The queue should be locked while this function is called.

    """
    return self._ReadNumericFile(pathutils.JOB_QUEUE_SERIAL_FILE)

  def ReadVersion(self):
    """Read the queue version.

    The queue should be locked while this function is called.

    """
    return self._ReadNumericFile(pathutils.JOB_QUEUE_VERSION_FILE)

  def InitAndVerifyQueue(self, must_lock):
    """Open and lock job queue.

    If necessary, the queue is automatically initialized.

    See L{InitAndVerifyQueue}

    """
    getents = runtime.GetEnts()

    # Lock queue
    queue_lock = utils.FileLock.Open(pathutils.JOB_QUEUE_LOCK_FILE)
    try:
      # The queue needs to be locked in exclusive mode to write to the serial
      # and version files.
      if must_lock:
        queue_lock.Exclusive(blocking=True)
        holding_lock = True
      else:
        try:
          queue_lock.Exclusive(blocking=False)
          holding_lock = True
        except errors.LockError:
          # Ignore errors and assume the process keeping the lock checked
          # everything.
          holding_lock = False

      if holding_lock:
        # Verify version
        version = self.ReadVersion()
        if version is None:
          # Write new version file
          utils.WriteFile(pathutils.JOB_QUEUE_VERSION_FILE,
                          uid=getents.masterd_uid, gid=getents.daemons_gid,
                          mode=constants.JOB_QUEUE_FILES_PERMS,
                          data="%s\n" % constants.JOB_QUEUE_VERSION)

          # Read again
          version = self.ReadVersion()

        if version != constants.JOB_QUEUE_VERSION:
          raise errors.JobQueueError("Found job queue version %s, expected %s",
                                     version, constants.JOB_QUEUE_VERSION)

        serial = self.ReadSerial()
        if serial is None:
          # Write new serial file
          utils.WriteFile(pathutils.JOB_QUEUE_SERIAL_FILE,
                          uid=getents.masterd_uid, gid=getents.daemons_gid,
                          mode=constants.JOB_QUEUE_FILES_PERMS,
                          data="%s\n" % 0)

          # Read again
          serial = self.ReadSerial()

        if serial is None:
          # There must be a serious problem
          raise errors.JobQueueError("Can't read/parse the job queue"
                                     " serial file")

        if not must_lock:
          # There's no need for more error handling. Closing the lock
          # file below in case of an error will unlock it anyway.
          queue_lock.Unlock()

    except:
      queue_lock.Close()
      raise

    return queue_lock

  def CheckDrainFlag(self):
    """Check if the queue is marked to be drained.

    This currently uses the queue drain file, which makes it a per-node flag.
    In the future this can be moved to the config file.

    See L{_Base.CheckDrainFlag}

    """
    return os.path.exists(pathutils.JOB_QUEUE_DRAIN_FILE)

  def SetDrainFlag(self, drain_flag):
    """Sets the drain flag for the queue.

    See L{_Base.SetDrainFlag}

    """
    getents = runtime.GetEnts()

    if drain_flag:
      utils.WriteFile(pathutils.JOB_QUEUE_DRAIN_FILE, data="",
                      uid=getents.masterd_uid, gid=getents.daemons_gid,
                      mode=constants.JOB_QUEUE_FILES_PERMS)
    else:
      utils.RemoveFile(pathutils.JOB_QUEUE_DRAIN_FILE)

    assert (not drain_flag) ^ self.CheckDrainFlag()


class CouchDBStorage(_Base):
  """CouchDB queue storage type.

  """
  def __init__(self, queue, archive):
    """Initializes this class

    @type queue: L{couchdb.client.Database} object
    @param queue: queue database instance

    """
    self._queue = queue
    self._archive = archive

  def _ReadNumericFile(self, doc_name):
    """Reads a document containing a number.

    See L{_Base._ReadNumericFile}

    """
    contents = utils.GetDocument(self._queue, doc_name)
    if not contents:
      # File haven't found
      return (None, None)

    try:
      return (int(contents["value"]), contents["_rev"])
    except (ValueError, TypeError), err:
      # Couldn't convert to int
      raise errors.JobQueueError("Content of file '%s' is not numeric: %s" %
                                 (doc_name, err))

  def ReadSerial(self):
    """Read the serial file.

    The queue should be locked while this function is called.

    """
    return self._ReadNumericFile("serial")

  def ReadVersion(self):
    """Read the queue version.

    The queue should be locked while this function is called.

    """
    return self._ReadNumericFile("version")

  def ReadDrain(self):
    """Read the serial file.

    The queue should be locked while this function is called.

    """
    try:
      return utils.GetDocument(self._queue, "drain")["_rev"]
    except TypeError:
      return None

  def ReadQueueView(self):
    """Read the view file.

    The queue should be locked while this function is called.

    """
    try:
      doc = utils.GetDocument(self._queue, "_design/queue_view")
      return (doc["views"], doc["_rev"])
    except TypeError:
      return (None, None)

  def ReadQueueFilter(self):
    """Read the view file.

    The queue should be locked while this function is called.

    """
    try:
      doc = utils.GetDocument(self._queue, "_design/filter")
      return (doc["filters"], doc["_rev"])
    except TypeError:
      return (None, None)

  def ReadArchiveView(self):
    """Read the view file.

    The queue should be locked while this function is called.

    """
    try:
      doc = utils.GetDocument(self._queue, "_design/queue_view")
      return (doc["views"], doc["_rev"])
    except TypeError:
      return (None, None)

  def ReadArchiveFilter(self):
    """Read the view file.

    The queue should be locked while this function is called.

    """
    try:
      doc = utils.GetDocument(self._queue, "_design/filter")
      return (doc["filters"], doc["_rev"])
    except TypeError:
      return (None, None)

  def InitAndVerifyQueue(self, must_lock):
    """Open and lock job queue.

    If necessary, the queue is automatically initialized.

    See L{_Base.InitAndVerifyQueue}

    """
    # Lock queue
    # my-TODO: lock path will change in the future
    queue_lock = utils.FileLock.Open(pathutils.JOB_QUEUE_LOCK_FILE)
    try:
      # The queue needs to be locked in exclusive mode to write to the serial
      # and version files.
      if must_lock:
        queue_lock.Exclusive(blocking=True)
        holding_lock = True
      else:
        try:
          queue_lock.Exclusive(blocking=False)
          holding_lock = True
        except errors.LockError:
          # Ignore errors and assume the process keeping the lock checked
          # everything.
          holding_lock = False

      if holding_lock:
        # Verify version
        (version, version_rev) = self.ReadVersion()
        if version is None:
          # Write new version doc to database
          data = {"_id": "version", "value": constants.JOB_QUEUE_VERSION}
          version_rev = utils.WriteDocument(self._queue, data)

          # Read again
          (version, vers_rev) = self.ReadVersion()

          assert version_rev == vers_rev, "Different revisions in version file"

        if version != constants.JOB_QUEUE_VERSION:
          raise errors.JobQueueError("Found job queue version %s, expected %s",
                                     version, constants.JOB_QUEUE_VERSION)

        # Verify serial
        (serial, serial_rev) = self.ReadSerial()
        if serial is None:
          # Write new serial doc to database
          data = {"_id": "serial", "value": 0}
          serial_rev = utils.WriteDocument(self._queue, data)

          # Read again
          (serial, ser_rev) = self.ReadSerial()

          assert serial_rev == ser_rev, "Different revisions in serial file"

        if serial is None:
          # There must be a serious problem
          raise errors.JobQueueError("Can't read/parse the job queue"
                                     " serial file")

        # Verify queue view document
        (view, view_rev) = self.ReadQueueView()
        if view is None:
          # Write new view doc to database
          data = copy.deepcopy(constants.QUEUE_VIEW)
          view_rev = utils.WriteDocument(self._queue, data)

          # Read again
          (view, v_rev) = self.ReadQueueView()

          assert view_rev == v_rev, "Different revisions in view document"

        if view is None:
          # There must be a serious problem
          raise errors.JobQueueError("Can't read/parse the job queue"
                                     " view document")
        # Verify queue filter document
        (filt, filt_rev) = self.ReadQueueFilter()
        if filt is None:
          # Write new view doc to database
          data = copy.deepcopy(constants.QUEUE_FILTER)
          filt_rev = utils.WriteDocument(self._queue, data)

          # Read again
          (filt, f_rev) = self.ReadQueueFilter()

          assert filt_rev == f_rev, "Different revisions in filter document"

        if filt is None:
          # There must be a serious problem
          raise errors.JobQueueError("Can't read/parse the job queue"
                                     " filter document")

        # Verify archive view document
        (view, view_rev) = self.ReadArchiveView()
        if view is None:
          # Write new view doc to database
          data = copy.deepcopy(constants.QUEUE_VIEW)
          view_rev = utils.WriteDocument(self._archive, data)

          # Read again
          (view, v_rev) = self.ReadArchiveView()

          assert view_rev == v_rev, "Different revisions in view document"

        if view is None:
          # There must be a serious problem
          raise errors.JobQueueError("Can't read/parse the archive job"
                                     " view document")
        # Verify archive filter document
        (filt, filt_rev) = self.ReadArchiveFilter()
        if filt is None:
          # Write new view doc to database
          data = copy.deepcopy(constants.QUEUE_FILTER)
          filt_rev = utils.WriteDocument(self._archive, data)

          # Read again
          (filt, f_rev) = self.ReadArchiveFilter()

          assert filt_rev == f_rev, "Different revisions in filter document"

        if filt is None:
          # There must be a serious problem
          raise errors.JobQueueError("Can't read/parse the archive job"
                                     " filter document")

        if not must_lock:
          # There's no need for more error handling. Closing the lock
          # file below in case of an error will unlock it anyway.
          queue_lock.Unlock()
    except:
      queue_lock.Close()
      raise

    return queue_lock

  def CheckDrainFlag(self):
    """Check if the queue is marked to be drained.

    This currently uses the queue drain file, which makes it a per-node flag.
    In the future this can be moved to the config file.

    See L{_Base.CheckDrainFlag}

    """
    if utils.GetDocument(self._queue, "drain"):
      return True
    else:
      return False

  def SetDrainFlag(self, drain_flag, drain_rev):
    """Sets the drain flag for the queue.

    See L{_Base.SetDrainFlag}

    """
    if drain_rev:
      data = {"_id": "drain", "_rev": drain_rev}
    else:
      data = {"_id": "drain"}

    if drain_flag:
      return utils.WriteDocument(self._queue, data)
    else:
      data["_deleted"] = True
      return utils.WriteDocument(self._queue, data)

    assert (not drain_flag) ^ self.CheckDrainFlag()


def FormatJobID(job_id):
  """Convert a job ID to int format.

  Currently this just is a no-op that performs some checks, but if we
  want to change the job id format this will abstract this change.

  @type job_id: int or long
  @param job_id: the numeric job id
  @rtype: int
  @return: the formatted job id

  """
  if not isinstance(job_id, (int, long)):
    raise errors.ProgrammerError("Job ID '%s' not numeric" % job_id)
  if job_id < 0:
    raise errors.ProgrammerError("Job ID %s is negative" % job_id)

  return job_id


def GetArchiveDirectory(job_id):
  """Returns the archive index for a job.

  @type job_id: str
  @param job_id: Job identifier
  @rtype: str
  @return: Direcotry name

  """
  return str(ParseJobId(job_id) / JOBS_PER_ARCHIVE_DIRECTORY)


def ParseJobId(job_id):
  """Parses a job ID and converts it to integer.

  """
  try:
    return int(job_id)
  except (ValueError, TypeError):
    raise errors.ParameterError("Invalid job ID '%s'" % job_id)


# Lookup table for configuration storage types
_JSTORAGE_TYPES = {
  constants.JQ_DISK: FileStorage,
  constants.JQ_COUCHDB: CouchDBStorage
  }


def GetJStoreClass(name):
  """Returns the class for a job queue storage type

  @type name: string
  @param name: Job queue storage type

  """
  try:
    return _JSTORAGE_TYPES[name]
  except KeyError:
    msg = "Unknown jstore type: %r" % name
    raise errors.JobQueueError(msg)


def GetJStore(name, **kargs):
  """Factory function for jstore methods.

  @type name: string
  @param name: Job queue storage type

  """
  return GetJStoreClass(name)(**kargs)
