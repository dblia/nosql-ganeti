#
#

# Copyright (C) 2006, 2007, 2008 Google Inc.
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


"""Job queue management abstraction package.

"""

from ganeti import errors
from ganeti import constants

from ganeti.jqueue import default, couch

# Lookup table for configuration storage types
_JOB_QUEUE_TYPES = {
  constants.JQ_DISK: default.DiskJobQueue,
  constants.JQ_COUCHDB: couch.CouchDBJobQueue
  }


def GetJobQueueClass(name):
  """Returns the class for a job queue storage type.

  @type name: string
  @param name: Job queue storage type

  """
  try:
    return _JOB_QUEUE_TYPES[name]
  except KeyError:
    msg = "Unknown job queue storage type: %r" % name
    raise errors.JobQueueError(msg)


def GetJobQueue(name, **kargs):
  """Factory method for job queue storage engines.

  @type name: string
  @param name: Job queue storage engine

  """
  return GetJobQueueClass(name)(**kargs)
