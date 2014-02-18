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


"""CouchDB driver for the configuration management"""

# pylint: disable=R0904
# R0904: Too many public methods

# pylint: disable=W0212
# W0212: Access to a protected member of a client class

# pylint: disable=W0221
# W0221: Arguments number differs from overridden method

# FIXME: Issue #1,
# The _UpgradeConfig call at _OpenConfig method, does not flushes all the
# changes to the databases because it would be timeconsuming due to the new
# format of the configuration file. To be fixed.
# FIXME: Issue #2,
# The new configuration file format, causes two sequential database updates on
# every config call. This corresponds to a change to the ACID property of every
# config.data transaction that modifies the file. To be fixed.

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
from ganeti import netutils

from ganeti.config.base import \
    _BaseConfigWriter, \
    _ValidateConfig, \
    _UPGRADE_CONFIG_JID, \
    _config_lock


class CouchDBConfigWriter(_BaseConfigWriter):
  """CouchDB storage configuration type.

  """
  def __init__(self, offline=False, accept_foreign=False):
    super(CouchDBConfigWriter, self).__init__()
    self._offline = offline
    # CouchDB initialization
    # Setup the connection with CouchDB Server for all databases
    self._hostip = netutils.Hostname.GetIP(self._my_hostname)

    ip = self._hostip
    port = constants.DEFAULT_COUCHDB_PORT

    # Get database instances
    self._cfg_db = utils.GetDBInstance(constants.CLUSTER_DB, ip, port)
    self._nodes_db = utils.GetDBInstance(constants.NODES_DB, ip, port)
    self._networks_db = utils.GetDBInstance(constants.NETWORKS_DB, ip, port)
    self._instances_db = utils.GetDBInstance(constants.INSTANCES_DB, ip, port)
    self._nodegroups_db = utils.GetDBInstance(constants.NODEGROUPS_DB, ip, port)

    self._OpenConfig(accept_foreign)

  # this method needs to be static, so that we can call it on the class
  @staticmethod
  def IsCluster():
    """Check if the cluster is configured.

    """
    try:
      ip = netutils.Hostname.GetIP(netutils.Hostname.GetSysName())
      port = constants.DEFAULT_COUCHDB_PORT
      utils.GetDBInstance(constants.CLUSTER_DB, ip, port)
    except NameError:
      return False
    except errors.OpPrereqError:
      return False

    return True

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

    # Write the cluster object to the 'config_data' database.
    self._BumpSerialNo()
    data = _ClusterObjectPrepare(self._config_data)
    self._WriteConfig(db_name=self._cfg_db, data=data)
    self._config_data._rev = data["_rev"]

    return port

  @locking.ssynchronized(_config_lock)
  def AddNodeGroup(self, group, ec_id, check_uuid=True):
    """Add a node group to the configuration.

    This method calls group.UpgradeConfig() to fill any missing attributes
    according to their default values.

    see L{_BaseConfigWriter.AddNodeGroup}

    """
    self._UnlockedAddNodeGroup(group, ec_id, check_uuid)

    # Write the cluster object to the 'config_data' database.
    self._BumpSerialNo()
    data = _ClusterObjectPrepare(self._config_data)
    self._WriteConfig(db_name=self._cfg_db, data=data)
    self._config_data._rev = data["_rev"]

    # Write the nodegroup object to the 'nodegroups' database.
    group._id = group.uuid
    data = objects.NodeGroup.ToDict(group)
    self._WriteConfig(db_name=self._nodegroups_db, data=data)
    group._rev = data["_rev"]

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

    group = self._config_data.nodegroups[group_uuid]
    del self._config_data.nodegroups[group_uuid]
    self._config_data.cluster.serial_no += 1

    # Write the cluster object to the 'config_data' database.
    self._BumpSerialNo()
    data = _ClusterObjectPrepare(self._config_data)
    self._WriteConfig(db_name=self._cfg_db, data=data)
    self._config_data._rev = data["_rev"]

    # Write the nodegroup object to the 'nodegroups' database.
    self._nodegroups_db.delete(objects.NodeGroup.ToDict(group))

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

    instance._id = instance.name
    instance.serial_no = 1
    instance.ctime = instance.mtime = time.time()
    self._config_data.instances[instance.name] = instance
    self._config_data.cluster.serial_no += 1
    self._UnlockedReleaseDRBDMinors(instance.name)
    self._UnlockedCommitTemporaryIps(ec_id)

    # Write the cluster object to the 'config_data' database.
    self._BumpSerialNo()
    data = _ClusterObjectPrepare(self._config_data)
    self._WriteConfig(db_name=self._cfg_db, data=data)
    self._config_data._rev = data["_rev"]

    # Write the instance object to the 'instances' database.
    data = objects.Instance.ToDict(instance)
    self._WriteConfig(db_name=self._instances_db, data=data)
    instance._rev = data["_rev"]

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

      # Write the cluster object to the 'config_data' database.
      self._BumpSerialNo()
      data = _ClusterObjectPrepare(self._config_data)
      self._WriteConfig(db_name=self._cfg_db, data=data)
      self._config_data._rev = data["_rev"]

      # Write the instance object to the 'instances' database.
      data = objects.Instance.ToDict(instance)
      self._WriteConfig(db_name=self._instances_db, data=data)
      instance._rev = data["_rev"]

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

    # Write the cluster object to the 'config_data' database.
    self._BumpSerialNo()
    data = _ClusterObjectPrepare(self._config_data)
    self._WriteConfig(db_name=self._cfg_db, data=data)
    self._config_data._rev = data["_rev"]

    # Write the instance object to the 'instances' database.
    self._instances_db.delete(objects.Instance.ToDict(inst))

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
    inst._id = new_name

    for (idx, disk) in enumerate(inst.disks):
      if disk.dev_type == constants.LD_FILE:
        # rename the file paths in logical and physical id
        file_storage_dir = os.path.dirname(os.path.dirname(disk.logical_id[1]))
        disk.logical_id = (disk.logical_id[0],
                           utils.PathJoin(file_storage_dir, inst.name,
                                          "disk%s" % idx))
        disk.physical_id = disk.logical_id

    # Actually replace instance object
    old_inst = self._config_data.instances[old_name]
    del self._config_data.instances[old_name]
    self._config_data.instances[inst.name] = inst

    # Force update of ssconf files
    self._config_data.cluster.serial_no += 1

    # Write the cluster object to the 'config_data' database.
    self._BumpSerialNo()
    data = _ClusterObjectPrepare(self._config_data)
    self._WriteConfig(db_name=self._cfg_db, data=data)
    self._config_data._rev = data["_rev"]

    # Write the instance object to the 'instances' database.
    # FIXME: Create a function in the utils module for doc renames.
    new_doc = objects.Instance.ToDict(inst)
    new_doc.pop("_rev")
    old_doc = objects.Instance.ToDict(old_inst)
    old_doc["_deleted"] = True
    self._instances_db.update([new_doc, old_doc])
    inst._rev = new_doc["_rev"]

  @locking.ssynchronized(_config_lock)
  def AddNode(self, node, ec_id):
    """Add a node to the configuration.

    See L{_BaseConfigWriter.AddNode}

    """
    logging.info("Adding node %s to configuration", node.name)

    self._EnsureUUID(node, ec_id)

    node._id = node.name
    node.serial_no = 1
    node.ctime = node.mtime = time.time()
    self._UnlockedAddNodeToGroup(node.name, node.group)
    self._config_data.nodes[node.name] = node
    self._config_data.cluster.serial_no += 1

    # Write the cluster object to the 'config_data' database.
    self._BumpSerialNo()
    data = _ClusterObjectPrepare(self._config_data)
    self._WriteConfig(db_name=self._cfg_db, data=data)
    self._config_data._rev = data["_rev"]

    # Write the node object to the 'nodes' database.
    data = objects.Node.ToDict(node)
    self._WriteConfig(db_name=self._nodes_db, data=data)
    node._rev = data["_rev"]

    # FIXME: I should add a check in the replication process.
    # Enable continuous replication if the node marked as MC.
    if node.master_candidate:
      results = []
      for db_name in constants.CONFIG_DATA_DBS:
        db_path = "".join(("/", db_name, "/"))
        res = utils.UnlockedReplicateSetup(self._hostip, node.primary_ip,
                                           db_path, False)
        results.append((db_path.strip("/"), res))

  @locking.ssynchronized(_config_lock)
  def RemoveNode(self, node_name):
    """Remove a node from the configuration.

    """
    logging.info("Removing node %s from configuration", node_name)

    if node_name not in self._config_data.nodes:
      raise errors.ConfigurationError("Unknown node '%s'" % node_name)

    node = self._config_data.nodes[node_name]
    self._UnlockedRemoveNodeFromGroup(self._config_data.nodes[node_name])
    del self._config_data.nodes[node_name]
    self._config_data.cluster.serial_no += 1

    # Write the cluster object to the 'config_data' database.
    self._BumpSerialNo()
    data = _ClusterObjectPrepare(self._config_data)
    self._WriteConfig(db_name=self._cfg_db, data=data)
    self._config_data._rev = data["_rev"]

    # Write the node object to the 'nodes' database.
    self._nodes_db.delete(objects.Node.ToDict(node))

    # FIXME: I should add a check in the replication process.
    # Disable continuous replication if node was MC.
    if node.master_candidate:
      results = []
      for db_name in constants.CONFIG_DATA_DBS:
        db_path = "".join(("/", db_name, "/"))
        res = utils.UnlockedReplicateSetup(self._hostip, node.primary_ip,
                                           db_path, True)
        results.append((db_path.strip("/"), res))

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

        # Write the cluster object to the 'config_data' database.
        self._BumpSerialNo()
        data = _ClusterObjectPrepare(self._config_data)
        self._WriteConfig(db_name=self._cfg_db, data=data)
        self._config_data._rev = data["_rev"]

        # Bulk update the node objects to the 'nodes' database.
        nodes_docs = []
        for node in mod_list:
          nodes_docs.append(objects.Node.ToDict(node))

        result = self._nodes_db.update(nodes_docs)
        for success, _id, _rev in result:
          if success:
            self._config_data.nodes[_id]._rev = _rev
          else:
            msg = "Updating node %s with _rev %s failed." % (_id, _rev)
            raise errors.ConfigurationError(msg)

    return mod_list

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
    nodes_docs = []
    nodegroups_docs = []
    now = time.time()
    for obj in frozenset(itertools.chain(*resmod)): # pylint: disable=W0142
      obj.serial_no += 1
      obj.mtime = now
      # nodes, nodegroups objects update
      if isinstance(obj, objects.Node):
        nodes_docs.append(objects.Node.ToDict(obj))
      elif isinstance(obj, objects.NodeGroup):
        nodegroups_docs.append(objects.NodeGroup.ToDict(obj))

    # Force ssconf update
    self._config_data.cluster.serial_no += 1

    # Write the cluster object to the 'config_data' database.
    self._BumpSerialNo()
    data = _ClusterObjectPrepare(self._config_data)
    self._WriteConfig(db_name=self._cfg_db, data=data)
    self._config_data._rev = data["_rev"]

    # Bulk update the node objects to the 'nodes' database.
    result = self._nodes_db.update(nodes_docs)
    for success, _id, _rev in result:
      if success:
        self._config_data.nodes[_id]._rev = _rev
      else:
        msg = "Updating node %s with _rev %s failed." % (_id, _rev)
        raise errors.ConfigurationError(msg)

    # Bulk update the nodegroup objects to the 'nodegroups' database.
    result = self._nodegroups_db.update(nodegroups_docs)
    for success, _id, _rev in result:
      if success:
        self._config_data.nodegroups[_id]._rev = _rev
      else:
        msg = "Updating nodegroup %s with _rev %s failed." % (_id, _rev)
        raise errors.ConfigurationError(msg)

  def _OpenConfig(self, accept_foreign):
    """Read the config data from the database.

    """
    raw_data = self._BuildConfigData()

    try:
      # Tranform <couchdb.client.Document> object to <ConfigData> object
      data = objects.ConfigData.FromDict(raw_data)
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
    modified = node_updated = False

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
      # nodegroups: add (default_nodegroup)
      default_nodegroup._id = default_nodegroup.uuid
      data = objects.NodeGroup.ToDict(default_nodegroup)
      self._WriteConfig(db_name=self._nodegroups_db, data=data)
      default_nodegroup._rev = data["_rev"]
      modified = True
    for node in self._config_data.nodes.values():
      if not node.group:
        node.group = self.LookupNodeGroup(None)
        modified = node_updated = True
      # This is technically *not* an upgrade, but needs to be done both when
      # nodegroups are being added, and upon normally loading the config,
      # because the members list of a node group is discarded upon
      # serializing/deserializing the object.
      if self._UnlockedAddNodeToGroup(node.name, node.group):
        # nodegroups: modify (self._config_data.nodegroups[node.group])
        group = self._config_data.nodegroups[node.group]
        data = objects.NodeGroup.ToDict(group)
        self._WriteConfig(db_name=self._nodegroups_db, data=data)
        group._rev = data["_rev"]
        modified = True
      if node_updated:
        data = objects.Node.ToDict(node)
        self._WriteConfig(db_name=self._nodes_db, data=data)
        node._rev = data["_rev"]

    if modified:
      # This is ok even if it acquires the internal lock, as _UpgradeConfig is
      # only called at config init time, without the lock held
      self.DropECReservations(_UPGRADE_CONFIG_JID)

  @locking.ssynchronized(_config_lock)
  def DistributeConfig(self, node, replicate):
    """Wrapper using config lock around utils.UnlockedReplicateSetup().

    @type node: L{objects.Node}
    @param node: node object
    @type replicate: bool
    @param replicate: enable or disable replication with the current node

    """
    results = []
    for db_name in constants.CONFIG_DATA_DBS:
      db_path = "".join(("/", db_name, "/"))
      res = utils.UnlockedReplicateSetup(self._hostip, node.primary_ip,
                                         db_path, replicate)
      results.append((db_path.strip("/"), res))

    return results

  def _WriteConfig(self, db_name=None, data=None, feedback_fn=None):
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

    # Save the ConfigData object to datababse
    try:
      utils.WriteDocument(db_name, data)
    except errors.LockError:
      raise errors.ConfigurationError("The configuration file has been"
                                      " modified since the last write, cannot"
                                      " update")
    self.write_count += 1

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
    # Write the cluster object to the 'config_data' database.
    self._BumpSerialNo()
    data = _ClusterObjectPrepare(self._config_data)
    self._WriteConfig(db_name=self._cfg_db, data=data)
    self._config_data._rev = data["_rev"]

  @locking.ssynchronized(_config_lock)
  def SetDRBDHelper(self, drbd_helper):
    """Set DRBD usermode helper.

    """
    self._config_data.cluster.drbd_usermode_helper = drbd_helper
    self._config_data.cluster.serial_no += 1
    # Write the cluster object to the 'config_data' database.
    self._BumpSerialNo()
    data = _ClusterObjectPrepare(self._config_data)
    self._WriteConfig(db_name=self._cfg_db, data=data)
    self._config_data._rev = data["_rev"]

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

    if isinstance(target, objects.Cluster) or update_serial:
      # Write the cluster object to the 'config_data' database.
      db_name = self._cfg_db
      self._BumpSerialNo()
      data = _ClusterObjectPrepare(self._config_data)
      self._WriteConfig(db_name=db_name, data=data, feedback_fn=feedback_fn)
      self._config_data._rev = data["_rev"]
    else:
      if isinstance(target, objects.Node):
        db_name = self._nodes_db
        data = objects.Node.ToDict(target)
      elif isinstance(target, objects.Instance):
        db_name = self._instances_db
        data = objects.Instance.ToDict(target)
      elif isinstance(target, objects.NodeGroup):
        db_name = self._nodegroups_db
        data = objects.NodeGroup.ToDict(target)
      elif isinstance(target, objects.Network):
        db_name = self._networks_db
        data = objects.Network.ToDict(target)

      # Write the updated object to the appropriate database.
      self._WriteConfig(db_name=db_name, data=data, feedback_fn=feedback_fn)
      target._rev = data["_rev"]

  @locking.ssynchronized(_config_lock)
  def AddNetwork(self, net, ec_id, check_uuid=True):
    """Add a network to the configuration.

    See L{_BaseConfigWriter.AddNetwork}

    """
    self._UnlockedAddNetwork(net, ec_id, check_uuid)

    # Write the cluster object to the 'config_data' database.
    self._BumpSerialNo()
    data = _ClusterObjectPrepare(self._config_data)
    self._WriteConfig(db_name=self._cfg_db, data=data)
    self._config_data._rev = data["_rev"]

    # Write the network object to the 'networks' database.
    net._id = net.uuid
    data = objects.Network.ToDict(net)
    self._WriteConfig(db_name=self._networks_db, data=data)
    net._rev = data["_rev"]

  @locking.ssynchronized(_config_lock)
  def RemoveNetwork(self, network_uuid):
    """Remove a network from the configuration.

    See L{_BaseConfigWriter.RemoveNetwork}

    """
    logging.info("Removing network %s from configuration", network_uuid)

    if network_uuid not in self._config_data.networks:
      raise errors.ConfigurationError("Unknown network '%s'" % network_uuid)

    net = self._config_data.networks[network_uuid]
    del self._config_data.networks[network_uuid]
    self._config_data.cluster.serial_no += 1

    # Write the cluster object to the 'config_data' database.
    self._BumpSerialNo()
    data = _ClusterObjectPrepare(self._config_data)
    self._WriteConfig(db_name=self._cfg_db, data=data)
    self._config_data._rev = data["_rev"]

    # Write the network object to the 'networks' database.
    self._networks_db.delete(objects.Node.ToDict(net))

  def _BuildConfigData(self):
    """This function builds the config.data from it's components, because we
    don't want to change it's memory represantation.

    @rtype: L{couchdb.client.Document}
    @return: The config.data with it's separated components in one object

    """
    # Get config.data from the db
    raw_data = self._cfg_db.get("config.data")

    # nodes
    nodes = {}
    view_nodes = self._nodes_db.view("_all_docs", include_docs=True)
    for row in view_nodes.rows:
      node = row["doc"]
      nodes[node["name"]] = node

    # instances
    instances = {}
    view_insts = self._instances_db.view("_all_docs", include_docs=True)
    for row in view_insts.rows:
      instance = row["doc"]
      instances[instance["name"]] = instance

    # nodegroups
    nodegroups = {}
    view_groups = self._nodegroups_db.view("_all_docs", include_docs=True)
    for row in view_groups.rows:
      nodegroup = row["doc"]
      nodegroups[nodegroup["uuid"]] = nodegroup

    # networks
    networks = {}
    view_networks = self._networks_db.view("_all_docs", include_docs=True)
    for row in view_networks.rows:
      network = row["doc"]
      networks[network["uuid"]] = network

    # build the config.data object
    raw_data["nodegroups"] = nodegroups
    raw_data["nodes"] = nodes
    raw_data["instances"] = instances
    raw_data["networks"] = networks

    return raw_data


def _ClusterObjectPrepare(config_data):
  """Prepares the config_data.cluster object for writing to disk.

  We separated the config.data object into it's most heavy loaded
  components {nodes, instances, nodegroups, networks, cluster} but
  the memory represantation remain as it was. So before we write
  the cluster part of the config.data to disk we should first flush
  the rest config.data components.

  @type data: L{objects.ConfigData}
  @param data: configuration data
  @rtype: dict
  @return: The config_data object ready for writing to disk

  """
  _config = objects.ConfigData(_id=config_data._id,
                               _rev=config_data._rev,
                               version=config_data.version,
                               serial_no=config_data.serial_no,
                               mtime=config_data.mtime,
                               ctime=config_data.ctime,
                               cluster=config_data.cluster,
                               instances={},
                               nodes={},
                               nodegroups={},
                               networks={})

  return objects.ConfigData.ToDict(_config)
