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


"""Configuration managment abstraction module

The configuration data is stored on every node but is updated on the master
only. After each update, the master distributes the data to the other nodes.

Currently, the data storage format is JSON. YAML was slow and consuming too
much memory.

"""

from ganeti import errors
from ganeti import constants
from ganeti.config import default


def GetConfigWriterClass(name):
  """Returns the class for a configuration data storage type

  @type name: string
  @param name: Configuration storage type

  """
  try:
    assert name == "disk"
    return default.DiskConfigWriter
  except KeyError:
    msg = "Unknown configuration storage type: %r" % name
    raise errors.ConfigurationError(msg)


def GetConfigWriter(name, **kargs):
  """Factory function for configuration storage methods.

  @type name: string
  @param name: Configuration storage type

  """
  return GetConfigWriterClass(name)(**kargs)
