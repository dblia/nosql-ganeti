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


"""Configuration management abstraction package.

The configuration data is stored on every node but is updated on the master
only. After each update, the master distributes the data to the other nodes.

Currently, the data storage format is JSON. YAML was slow and consuming too
much memory.

"""

from ganeti import errors
from ganeti import constants

from ganeti.config import default, couch

# Lookup table for configuration storage types
_CONFIG_DATA_TYPES = {
  constants.CFG_DISK: default.DiskConfigWriter,
  constants.CFG_COUCHDB: couch.CouchDBConfigWriter
  }


def GetConfigWriterClass(name):
  """Returns the class for a configuration data storage type.

  @type name: string
  @param name: Configuration storage type

  """
  try:
    return _CONFIG_DATA_TYPES[name]
  except KeyError:
    msg = "Unknown configuration storage type: %r" % name
    raise errors.ConfigurationError(msg)


def GetConfigWriter(name, **kargs):
  """Factory method for configuration storage engines.

  @type name: string
  @param name: Configuration storage type

  """
  return GetConfigWriterClass(name)(**kargs)
