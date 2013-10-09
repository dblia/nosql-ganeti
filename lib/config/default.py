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


"""Default ganeti disk driver for configuration managment.

"""

# pylint: disable=R0904
# R0904: Too many public methods

# pylint: disable=W0212
# W0212: Access to a protected member %s of a client class

# pylint: disable=W0221
# W0221: Arguments number differs from overridden method

import copy
import os
import random
import logging
import time
import itertools

from ganeti import errors
from ganeti import locking
from ganeti import utils
from ganeti import constants
from ganeti import objects
from ganeti import serializer
from ganeti import runtime
from ganeti import pathutils

from ganeti.config import base


# Functions and contants needed from base file
_config_lock = base._config_lock
_UPGRADE_CONFIG_JID = base._UPGRADE_CONFIG_JID
_ValidateConfig = base._ValidateConfig


class DiskConfigWriter(base._BaseConfigWriter):
  """Disk storage configuration type

  """
  def __init__(self, cfg_file=None, offline=False, _getents=runtime.GetEnts,
               accept_foreign=False):
    super(DiskConfigWriter, self).__init__()
    if cfg_file is None:
      self._cfg_file = pathutils.CLUSTER_CONF_FILE
    else:
      self._cfg_file = cfg_file
    self._offline = offline
    self._getents = _getents
    self._cfg_id = None
    self._OpenConfig(accept_foreign)

  # this method needs to be static, so that we can call it on the class
  @staticmethod
  def IsCluster():
    """Check if the cluster is configured.

    """
    return os.path.exists(pathutils.CLUSTER_CONF_FILE)

  @locking.ssynchronized(_config_lock)
  def AllocatePort(self):
    """Allocate a port.

    The port will be taken from the available port pool or from the
    default port range (and in this case we increase
    highest_used_port).

    """
    # If there are TCP/IP ports configured, we use them first.
    if self._config_data.cluster.tcpudp_port_pool:
      port = self._config_data.cluster.tcpudp_port_pool.pop()
    else:
      port = self._config_data.cluster.highest_used_port + 1
      if port >= constants.LAST_DRBD_PORT:
        raise errors.ConfigurationError("The highest used port is greater"
                                        " than %s. Aborting." %
                                        constants.LAST_DRBD_PORT)
      self._config_data.cluster.highest_used_port = port

    self._WriteConfig()
    return port

  @locking.ssynchronized(_config_lock)
  def AddNodeGroup(self, group, ec_id, check_uuid=True):
    """Add a node group to the configuration.

    This method calls group.UpgradeConfig() to fill any missing attributes
    according to their default values.

    See L{_BaseConfigWriter.AddNodeGroup}

    """
    self._UnlockedAddNodeGroup(group, ec_id, check_uuid)
    self._WriteConfig()

  def _UnlockedAddNodeGroup(self, group, ec_id, check_uuid):
    """Add a node group to the configuration.

    """
    logging.info("Adding node group %s to configuration", group.name)

    # Some code might need to add a node group with a pre-populated UUID
    # generated with ConfigWriter.GenerateUniqueID(). We allow them to bypass
    # the "does this UUID" exist already check.
    if check_uuid:
      self._EnsureUUID(group, ec_id)

    try:
      existing_uuid = self._UnlockedLookupNodeGroup(group.name)
    except errors.OpPrereqError:
      pass
    else:
      raise errors.OpPrereqError("Desired group name '%s' already exists as a"
                                 " node group (UUID: %s)" %
                                 (group.name, existing_uuid),
                                 errors.ECODE_EXISTS)

    group.serial_no = 1
    group.ctime = group.mtime = time.time()
    group.UpgradeConfig()

    self._config_data.nodegroups[group.uuid] = group
    self._config_data.cluster.serial_no += 1

  @locking.ssynchronized(_config_lock)
  def RemoveNodeGroup(self, group_uuid):
    """Remove a node group from the configuration.

    See L{_BaseConfigWriter.RemoveNodeGroup}

    """
    logging.info("Removing node group %s from configuration", group_uuid)

    if group_uuid not in self._config_data.nodegroups:
      raise errors.ConfigurationError("Unknown node group '%s'" % group_uuid)

    assert len(self._config_data.nodegroups) != 1, \
            "Group '%s' is the only group, cannot be removed" % group_uuid

    del self._config_data.nodegroups[group_uuid]
    self._config_data.cluster.serial_no += 1
    self._WriteConfig()

  @locking.ssynchronized(_config_lock)
  def AddInstance(self, instance, ec_id):
    """Add an instance to the config.

    This should be used after creating a new instance.

    See L{_BaseConfigWriter.AddInstance}

    """
    if not isinstance(instance, objects.Instance):
      raise errors.ProgrammerError("Invalid type passed to AddInstance")

    if instance.disk_template != constants.DT_DISKLESS:
      all_lvs = instance.MapLVsByNode()
      logging.info("Instance '%s' DISK_LAYOUT: %s", instance.name, all_lvs)

    all_macs = self._AllMACs()
    for nic in instance.nics:
      if nic.mac in all_macs:
        raise errors.ConfigurationError("Cannot add instance %s:"
                                        " MAC address '%s' already in use." %
                                        (instance.name, nic.mac))

    self._EnsureUUID(instance, ec_id)

    instance.serial_no = 1
    instance.ctime = instance.mtime = time.time()
    self._config_data.instances[instance.name] = instance
    self._config_data.cluster.serial_no += 1
    self._UnlockedReleaseDRBDMinors(instance.name)
    self._UnlockedCommitTemporaryIps(ec_id)
    self._WriteConfig()

  def _SetInstanceStatus(self, instance_name, status):
    """Set the instance's status to a given value.

    """
    assert status in constants.ADMINST_ALL, \
           "Invalid status '%s' passed to SetInstanceStatus" % (status,)

    if instance_name not in self._config_data.instances:
      raise errors.ConfigurationError("Unknown instance '%s'" %
                                      instance_name)
    instance = self._config_data.instances[instance_name]
    if instance.admin_state != status:
      instance.admin_state = status
      instance.serial_no += 1
      instance.mtime = time.time()
      self._WriteConfig()

  @locking.ssynchronized(_config_lock)
  def RemoveInstance(self, instance_name):
    """Remove the instance from the configuration.

    """
    if instance_name not in self._config_data.instances:
      raise errors.ConfigurationError("Unknown instance '%s'" % instance_name)

    # If a network port has been allocated to the instance,
    # return it to the pool of free ports.
    inst = self._config_data.instances[instance_name]
    network_port = getattr(inst, "network_port", None)
    if network_port is not None:
      self._config_data.cluster.tcpudp_port_pool.add(network_port)

    instance = self._UnlockedGetInstanceInfo(instance_name)

    for nic in instance.nics:
      if nic.network and nic.ip:
        # Return all IP addresses to the respective address pools
        self._UnlockedCommitIp(constants.RELEASE_ACTION, nic.network, nic.ip)

    del self._config_data.instances[instance_name]
    self._config_data.cluster.serial_no += 1
    self._WriteConfig()

  @locking.ssynchronized(_config_lock)
  def RenameInstance(self, old_name, new_name):
    """Rename an instance.

    This needs to be done in ConfigWriter and not by RemoveInstance
    combined with AddInstance as only we can guarantee an atomic
    rename.

    """
    if old_name not in self._config_data.instances:
      raise errors.ConfigurationError("Unknown instance '%s'" % old_name)

    # Operate on a copy to not loose instance object in case of a failure
    inst = self._config_data.instances[old_name].Copy()
    inst.name = new_name

    for (idx, disk) in enumerate(inst.disks):
      if disk.dev_type == constants.LD_FILE:
        # rename the file paths in logical and physical id
        file_storage_dir = os.path.dirname(os.path.dirname(disk.logical_id[1]))
        disk.logical_id = (disk.logical_id[0],
                           utils.PathJoin(file_storage_dir, inst.name,
                                          "disk%s" % idx))
        disk.physical_id = disk.logical_id

    # Actually replace instance object
    del self._config_data.instances[old_name]
    self._config_data.instances[inst.name] = inst

    # Force update of ssconf files
    self._config_data.cluster.serial_no += 1

    self._WriteConfig()

  @locking.ssynchronized(_config_lock)
  def AddNode(self, node, ec_id):
    """Add a node to the configuration.

    See L{_BaseConfigWriter.AddNode}

    """
    logging.info("Adding node %s to configuration", node.name)

    self._EnsureUUID(node, ec_id)

    node.serial_no = 1
    node.ctime = node.mtime = time.time()
    self._UnlockedAddNodeToGroup(node.name, node.group)
    self._config_data.nodes[node.name] = node
    self._config_data.cluster.serial_no += 1
    self._WriteConfig()

  @locking.ssynchronized(_config_lock)
  def RemoveNode(self, node_name):
    """Remove a node from the configuration.

    """
    logging.info("Removing node %s from configuration", node_name)

    if node_name not in self._config_data.nodes:
      raise errors.ConfigurationError("Unknown node '%s'" % node_name)

    self._UnlockedRemoveNodeFromGroup(self._config_data.nodes[node_name])
    del self._config_data.nodes[node_name]
    self._config_data.cluster.serial_no += 1
    self._WriteConfig()

  @locking.ssynchronized(_config_lock)
  def MaintainCandidatePool(self, exceptions):
    """Try to grow the candidate pool to the desired size.

    See L{_BaseConfigWriter.MaintainCandidatePool}

    """
    mc_now, mc_max, _ = self._UnlockedGetMasterCandidateStats(exceptions)
    mod_list = []
    if mc_now < mc_max:
      node_list = self._config_data.nodes.keys()
      random.shuffle(node_list)
      for name in node_list:
        if mc_now >= mc_max:
          break
        node = self._config_data.nodes[name]
        if (node.master_candidate or node.offline or node.drained or
            node.name in exceptions or not node.master_capable):
          continue
        mod_list.append(node)
        node.master_candidate = True
        node.serial_no += 1
        mc_now += 1
      if mc_now != mc_max:
        # this should not happen
        logging.warning("Warning: MaintainCandidatePool didn't manage to"
                        " fill the candidate pool (%d/%d)", mc_now, mc_max)
      if mod_list:
        self._config_data.cluster.serial_no += 1
        self._WriteConfig()

    return mod_list

  def _UnlockedAddNodeToGroup(self, node_name, nodegroup_uuid):
    """Add a given node to the specified group.

    """
    if nodegroup_uuid not in self._config_data.nodegroups:
      # This can happen if a node group gets deleted between its lookup and
      # when we're adding the first node to it, since we don't keep a lock in
      # the meantime. It's ok though, as we'll fail cleanly if the node group
      # is not found anymore.
      raise errors.OpExecError("Unknown node group: %s" % nodegroup_uuid)
    if node_name not in self._config_data.nodegroups[nodegroup_uuid].members:
      self._config_data.nodegroups[nodegroup_uuid].members.append(node_name)

  def _UnlockedRemoveNodeFromGroup(self, node):
    """Remove a given node from its group.

    """
    nodegroup = node.group
    if nodegroup not in self._config_data.nodegroups:
      logging.warning("Warning: node '%s' has unknown node group '%s'"
                      " (while being removed from it)", node.name, nodegroup)
    nodegroup_obj = self._config_data.nodegroups[nodegroup]
    if node.name not in nodegroup_obj.members:
      logging.warning("Warning: node '%s' not a member of its node group '%s'"
                      " (while being removed from it)", node.name, nodegroup)
    else:
      nodegroup_obj.members.remove(node.name)

  @locking.ssynchronized(_config_lock)
  def AssignGroupNodes(self, mods):
    """Changes the group of a number of nodes.

    See L{_BaseConfigWriter.AssignGroupNodes}

    """
    groups = self._config_data.nodegroups
    nodes = self._config_data.nodes

    resmod = []

    # Try to resolve names/UUIDs first
    for (node_name, new_group_uuid) in mods:
      try:
        node = nodes[node_name]
      except KeyError:
        raise errors.ConfigurationError("Unable to find node '%s'" % node_name)

      if node.group == new_group_uuid:
        # Node is being assigned to its current group
        logging.debug("Node '%s' was assigned to its current group (%s)",
                      node_name, node.group)
        continue

      # Try to find current group of node
      try:
        old_group = groups[node.group]
      except KeyError:
        raise errors.ConfigurationError("Unable to find old group '%s'" %
                                        node.group)

      # Try to find new group for node
      try:
        new_group = groups[new_group_uuid]
      except KeyError:
        raise errors.ConfigurationError("Unable to find new group '%s'" %
                                        new_group_uuid)

      assert node.name in old_group.members, \
        ("Inconsistent configuration: node '%s' not listed in members for its"
         " old group '%s'" % (node.name, old_group.uuid))
      assert node.name not in new_group.members, \
        ("Inconsistent configuration: node '%s' already listed in members for"
         " its new group '%s'" % (node.name, new_group.uuid))

      resmod.append((node, old_group, new_group))

    # Apply changes
    for (node, old_group, new_group) in resmod:
      assert node.uuid != new_group.uuid and old_group.uuid != new_group.uuid, \
        "Assigning to current group is not possible"

      node.group = new_group.uuid

      # Update members of involved groups
      if node.name in old_group.members:
        old_group.members.remove(node.name)
      if node.name not in new_group.members:
        new_group.members.append(node.name)

    # Update timestamps and serials (only once per node/group object)
    now = time.time()
    for obj in frozenset(itertools.chain(*resmod)): # pylint: disable=W0142
      obj.serial_no += 1
      obj.mtime = now

    # Force ssconf update
    self._config_data.cluster.serial_no += 1

    self._WriteConfig()

  def _OpenConfig(self, accept_foreign):
    """Read the config data from disk.

    """
    raw_data = utils.ReadFile(self._cfg_file)

    try:
      data = objects.ConfigData.FromDict(serializer.Load(raw_data))
    except Exception, err:
      raise errors.ConfigurationError(err)

    # Make sure the configuration has the right version
    _ValidateConfig(data)

    if (not hasattr(data, "cluster") or
        not hasattr(data.cluster, "rsahostkeypub")):
      raise errors.ConfigurationError("Incomplete configuration"
                                      " (missing cluster.rsahostkeypub)")

    if data.cluster.master_node != self._my_hostname and not accept_foreign:
      msg = ("The configuration denotes node %s as master, while my"
             " hostname is %s; opening a foreign configuration is only"
             " possible in accept_foreign mode" %
             (data.cluster.master_node, self._my_hostname))
      raise errors.ConfigurationError(msg)

    self._config_data = data
    # reset the last serial as -1 so that the next write will cause
    # ssconf update
    self._last_cluster_serial = -1

    # Upgrade configuration if needed
    self._UpgradeConfig()

    self._cfg_id = utils.GetFileID(path=self._cfg_file)

  def _UpgradeConfig(self):
    """Run any upgrade steps.

    This method performs both in-object upgrades and also update some data
    elements that need uniqueness across the whole configuration or interact
    with other objects.

    @warning: this function will call L{_WriteConfig()}, but also
        L{DropECReservations} so it needs to be called only from a
        "safe" place (the constructor). If one wanted to call it with
        the lock held, a DropECReservationUnlocked would need to be
        created first, to avoid causing deadlock.

    """
    # Keep a copy of the persistent part of _config_data to check for changes
    # Serialization doesn't guarantee order in dictionaries
    oldconf = copy.deepcopy(self._config_data.ToDict())

    # In-object upgrades
    self._config_data.UpgradeConfig()

    for item in self._AllUUIDObjects():
      if item.uuid is None:
        item.uuid = self._GenerateUniqueID(_UPGRADE_CONFIG_JID)
    if not self._config_data.nodegroups:
      default_nodegroup_name = constants.INITIAL_NODE_GROUP_NAME
      default_nodegroup = objects.NodeGroup(name=default_nodegroup_name,
                                            members=[])
      self._UnlockedAddNodeGroup(default_nodegroup, _UPGRADE_CONFIG_JID, True)
    for node in self._config_data.nodes.values():
      if not node.group:
        node.group = self.LookupNodeGroup(None)
      # This is technically *not* an upgrade, but needs to be done both when
      # nodegroups are being added, and upon normally loading the config,
      # because the members list of a node group is discarded upon
      # serializing/deserializing the object.
      self._UnlockedAddNodeToGroup(node.name, node.group)

    modified = (oldconf != self._config_data.ToDict())
    if modified:
      self._WriteConfig()
      # This is ok even if it acquires the internal lock, as _UpgradeConfig is
      # only called at config init time, without the lock held
      self.DropECReservations(_UPGRADE_CONFIG_JID)

  def DistributeConfig(self, feedback_fn):
    """Distribute the configuration to the other nodes.

    Currently, this only copies the configuration file. In the future,
    it could be used to encapsulate the 2/3-phase update mechanism.

    """
    if self._offline:
      return True

    bad = False

    node_list = []
    addr_list = []
    myhostname = self._my_hostname
    # we can skip checking whether _UnlockedGetNodeInfo returns None
    # since the node list comes from _UnlocketGetNodeList, and we are
    # called with the lock held, so no modifications should take place
    # in between
    for node_name in self._UnlockedGetNodeList():
      if node_name == myhostname:
        continue
      node_info = self._UnlockedGetNodeInfo(node_name)
      if not node_info.master_candidate:
        continue
      node_list.append(node_info.name)
      addr_list.append(node_info.primary_ip)

    # TODO: Use dedicated resolver talking to config writer for name resolution
    result = \
      self._GetRpc(addr_list).call_upload_file(node_list, self._cfg_file)
    for to_node, to_result in result.items():
      msg = to_result.fail_msg
      if msg:
        msg = ("Copy of file %s to node %s failed: %s" %
               (self._cfg_file, to_node, msg))
        logging.error(msg)

        if feedback_fn:
          feedback_fn(msg)

        bad = True

    return not bad

  def _WriteConfig(self, destination=None, feedback_fn=None):
    """Write the configuration data to persistent storage.

    """
    assert feedback_fn is None or callable(feedback_fn)

    # Warn on config errors, but don't abort the save - the
    # configuration has already been modified, and we can't revert;
    # the best we can do is to warn the user and save as is, leaving
    # recovery to the user
    config_errors = self._UnlockedVerifyConfig()
    if config_errors:
      errmsg = ("Configuration data is not consistent: %s" %
                (utils.CommaJoin(config_errors)))
      logging.critical(errmsg)
      if feedback_fn:
        feedback_fn(errmsg)

    if destination is None:
      destination = self._cfg_file
    self._BumpSerialNo()
    txt = serializer.Dump(self._config_data.ToDict())

    getents = self._getents()
    try:
      fd = utils.SafeWriteFile(destination, self._cfg_id, data=txt,
                               close=False, gid=getents.confd_gid, mode=0640)
    except errors.LockError:
      raise errors.ConfigurationError("The configuration file has been"
                                      " modified since the last write, cannot"
                                      " update")
    try:
      self._cfg_id = utils.GetFileID(fd=fd)
    finally:
      os.close(fd)

    self.write_count += 1

    # and redistribute the config file to master candidates
    self.DistributeConfig(feedback_fn)

    # Write ssconf files on all nodes (including locally)
    if self._last_cluster_serial < self._config_data.cluster.serial_no:
      if not self._offline:
        result = self._GetRpc(None).call_write_ssconf_files(
          self._UnlockedGetOnlineNodeList(),
          self._UnlockedGetSsconfValues())

        for nname, nresu in result.items():
          msg = nresu.fail_msg
          if msg:
            errmsg = ("Error while uploading ssconf files to"
                      " node %s: %s" % (nname, msg))
            logging.warning(errmsg)

            if feedback_fn:
              feedback_fn(errmsg)

      self._last_cluster_serial = self._config_data.cluster.serial_no

  @locking.ssynchronized(_config_lock)
  def SetVGName(self, vg_name):
    """Set the volume group name.

    """
    self._config_data.cluster.volume_group_name = vg_name
    self._config_data.cluster.serial_no += 1
    self._WriteConfig()

  @locking.ssynchronized(_config_lock)
  def SetDRBDHelper(self, drbd_helper):
    """Set DRBD usermode helper.

    """
    self._config_data.cluster.drbd_usermode_helper = drbd_helper
    self._config_data.cluster.serial_no += 1
    self._WriteConfig()

  @locking.ssynchronized(_config_lock)
  def Update(self, target, feedback_fn, ec_id=None):
    """Notify function to be called after updates.

    This function must be called when an object (as returned by
    GetInstanceInfo, GetNodeInfo, GetCluster) has been updated and the
    caller wants the modifications saved to the backing store. Note
    that all modified objects will be saved, but the target argument
    is the one the caller wants to ensure that it's saved.

    See L{_BaseConfigWriter.Update}

    """
    if self._config_data is None:
      raise errors.ProgrammerError("Configuration file not read,"
                                   " cannot save.")
    update_serial = False
    if isinstance(target, objects.Cluster):
      test = target == self._config_data.cluster
    elif isinstance(target, objects.Node):
      test = target in self._config_data.nodes.values()
      update_serial = True
    elif isinstance(target, objects.Instance):
      test = target in self._config_data.instances.values()
    elif isinstance(target, objects.NodeGroup):
      test = target in self._config_data.nodegroups.values()
    elif isinstance(target, objects.Network):
      test = target in self._config_data.networks.values()
    else:
      raise errors.ProgrammerError("Invalid object type (%s) passed to"
                                   " ConfigWriter.Update" % type(target))
    if not test:
      raise errors.ConfigurationError("Configuration updated since object"
                                      " has been read or unknown object")
    target.serial_no += 1
    target.mtime = now = time.time()

    if update_serial:
      # for node updates, we need to increase the cluster serial too
      self._config_data.cluster.serial_no += 1
      self._config_data.cluster.mtime = now

    if isinstance(target, objects.Instance):
      self._UnlockedReleaseDRBDMinors(target.name)

    if ec_id is not None:
      # Commit all ips reserved by OpInstanceSetParams and OpGroupSetParams
      self._UnlockedCommitTemporaryIps(ec_id)

    self._WriteConfig(feedback_fn=feedback_fn)

  @locking.ssynchronized(_config_lock)
  def AddNetwork(self, net, ec_id, check_uuid=True):
    """Add a network to the configuration.

    See L{_BaseConfigWriter.AddNetwork}

    """
    self._UnlockedAddNetwork(net, ec_id, check_uuid)
    self._WriteConfig()

  @locking.ssynchronized(_config_lock)
  def RemoveNetwork(self, network_uuid):
    """Remove a network from the configuration.

    See L{_BaseConfigWriter.RemoveNetwork}

    """
    logging.info("Removing network %s from configuration", network_uuid)

    if network_uuid not in self._config_data.networks:
      raise errors.ConfigurationError("Unknown network '%s'" % network_uuid)

    del self._config_data.networks[network_uuid]
    self._config_data.cluster.serial_no += 1
    self._WriteConfig()
