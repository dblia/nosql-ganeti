#
#

# Copyright (C) 2006, 2007, 2010, 2011, 2012 Google Inc.
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

"""Cluster related commands"""

# pylint: disable=W0401,W0613,W0614,C0103
# W0401: Wildcard import ganeti.cli
# W0613: Unused argument, since all functions follow the same API
# W0614: Unused import %s from wildcard import (since we need cli)
# C0103: Invalid name gnt-cluster

import os.path
import time
import OpenSSL
import itertools

from ganeti.cli import *
from ganeti import opcodes
from ganeti import constants
from ganeti import errors
from ganeti import utils
from ganeti import bootstrap
from ganeti import ssh
from ganeti import objects
from ganeti import uidpool
from ganeti import compat
from ganeti import netutils
from ganeti import pathutils


ON_OPT = cli_option("--on", default=False,
                    action="store_true", dest="on",
                    help="Recover from an EPO")

GROUPS_OPT = cli_option("--groups", default=False,
                        action="store_true", dest="groups",
                        help="Arguments are node groups instead of nodes")

FORCE_FAILOVER = cli_option("--yes-do-it", dest="yes_do_it",
                            help="Override interactive check for --no-voting",
                            default=False, action="store_true")

_EPO_PING_INTERVAL = 30 # 30 seconds between pings
_EPO_PING_TIMEOUT = 1 # 1 second
_EPO_REACHABLE_TIMEOUT = 15 * 60 # 15 minutes


@UsesRPC
def InitCluster(opts, args):
  """Initialize the cluster.

  @param opts: the command line options selected by the user
  @type args: list
  @param args: should contain only one element, the desired
      cluster name
  @rtype: int
  @return: the desired exit code

  """
  if not opts.lvm_storage and opts.vg_name:
    ToStderr("Options --no-lvm-storage and --vg-name conflict.")
    return 1

  vg_name = opts.vg_name
  if opts.lvm_storage and not opts.vg_name:
    vg_name = constants.DEFAULT_VG

  if not opts.drbd_storage and opts.drbd_helper:
    ToStderr("Options --no-drbd-storage and --drbd-usermode-helper conflict.")
    return 1

  drbd_helper = opts.drbd_helper
  if opts.drbd_storage and not opts.drbd_helper:
    drbd_helper = constants.DEFAULT_DRBD_HELPER

  master_netdev = opts.master_netdev
  if master_netdev is None:
    master_netdev = constants.DEFAULT_BRIDGE

  hvlist = opts.enabled_hypervisors
  if hvlist is None:
    hvlist = constants.DEFAULT_ENABLED_HYPERVISOR
  hvlist = hvlist.split(",")

  hvparams = dict(opts.hvparams)
  beparams = opts.beparams
  nicparams = opts.nicparams

  diskparams = dict(opts.diskparams)

  # check the disk template types here, as we cannot rely on the type check done
  # by the opcode parameter types
  diskparams_keys = set(diskparams.keys())
  if not (diskparams_keys <= constants.DISK_TEMPLATES):
    unknown = utils.NiceSort(diskparams_keys - constants.DISK_TEMPLATES)
    ToStderr("Disk templates unknown: %s" % utils.CommaJoin(unknown))
    return 1

  # prepare beparams dict
  beparams = objects.FillDict(constants.BEC_DEFAULTS, beparams)
  utils.ForceDictType(beparams, constants.BES_PARAMETER_COMPAT)

  # prepare nicparams dict
  nicparams = objects.FillDict(constants.NICC_DEFAULTS, nicparams)
  utils.ForceDictType(nicparams, constants.NICS_PARAMETER_TYPES)

  # prepare ndparams dict
  if opts.ndparams is None:
    ndparams = dict(constants.NDC_DEFAULTS)
  else:
    ndparams = objects.FillDict(constants.NDC_DEFAULTS, opts.ndparams)
    utils.ForceDictType(ndparams, constants.NDS_PARAMETER_TYPES)

  # prepare hvparams dict
  for hv in constants.HYPER_TYPES:
    if hv not in hvparams:
      hvparams[hv] = {}
    hvparams[hv] = objects.FillDict(constants.HVC_DEFAULTS[hv], hvparams[hv])
    utils.ForceDictType(hvparams[hv], constants.HVS_PARAMETER_TYPES)

  # prepare diskparams dict
  for templ in constants.DISK_TEMPLATES:
    if templ not in diskparams:
      diskparams[templ] = {}
    diskparams[templ] = objects.FillDict(constants.DISK_DT_DEFAULTS[templ],
                                         diskparams[templ])
    utils.ForceDictType(diskparams[templ], constants.DISK_DT_TYPES)

  # prepare ipolicy dict
  ipolicy_raw = CreateIPolicyFromOpts(
    ispecs_mem_size=opts.ispecs_mem_size,
    ispecs_cpu_count=opts.ispecs_cpu_count,
    ispecs_disk_count=opts.ispecs_disk_count,
    ispecs_disk_size=opts.ispecs_disk_size,
    ispecs_nic_count=opts.ispecs_nic_count,
    ipolicy_disk_templates=opts.ipolicy_disk_templates,
    ipolicy_vcpu_ratio=opts.ipolicy_vcpu_ratio,
    ipolicy_spindle_ratio=opts.ipolicy_spindle_ratio,
    fill_all=True)
  ipolicy = objects.FillIPolicy(constants.IPOLICY_DEFAULTS, ipolicy_raw)

  if opts.candidate_pool_size is None:
    opts.candidate_pool_size = constants.MASTER_POOL_SIZE_DEFAULT

  if opts.mac_prefix is None:
    opts.mac_prefix = constants.DEFAULT_MAC_PREFIX

  uid_pool = opts.uid_pool
  if uid_pool is not None:
    uid_pool = uidpool.ParseUidPool(uid_pool)

  if opts.prealloc_wipe_disks is None:
    opts.prealloc_wipe_disks = False

  external_ip_setup_script = opts.use_external_mip_script
  if external_ip_setup_script is None:
    external_ip_setup_script = False

  try:
    primary_ip_version = int(opts.primary_ip_version)
  except (ValueError, TypeError), err:
    ToStderr("Invalid primary ip version value: %s" % str(err))
    return 1

  master_netmask = opts.master_netmask
  try:
    if master_netmask is not None:
      master_netmask = int(master_netmask)
  except (ValueError, TypeError), err:
    ToStderr("Invalid master netmask value: %s" % str(err))
    return 1

  if opts.disk_state:
    disk_state = utils.FlatToDict(opts.disk_state)
  else:
    disk_state = {}

  hv_state = dict(opts.hv_state)

  if opts.backend_storage not in constants.VALID_BACKEND_STORAGE_TYPES:
    avail = " ".join(constants.VALID_BACKEND_STORAGE_TYPES)
    ToStderr("Unknown backend storage type: %s." % opts.backend_storage)
    ToStderr("Please specify one of the following available types: %s" % avail)
    return 1

  bootstrap.InitCluster(cluster_name=args[0],
                        backend_storage=opts.backend_storage,
                        secondary_ip=opts.secondary_ip,
                        vg_name=vg_name,
                        mac_prefix=opts.mac_prefix,
                        master_netmask=master_netmask,
                        master_netdev=master_netdev,
                        file_storage_dir=opts.file_storage_dir,
                        shared_file_storage_dir=opts.shared_file_storage_dir,
                        enabled_hypervisors=hvlist,
                        hvparams=hvparams,
                        beparams=beparams,
                        nicparams=nicparams,
                        ndparams=ndparams,
                        diskparams=diskparams,
                        ipolicy=ipolicy,
                        candidate_pool_size=opts.candidate_pool_size,
                        modify_etc_hosts=opts.modify_etc_hosts,
                        modify_ssh_setup=opts.modify_ssh_setup,
                        maintain_node_health=opts.maintain_node_health,
                        drbd_helper=drbd_helper,
                        uid_pool=uid_pool,
                        default_iallocator=opts.default_iallocator,
                        primary_ip_version=primary_ip_version,
                        prealloc_wipe_disks=opts.prealloc_wipe_disks,
                        use_external_mip_script=external_ip_setup_script,
                        hv_state=hv_state,
                        disk_state=disk_state,
                        )
  op = opcodes.OpClusterPostInit()
  SubmitOpCode(op, opts=opts)
  return 0


@UsesRPC
def DestroyCluster(opts, args):
  """Destroy the cluster.

  @param opts: the command line options selected by the user
  @type args: list
  @param args: should be an empty list
  @rtype: int
  @return: the desired exit code

  """
  if not opts.yes_do_it:
    ToStderr("Destroying a cluster is irreversible. If you really want"
             " destroy this cluster, supply the --yes-do-it option.")
    return 1

  op = opcodes.OpClusterDestroy()
  master = SubmitOpCode(op, opts=opts)
  # if we reached this, the opcode didn't fail; we can proceed to
  # shutdown all the daemons
  bootstrap.FinalizeClusterDestroy(master)
  return 0


def RenameCluster(opts, args):
  """Rename the cluster.

  @param opts: the command line options selected by the user
  @type args: list
  @param args: should contain only one element, the new cluster name
  @rtype: int
  @return: the desired exit code

  """
  cl = GetClient()

  (cluster_name, ) = cl.QueryConfigValues(["cluster_name"])

  new_name = args[0]
  if not opts.force:
    usertext = ("This will rename the cluster from '%s' to '%s'. If you are"
                " connected over the network to the cluster name, the"
                " operation is very dangerous as the IP address will be"
                " removed from the node and the change may not go through."
                " Continue?") % (cluster_name, new_name)
    if not AskUser(usertext):
      return 1

  op = opcodes.OpClusterRename(name=new_name)
  result = SubmitOpCode(op, opts=opts, cl=cl)

  if result:
    ToStdout("Cluster renamed from '%s' to '%s'", cluster_name, result)

  return 0


def ActivateMasterIp(opts, args):
  """Activates the master IP.

  """
  op = opcodes.OpClusterActivateMasterIp()
  SubmitOpCode(op)
  return 0


def DeactivateMasterIp(opts, args):
  """Deactivates the master IP.

  """
  if not opts.confirm:
    usertext = ("This will disable the master IP. All the open connections to"
                " the master IP will be closed. To reach the master you will"
                " need to use its node IP."
                " Continue?")
    if not AskUser(usertext):
      return 1

  op = opcodes.OpClusterDeactivateMasterIp()
  SubmitOpCode(op)
  return 0


def RedistributeConfig(opts, args):
  """Forces push of the cluster configuration.

  @param opts: the command line options selected by the user
  @type args: list
  @param args: empty list
  @rtype: int
  @return: the desired exit code

  """
  op = opcodes.OpClusterRedistConf()
  SubmitOrSend(op, opts)
  return 0


def ShowClusterVersion(opts, args):
  """Write version of ganeti software to the standard output.

  @param opts: the command line options selected by the user
  @type args: list
  @param args: should be an empty list
  @rtype: int
  @return: the desired exit code

  """
  cl = GetClient(query=True)
  result = cl.QueryClusterInfo()
  ToStdout("Software version: %s", result["software_version"])
  ToStdout("Internode protocol: %s", result["protocol_version"])
  ToStdout("Configuration format: %s", result["config_version"])
  ToStdout("OS api version: %s", result["os_api_version"])
  ToStdout("Export interface: %s", result["export_version"])
  return 0


def ShowClusterMaster(opts, args):
  """Write name of master node to the standard output.

  @param opts: the command line options selected by the user
  @type args: list
  @param args: should be an empty list
  @rtype: int
  @return: the desired exit code

  """
  master = bootstrap.GetMaster()
  ToStdout(master)
  return 0


def _PrintGroupedParams(paramsdict, level=1, roman=False):
  """Print Grouped parameters (be, nic, disk) by group.

  @type paramsdict: dict of dicts
  @param paramsdict: {group: {param: value, ...}, ...}
  @type level: int
  @param level: Level of indention

  """
  indent = "  " * level
  for item, val in sorted(paramsdict.items()):
    if isinstance(val, dict):
      ToStdout("%s- %s:", indent, item)
      _PrintGroupedParams(val, level=level + 1, roman=roman)
    elif roman and isinstance(val, int):
      ToStdout("%s  %s: %s", indent, item, compat.TryToRoman(val))
    else:
      ToStdout("%s  %s: %s", indent, item, val)


def ShowClusterConfig(opts, args):
  """Shows cluster information.

  @param opts: the command line options selected by the user
  @type args: list
  @param args: should be an empty list
  @rtype: int
  @return: the desired exit code

  """
  cl = GetClient(query=True)
  result = cl.QueryClusterInfo()

  ToStdout("Cluster name: %s", result["name"])
  ToStdout("Cluster UUID: %s", result["uuid"])

  ToStdout("Creation time: %s", utils.FormatTime(result["ctime"]))
  ToStdout("Modification time: %s", utils.FormatTime(result["mtime"]))

  ToStdout("Master node: %s", result["master"])

  ToStdout("Architecture (this node): %s (%s)",
           result["architecture"][0], result["architecture"][1])

  if result["tags"]:
    tags = utils.CommaJoin(utils.NiceSort(result["tags"]))
  else:
    tags = "(none)"

  ToStdout("Tags: %s", tags)

  ToStdout("Default hypervisor: %s", result["default_hypervisor"])
  ToStdout("Enabled hypervisors: %s",
           utils.CommaJoin(result["enabled_hypervisors"]))

  ToStdout("Hypervisor parameters:")
  _PrintGroupedParams(result["hvparams"])

  ToStdout("OS-specific hypervisor parameters:")
  _PrintGroupedParams(result["os_hvp"])

  ToStdout("OS parameters:")
  _PrintGroupedParams(result["osparams"])

  ToStdout("Hidden OSes: %s", utils.CommaJoin(result["hidden_os"]))
  ToStdout("Blacklisted OSes: %s", utils.CommaJoin(result["blacklisted_os"]))

  ToStdout("Cluster parameters:")
  ToStdout("  - candidate pool size: %s",
            compat.TryToRoman(result["candidate_pool_size"],
                              convert=opts.roman_integers))
  ToStdout("  - master netdev: %s", result["master_netdev"])
  ToStdout("  - master netmask: %s", result["master_netmask"])
  ToStdout("  - use external master IP address setup script: %s",
           result["use_external_mip_script"])
  ToStdout("  - lvm volume group: %s", result["volume_group_name"])
  if result["reserved_lvs"]:
    reserved_lvs = utils.CommaJoin(result["reserved_lvs"])
  else:
    reserved_lvs = "(none)"
  ToStdout("  - lvm reserved volumes: %s", reserved_lvs)
  ToStdout("  - drbd usermode helper: %s", result["drbd_usermode_helper"])
  ToStdout("  - file storage path: %s", result["file_storage_dir"])
  ToStdout("  - shared file storage path: %s",
           result["shared_file_storage_dir"])
  ToStdout("  - maintenance of node health: %s",
           result["maintain_node_health"])
  ToStdout("  - uid pool: %s", uidpool.FormatUidPool(result["uid_pool"]))
  ToStdout("  - default instance allocator: %s", result["default_iallocator"])
  ToStdout("  - primary ip version: %d", result["primary_ip_version"])
  ToStdout("  - preallocation wipe disks: %s", result["prealloc_wipe_disks"])
  ToStdout("  - OS search path: %s", utils.CommaJoin(pathutils.OS_SEARCH_PATH))
  ToStdout("  - ExtStorage Providers search path: %s",
           utils.CommaJoin(pathutils.ES_SEARCH_PATH))

  ToStdout("Default node parameters:")
  _PrintGroupedParams(result["ndparams"], roman=opts.roman_integers)

  ToStdout("Default instance parameters:")
  _PrintGroupedParams(result["beparams"], roman=opts.roman_integers)

  ToStdout("Default nic parameters:")
  _PrintGroupedParams(result["nicparams"], roman=opts.roman_integers)

  ToStdout("Default disk parameters:")
  _PrintGroupedParams(result["diskparams"], roman=opts.roman_integers)

  ToStdout("Instance policy - limits for instances:")
  for key in constants.IPOLICY_ISPECS:
    ToStdout("  - %s", key)
    _PrintGroupedParams(result["ipolicy"][key], roman=opts.roman_integers)
  ToStdout("  - enabled disk templates: %s",
           utils.CommaJoin(result["ipolicy"][constants.IPOLICY_DTS]))
  for key in constants.IPOLICY_PARAMETERS:
    ToStdout("  - %s: %s", key, result["ipolicy"][key])

  return 0


def ClusterCopyFile(opts, args):
  """Copy a file from master to some nodes.

  @param opts: the command line options selected by the user
  @type args: list
  @param args: should contain only one element, the path of
      the file to be copied
  @rtype: int
  @return: the desired exit code

  """
  filename = args[0]
  if not os.path.exists(filename):
    raise errors.OpPrereqError("No such filename '%s'" % filename,
                               errors.ECODE_INVAL)

  cl = GetClient()

  cluster_name = cl.QueryConfigValues(["cluster_name"])[0]

  results = GetOnlineNodes(nodes=opts.nodes, cl=cl, filter_master=True,
                           secondary_ips=opts.use_replication_network,
                           nodegroup=opts.nodegroup)

  srun = ssh.SshRunner(cluster_name)
  for node in results:
    if not srun.CopyFileToNode(node, filename):
      ToStderr("Copy of file %s to node %s failed", filename, node)

  return 0


def RunClusterCommand(opts, args):
  """Run a command on some nodes.

  @param opts: the command line options selected by the user
  @type args: list
  @param args: should contain the command to be run and its arguments
  @rtype: int
  @return: the desired exit code

  """
  cl = GetClient()

  command = " ".join(args)

  nodes = GetOnlineNodes(nodes=opts.nodes, cl=cl, nodegroup=opts.nodegroup)

  cluster_name, master_node = cl.QueryConfigValues(["cluster_name",
                                                    "master_node"])

  srun = ssh.SshRunner(cluster_name=cluster_name)

  # Make sure master node is at list end
  if master_node in nodes:
    nodes.remove(master_node)
    nodes.append(master_node)

  for name in nodes:
    result = srun.Run(name, constants.SSH_LOGIN_USER, command)

    if opts.failure_only and result.exit_code == constants.EXIT_SUCCESS:
      # Do not output anything for successful commands
      continue

    ToStdout("------------------------------------------------")
    if opts.show_machine_names:
      for line in result.output.splitlines():
        ToStdout("%s: %s", name, line)
    else:
      ToStdout("node: %s", name)
      ToStdout("%s", result.output)
    ToStdout("return code = %s", result.exit_code)

  return 0


def VerifyCluster(opts, args):
  """Verify integrity of cluster, performing various test on nodes.

  @param opts: the command line options selected by the user
  @type args: list
  @param args: should be an empty list
  @rtype: int
  @return: the desired exit code

  """
  skip_checks = []

  if opts.skip_nplusone_mem:
    skip_checks.append(constants.VERIFY_NPLUSONE_MEM)

  cl = GetClient()

  op = opcodes.OpClusterVerify(verbose=opts.verbose,
                               error_codes=opts.error_codes,
                               debug_simulate_errors=opts.simulate_errors,
                               skip_checks=skip_checks,
                               ignore_errors=opts.ignore_errors,
                               group_name=opts.nodegroup)
  result = SubmitOpCode(op, cl=cl, opts=opts)

  # Keep track of submitted jobs
  jex = JobExecutor(cl=cl, opts=opts)

  for (status, job_id) in result[constants.JOB_IDS_KEY]:
    jex.AddJobId(None, status, job_id)

  results = jex.GetResults()

  (bad_jobs, bad_results) = \
    map(len,
        # Convert iterators to lists
        map(list,
            # Count errors
            map(compat.partial(itertools.ifilterfalse, bool),
                # Convert result to booleans in a tuple
                zip(*((job_success, len(op_results) == 1 and op_results[0])
                      for (job_success, op_results) in results)))))

  if bad_jobs == 0 and bad_results == 0:
    rcode = constants.EXIT_SUCCESS
  else:
    rcode = constants.EXIT_FAILURE
    if bad_jobs > 0:
      ToStdout("%s job(s) failed while verifying the cluster.", bad_jobs)

  return rcode


def VerifyDisks(opts, args):
  """Verify integrity of cluster disks.

  @param opts: the command line options selected by the user
  @type args: list
  @param args: should be an empty list
  @rtype: int
  @return: the desired exit code

  """
  cl = GetClient()

  op = opcodes.OpClusterVerifyDisks()

  result = SubmitOpCode(op, cl=cl, opts=opts)

  # Keep track of submitted jobs
  jex = JobExecutor(cl=cl, opts=opts)

  for (status, job_id) in result[constants.JOB_IDS_KEY]:
    jex.AddJobId(None, status, job_id)

  retcode = constants.EXIT_SUCCESS

  for (status, result) in jex.GetResults():
    if not status:
      ToStdout("Job failed: %s", result)
      continue

    ((bad_nodes, instances, missing), ) = result

    for node, text in bad_nodes.items():
      ToStdout("Error gathering data on node %s: %s",
               node, utils.SafeEncode(text[-400:]))
      retcode = constants.EXIT_FAILURE
      ToStdout("You need to fix these nodes first before fixing instances")

    for iname in instances:
      if iname in missing:
        continue
      op = opcodes.OpInstanceActivateDisks(instance_name=iname)
      try:
        ToStdout("Activating disks for instance '%s'", iname)
        SubmitOpCode(op, opts=opts, cl=cl)
      except errors.GenericError, err:
        nret, msg = FormatError(err)
        retcode |= nret
        ToStderr("Error activating disks for instance %s: %s", iname, msg)

    if missing:
      for iname, ival in missing.iteritems():
        all_missing = compat.all(x[0] in bad_nodes for x in ival)
        if all_missing:
          ToStdout("Instance %s cannot be verified as it lives on"
                   " broken nodes", iname)
        else:
          ToStdout("Instance %s has missing logical volumes:", iname)
          ival.sort()
          for node, vol in ival:
            if node in bad_nodes:
              ToStdout("\tbroken node %s /dev/%s", node, vol)
            else:
              ToStdout("\t%s /dev/%s", node, vol)

      ToStdout("You need to replace or recreate disks for all the above"
               " instances if this message persists after fixing broken nodes.")
      retcode = constants.EXIT_FAILURE
    elif not instances:
      ToStdout("No disks need to be activated.")

  return retcode


def RepairDiskSizes(opts, args):
  """Verify sizes of cluster disks.

  @param opts: the command line options selected by the user
  @type args: list
  @param args: optional list of instances to restrict check to
  @rtype: int
  @return: the desired exit code

  """
  op = opcodes.OpClusterRepairDiskSizes(instances=args)
  SubmitOpCode(op, opts=opts)


@UsesRPC
def MasterFailover(opts, args):
  """Failover the master node.

  This command, when run on a non-master node, will cause the current
  master to cease being master, and the non-master to become new
  master.

  @param opts: the command line options selected by the user
  @type args: list
  @param args: should be an empty list
  @rtype: int
  @return: the desired exit code

  """
  if opts.no_voting and not opts.yes_do_it:
    usertext = ("This will perform the failover even if most other nodes"
                " are down, or if this node is outdated. This is dangerous"
                " as it can lead to a non-consistent cluster. Check the"
                " gnt-cluster(8) man page before proceeding. Continue?")
    if not AskUser(usertext):
      return 1

  return bootstrap.MasterFailover(no_voting=opts.no_voting)


def MasterPing(opts, args):
  """Checks if the master is alive.

  @param opts: the command line options selected by the user
  @type args: list
  @param args: should be an empty list
  @rtype: int
  @return: the desired exit code

  """
  try:
    cl = GetClient()
    cl.QueryClusterInfo()
    return 0
  except Exception: # pylint: disable=W0703
    return 1


def SearchTags(opts, args):
  """Searches the tags on all the cluster.

  @param opts: the command line options selected by the user
  @type args: list
  @param args: should contain only one element, the tag pattern
  @rtype: int
  @return: the desired exit code

  """
  op = opcodes.OpTagsSearch(pattern=args[0])
  result = SubmitOpCode(op, opts=opts)
  if not result:
    return 1
  result = list(result)
  result.sort()
  for path, tag in result:
    ToStdout("%s %s", path, tag)


def _ReadAndVerifyCert(cert_filename, verify_private_key=False):
  """Reads and verifies an X509 certificate.

  @type cert_filename: string
  @param cert_filename: the path of the file containing the certificate to
                        verify encoded in PEM format
  @type verify_private_key: bool
  @param verify_private_key: whether to verify the private key in addition to
                             the public certificate
  @rtype: string
  @return: a string containing the PEM-encoded certificate.

  """
  try:
    pem = utils.ReadFile(cert_filename)
  except IOError, err:
    raise errors.X509CertError(cert_filename,
                               "Unable to read certificate: %s" % str(err))

  try:
    OpenSSL.crypto.load_certificate(OpenSSL.crypto.FILETYPE_PEM, pem)
  except Exception, err:
    raise errors.X509CertError(cert_filename,
                               "Unable to load certificate: %s" % str(err))

  if verify_private_key:
    try:
      OpenSSL.crypto.load_privatekey(OpenSSL.crypto.FILETYPE_PEM, pem)
    except Exception, err:
      raise errors.X509CertError(cert_filename,
                                 "Unable to load private key: %s" % str(err))

  return pem


def _RenewCrypto(new_cluster_cert, new_rapi_cert, # pylint: disable=R0911
                 rapi_cert_filename, new_spice_cert, spice_cert_filename,
                 spice_cacert_filename, new_confd_hmac_key, new_cds,
                 cds_filename, force):
  """Renews cluster certificates, keys and secrets.

  @type new_cluster_cert: bool
  @param new_cluster_cert: Whether to generate a new cluster certificate
  @type new_rapi_cert: bool
  @param new_rapi_cert: Whether to generate a new RAPI certificate
  @type rapi_cert_filename: string
  @param rapi_cert_filename: Path to file containing new RAPI certificate
  @type new_spice_cert: bool
  @param new_spice_cert: Whether to generate a new SPICE certificate
  @type spice_cert_filename: string
  @param spice_cert_filename: Path to file containing new SPICE certificate
  @type spice_cacert_filename: string
  @param spice_cacert_filename: Path to file containing the certificate of the
                                CA that signed the SPICE certificate
  @type new_confd_hmac_key: bool
  @param new_confd_hmac_key: Whether to generate a new HMAC key
  @type new_cds: bool
  @param new_cds: Whether to generate a new cluster domain secret
  @type cds_filename: string
  @param cds_filename: Path to file containing new cluster domain secret
  @type force: bool
  @param force: Whether to ask user for confirmation

  """
  if new_rapi_cert and rapi_cert_filename:
    ToStderr("Only one of the --new-rapi-certificate and --rapi-certificate"
             " options can be specified at the same time.")
    return 1

  if new_cds and cds_filename:
    ToStderr("Only one of the --new-cluster-domain-secret and"
             " --cluster-domain-secret options can be specified at"
             " the same time.")
    return 1

  if new_spice_cert and (spice_cert_filename or spice_cacert_filename):
    ToStderr("When using --new-spice-certificate, the --spice-certificate"
             " and --spice-ca-certificate must not be used.")
    return 1

  if bool(spice_cacert_filename) ^ bool(spice_cert_filename):
    ToStderr("Both --spice-certificate and --spice-ca-certificate must be"
             " specified.")
    return 1

  rapi_cert_pem, spice_cert_pem, spice_cacert_pem = (None, None, None)
  try:
    if rapi_cert_filename:
      rapi_cert_pem = _ReadAndVerifyCert(rapi_cert_filename, True)
    if spice_cert_filename:
      spice_cert_pem = _ReadAndVerifyCert(spice_cert_filename, True)
      spice_cacert_pem = _ReadAndVerifyCert(spice_cacert_filename)
  except errors.X509CertError, err:
    ToStderr("Unable to load X509 certificate from %s: %s", err[0], err[1])
    return 1

  if cds_filename:
    try:
      cds = utils.ReadFile(cds_filename)
    except Exception, err: # pylint: disable=W0703
      ToStderr("Can't load new cluster domain secret from %s: %s" %
               (cds_filename, str(err)))
      return 1
  else:
    cds = None

  if not force:
    usertext = ("This requires all daemons on all nodes to be restarted and"
                " may take some time. Continue?")
    if not AskUser(usertext):
      return 1

  def _RenewCryptoInner(ctx):
    ctx.feedback_fn("Updating certificates and keys")
    bootstrap.GenerateClusterCrypto(new_cluster_cert,
                                    new_rapi_cert,
                                    new_spice_cert,
                                    new_confd_hmac_key,
                                    new_cds,
                                    rapi_cert_pem=rapi_cert_pem,
                                    spice_cert_pem=spice_cert_pem,
                                    spice_cacert_pem=spice_cacert_pem,
                                    cds=cds)

    files_to_copy = []

    if new_cluster_cert:
      files_to_copy.append(pathutils.NODED_CERT_FILE)

    if new_rapi_cert or rapi_cert_pem:
      files_to_copy.append(pathutils.RAPI_CERT_FILE)

    if new_spice_cert or spice_cert_pem:
      files_to_copy.append(pathutils.SPICE_CERT_FILE)
      files_to_copy.append(pathutils.SPICE_CACERT_FILE)

    if new_confd_hmac_key:
      files_to_copy.append(pathutils.CONFD_HMAC_KEY)

    if new_cds or cds:
      files_to_copy.append(pathutils.CLUSTER_DOMAIN_SECRET_FILE)

    if files_to_copy:
      for node_name in ctx.nonmaster_nodes:
        ctx.feedback_fn("Copying %s to %s" %
                        (", ".join(files_to_copy), node_name))
        for file_name in files_to_copy:
          ctx.ssh.CopyFileToNode(node_name, file_name)

  RunWhileClusterStopped(ToStdout, _RenewCryptoInner)

  ToStdout("All requested certificates and keys have been replaced."
           " Running \"gnt-cluster verify\" now is recommended.")

  return 0


def RenewCrypto(opts, args):
  """Renews cluster certificates, keys and secrets.

  """
  return _RenewCrypto(opts.new_cluster_cert,
                      opts.new_rapi_cert,
                      opts.rapi_cert,
                      opts.new_spice_cert,
                      opts.spice_cert,
                      opts.spice_cacert,
                      opts.new_confd_hmac_key,
                      opts.new_cluster_domain_secret,
                      opts.cluster_domain_secret,
                      opts.force)


def SetClusterParams(opts, args):
  """Modify the cluster.

  @param opts: the command line options selected by the user
  @type args: list
  @param args: should be an empty list
  @rtype: int
  @return: the desired exit code

  """
  if not (not opts.lvm_storage or opts.vg_name or
          not opts.drbd_storage or opts.drbd_helper or
          opts.enabled_hypervisors or opts.hvparams or
          opts.beparams or opts.nicparams or
          opts.ndparams or opts.diskparams or
          opts.candidate_pool_size is not None or
          opts.uid_pool is not None or
          opts.maintain_node_health is not None or
          opts.add_uids is not None or
          opts.remove_uids is not None or
          opts.default_iallocator is not None or
          opts.reserved_lvs is not None or
          opts.master_netdev is not None or
          opts.master_netmask is not None or
          opts.use_external_mip_script is not None or
          opts.prealloc_wipe_disks is not None or
          opts.hv_state or
          opts.disk_state or
          opts.ispecs_mem_size or
          opts.ispecs_cpu_count or
          opts.ispecs_disk_count or
          opts.ispecs_disk_size or
          opts.ispecs_nic_count or
          opts.ipolicy_disk_templates is not None or
          opts.ipolicy_vcpu_ratio is not None or
          opts.ipolicy_spindle_ratio is not None):
    ToStderr("Please give at least one of the parameters.")
    return 1

  vg_name = opts.vg_name
  if not opts.lvm_storage and opts.vg_name:
    ToStderr("Options --no-lvm-storage and --vg-name conflict.")
    return 1

  if not opts.lvm_storage:
    vg_name = ""

  drbd_helper = opts.drbd_helper
  if not opts.drbd_storage and opts.drbd_helper:
    ToStderr("Options --no-drbd-storage and --drbd-usermode-helper conflict.")
    return 1

  if not opts.drbd_storage:
    drbd_helper = ""

  hvlist = opts.enabled_hypervisors
  if hvlist is not None:
    hvlist = hvlist.split(",")

  # a list of (name, dict) we can pass directly to dict() (or [])
  hvparams = dict(opts.hvparams)
  for hv_params in hvparams.values():
    utils.ForceDictType(hv_params, constants.HVS_PARAMETER_TYPES)

  diskparams = dict(opts.diskparams)

  for dt_params in diskparams.values():
    utils.ForceDictType(dt_params, constants.DISK_DT_TYPES)

  beparams = opts.beparams
  utils.ForceDictType(beparams, constants.BES_PARAMETER_COMPAT)

  nicparams = opts.nicparams
  utils.ForceDictType(nicparams, constants.NICS_PARAMETER_TYPES)

  ndparams = opts.ndparams
  if ndparams is not None:
    utils.ForceDictType(ndparams, constants.NDS_PARAMETER_TYPES)

  ipolicy = CreateIPolicyFromOpts(
    ispecs_mem_size=opts.ispecs_mem_size,
    ispecs_cpu_count=opts.ispecs_cpu_count,
    ispecs_disk_count=opts.ispecs_disk_count,
    ispecs_disk_size=opts.ispecs_disk_size,
    ispecs_nic_count=opts.ispecs_nic_count,
    ipolicy_disk_templates=opts.ipolicy_disk_templates,
    ipolicy_vcpu_ratio=opts.ipolicy_vcpu_ratio,
    ipolicy_spindle_ratio=opts.ipolicy_spindle_ratio,
    )

  mnh = opts.maintain_node_health

  uid_pool = opts.uid_pool
  if uid_pool is not None:
    uid_pool = uidpool.ParseUidPool(uid_pool)

  add_uids = opts.add_uids
  if add_uids is not None:
    add_uids = uidpool.ParseUidPool(add_uids)

  remove_uids = opts.remove_uids
  if remove_uids is not None:
    remove_uids = uidpool.ParseUidPool(remove_uids)

  if opts.reserved_lvs is not None:
    if opts.reserved_lvs == "":
      opts.reserved_lvs = []
    else:
      opts.reserved_lvs = utils.UnescapeAndSplit(opts.reserved_lvs, sep=",")

  if opts.master_netmask is not None:
    try:
      opts.master_netmask = int(opts.master_netmask)
    except ValueError:
      ToStderr("The --master-netmask option expects an int parameter.")
      return 1

  ext_ip_script = opts.use_external_mip_script

  if opts.disk_state:
    disk_state = utils.FlatToDict(opts.disk_state)
  else:
    disk_state = {}

  hv_state = dict(opts.hv_state)

  op = opcodes.OpClusterSetParams(vg_name=vg_name,
                                  drbd_helper=drbd_helper,
                                  enabled_hypervisors=hvlist,
                                  hvparams=hvparams,
                                  os_hvp=None,
                                  beparams=beparams,
                                  nicparams=nicparams,
                                  ndparams=ndparams,
                                  diskparams=diskparams,
                                  ipolicy=ipolicy,
                                  candidate_pool_size=opts.candidate_pool_size,
                                  maintain_node_health=mnh,
                                  uid_pool=uid_pool,
                                  add_uids=add_uids,
                                  remove_uids=remove_uids,
                                  default_iallocator=opts.default_iallocator,
                                  prealloc_wipe_disks=opts.prealloc_wipe_disks,
                                  master_netdev=opts.master_netdev,
                                  master_netmask=opts.master_netmask,
                                  reserved_lvs=opts.reserved_lvs,
                                  use_external_mip_script=ext_ip_script,
                                  hv_state=hv_state,
                                  disk_state=disk_state,
                                  )
  SubmitOrSend(op, opts)
  return 0


def QueueOps(opts, args):
  """Queue operations.

  @param opts: the command line options selected by the user
  @type args: list
  @param args: should contain only one element, the subcommand
  @rtype: int
  @return: the desired exit code

  """
  command = args[0]
  client = GetClient()
  if command in ("drain", "undrain"):
    drain_flag = command == "drain"
    client.SetQueueDrainFlag(drain_flag)
  elif command == "info":
    result = client.QueryConfigValues(["drain_flag"])
    if result[0]:
      val = "set"
    else:
      val = "unset"
    ToStdout("The drain flag is %s" % val)
  else:
    raise errors.OpPrereqError("Command '%s' is not valid." % command,
                               errors.ECODE_INVAL)

  return 0


def _ShowWatcherPause(until):
  if until is None or until < time.time():
    ToStdout("The watcher is not paused.")
  else:
    ToStdout("The watcher is paused until %s.", time.ctime(until))


def WatcherOps(opts, args):
  """Watcher operations.

  @param opts: the command line options selected by the user
  @type args: list
  @param args: should contain only one element, the subcommand
  @rtype: int
  @return: the desired exit code

  """
  command = args[0]
  client = GetClient()

  if command == "continue":
    client.SetWatcherPause(None)
    ToStdout("The watcher is no longer paused.")

  elif command == "pause":
    if len(args) < 2:
      raise errors.OpPrereqError("Missing pause duration", errors.ECODE_INVAL)

    result = client.SetWatcherPause(time.time() + ParseTimespec(args[1]))
    _ShowWatcherPause(result)

  elif command == "info":
    result = client.QueryConfigValues(["watcher_pause"])
    _ShowWatcherPause(result[0])

  else:
    raise errors.OpPrereqError("Command '%s' is not valid." % command,
                               errors.ECODE_INVAL)

  return 0


def _OobPower(opts, node_list, power):
  """Puts the node in the list to desired power state.

  @param opts: The command line options selected by the user
  @param node_list: The list of nodes to operate on
  @param power: True if they should be powered on, False otherwise
  @return: The success of the operation (none failed)

  """
  if power:
    command = constants.OOB_POWER_ON
  else:
    command = constants.OOB_POWER_OFF

  op = opcodes.OpOobCommand(node_names=node_list,
                            command=command,
                            ignore_status=True,
                            timeout=opts.oob_timeout,
                            power_delay=opts.power_delay)
  result = SubmitOpCode(op, opts=opts)
  errs = 0
  for node_result in result:
    (node_tuple, data_tuple) = node_result
    (_, node_name) = node_tuple
    (data_status, _) = data_tuple
    if data_status != constants.RS_NORMAL:
      assert data_status != constants.RS_UNAVAIL
      errs += 1
      ToStderr("There was a problem changing power for %s, please investigate",
               node_name)

  if errs > 0:
    return False

  return True


def _InstanceStart(opts, inst_list, start, no_remember=False):
  """Puts the instances in the list to desired state.

  @param opts: The command line options selected by the user
  @param inst_list: The list of instances to operate on
  @param start: True if they should be started, False for shutdown
  @param no_remember: If the instance state should be remembered
  @return: The success of the operation (none failed)

  """
  if start:
    opcls = opcodes.OpInstanceStartup
    text_submit, text_success, text_failed = ("startup", "started", "starting")
  else:
    opcls = compat.partial(opcodes.OpInstanceShutdown,
                           timeout=opts.shutdown_timeout,
                           no_remember=no_remember)
    text_submit, text_success, text_failed = ("shutdown", "stopped", "stopping")

  jex = JobExecutor(opts=opts)

  for inst in inst_list:
    ToStdout("Submit %s of instance %s", text_submit, inst)
    op = opcls(instance_name=inst)
    jex.QueueJob(inst, op)

  results = jex.GetResults()
  bad_cnt = len([1 for (success, _) in results if not success])

  if bad_cnt == 0:
    ToStdout("All instances have been %s successfully", text_success)
  else:
    ToStderr("There were errors while %s instances:\n"
             "%d error(s) out of %d instance(s)", text_failed, bad_cnt,
             len(results))
    return False

  return True


class _RunWhenNodesReachableHelper:
  """Helper class to make shared internal state sharing easier.

  @ivar success: Indicates if all action_cb calls were successful

  """
  def __init__(self, node_list, action_cb, node2ip, port, feedback_fn,
               _ping_fn=netutils.TcpPing, _sleep_fn=time.sleep):
    """Init the object.

    @param node_list: The list of nodes to be reachable
    @param action_cb: Callback called when a new host is reachable
    @type node2ip: dict
    @param node2ip: Node to ip mapping
    @param port: The port to use for the TCP ping
    @param feedback_fn: The function used for feedback
    @param _ping_fn: Function to check reachabilty (for unittest use only)
    @param _sleep_fn: Function to sleep (for unittest use only)

    """
    self.down = set(node_list)
    self.up = set()
    self.node2ip = node2ip
    self.success = True
    self.action_cb = action_cb
    self.port = port
    self.feedback_fn = feedback_fn
    self._ping_fn = _ping_fn
    self._sleep_fn = _sleep_fn

  def __call__(self):
    """When called we run action_cb.

    @raises utils.RetryAgain: When there are still down nodes

    """
    if not self.action_cb(self.up):
      self.success = False

    if self.down:
      raise utils.RetryAgain()
    else:
      return self.success

  def Wait(self, secs):
    """Checks if a host is up or waits remaining seconds.

    @param secs: The secs remaining

    """
    start = time.time()
    for node in self.down:
      if self._ping_fn(self.node2ip[node], self.port, timeout=_EPO_PING_TIMEOUT,
                       live_port_needed=True):
        self.feedback_fn("Node %s became available" % node)
        self.up.add(node)
        self.down -= self.up
        # If we have a node available there is the possibility to run the
        # action callback successfully, therefore we don't wait and return
        return

    self._sleep_fn(max(0.0, start + secs - time.time()))


def _RunWhenNodesReachable(node_list, action_cb, interval):
  """Run action_cb when nodes become reachable.

  @param node_list: The list of nodes to be reachable
  @param action_cb: Callback called when a new host is reachable
  @param interval: The earliest time to retry

  """
  client = GetClient()
  cluster_info = client.QueryClusterInfo()
  if cluster_info["primary_ip_version"] == constants.IP4_VERSION:
    family = netutils.IPAddress.family
  else:
    family = netutils.IP6Address.family

  node2ip = dict((node, netutils.GetHostname(node, family=family).ip)
                 for node in node_list)

  port = netutils.GetDaemonPort(constants.NODED)
  helper = _RunWhenNodesReachableHelper(node_list, action_cb, node2ip, port,
                                        ToStdout)

  try:
    return utils.Retry(helper, interval, _EPO_REACHABLE_TIMEOUT,
                       wait_fn=helper.Wait)
  except utils.RetryTimeout:
    ToStderr("Time exceeded while waiting for nodes to become reachable"
             " again:\n  - %s", "  - ".join(helper.down))
    return False


def _MaybeInstanceStartup(opts, inst_map, nodes_online,
                          _instance_start_fn=_InstanceStart):
  """Start the instances conditional based on node_states.

  @param opts: The command line options selected by the user
  @param inst_map: A dict of inst -> nodes mapping
  @param nodes_online: A list of nodes online
  @param _instance_start_fn: Callback to start instances (unittest use only)
  @return: Success of the operation on all instances

  """
  start_inst_list = []
  for (inst, nodes) in inst_map.items():
    if not (nodes - nodes_online):
      # All nodes the instance lives on are back online
      start_inst_list.append(inst)

  for inst in start_inst_list:
    del inst_map[inst]

  if start_inst_list:
    return _instance_start_fn(opts, start_inst_list, True)

  return True


def _EpoOn(opts, full_node_list, node_list, inst_map):
  """Does the actual power on.

  @param opts: The command line options selected by the user
  @param full_node_list: All nodes to operate on (includes nodes not supporting
                         OOB)
  @param node_list: The list of nodes to operate on (all need to support OOB)
  @param inst_map: A dict of inst -> nodes mapping
  @return: The desired exit status

  """
  if node_list and not _OobPower(opts, node_list, False):
    ToStderr("Not all nodes seem to get back up, investigate and start"
             " manually if needed")

  # Wait for the nodes to be back up
  action_cb = compat.partial(_MaybeInstanceStartup, opts, dict(inst_map))

  ToStdout("Waiting until all nodes are available again")
  if not _RunWhenNodesReachable(full_node_list, action_cb, _EPO_PING_INTERVAL):
    ToStderr("Please investigate and start stopped instances manually")
    return constants.EXIT_FAILURE

  return constants.EXIT_SUCCESS


def _EpoOff(opts, node_list, inst_map):
  """Does the actual power off.

  @param opts: The command line options selected by the user
  @param node_list: The list of nodes to operate on (all need to support OOB)
  @param inst_map: A dict of inst -> nodes mapping
  @return: The desired exit status

  """
  if not _InstanceStart(opts, inst_map.keys(), False, no_remember=True):
    ToStderr("Please investigate and stop instances manually before continuing")
    return constants.EXIT_FAILURE

  if not node_list:
    return constants.EXIT_SUCCESS

  if _OobPower(opts, node_list, False):
    return constants.EXIT_SUCCESS
  else:
    return constants.EXIT_FAILURE


def Epo(opts, args, cl=None, _on_fn=_EpoOn, _off_fn=_EpoOff,
        _confirm_fn=ConfirmOperation,
        _stdout_fn=ToStdout, _stderr_fn=ToStderr):
  """EPO operations.

  @param opts: the command line options selected by the user
  @type args: list
  @param args: should contain only one element, the subcommand
  @rtype: int
  @return: the desired exit code

  """
  if opts.groups and opts.show_all:
    _stderr_fn("Only one of --groups or --all are allowed")
    return constants.EXIT_FAILURE
  elif args and opts.show_all:
    _stderr_fn("Arguments in combination with --all are not allowed")
    return constants.EXIT_FAILURE

  if cl is None:
    cl = GetClient()

  if opts.groups:
    node_query_list = \
      itertools.chain(*cl.QueryGroups(args, ["node_list"], False))
  else:
    node_query_list = args

  result = cl.QueryNodes(node_query_list, ["name", "master", "pinst_list",
                                           "sinst_list", "powered", "offline"],
                         False)

  all_nodes = map(compat.fst, result)
  node_list = []
  inst_map = {}
  for (node, master, pinsts, sinsts, powered, offline) in result:
    if not offline:
      for inst in (pinsts + sinsts):
        if inst in inst_map:
          if not master:
            inst_map[inst].add(node)
        elif master:
          inst_map[inst] = set()
        else:
          inst_map[inst] = set([node])

    if master and opts.on:
      # We ignore the master for turning on the machines, in fact we are
      # already operating on the master at this point :)
      continue
    elif master and not opts.show_all:
      _stderr_fn("%s is the master node, please do a master-failover to another"
                 " node not affected by the EPO or use --all if you intend to"
                 " shutdown the whole cluster", node)
      return constants.EXIT_FAILURE
    elif powered is None:
      _stdout_fn("Node %s does not support out-of-band handling, it can not be"
                 " handled in a fully automated manner", node)
    elif powered == opts.on:
      _stdout_fn("Node %s is already in desired power state, skipping", node)
    elif not offline or (offline and powered):
      node_list.append(node)

  if not (opts.force or _confirm_fn(all_nodes, "nodes", "epo")):
    return constants.EXIT_FAILURE

  if opts.on:
    return _on_fn(opts, all_nodes, node_list, inst_map)
  else:
    return _off_fn(opts, node_list, inst_map)


commands = {
  "init": (
    InitCluster, [ArgHost(min=1, max=1)],
    [BACKEND_OPT, BACKEND_STORAGE, CP_SIZE_OPT, ENABLED_HV_OPT,
     GLOBAL_FILEDIR_OPT, HVLIST_OPT, MAC_PREFIX_OPT, MASTER_NETDEV_OPT,
     MASTER_NETMASK_OPT, NIC_PARAMS_OPT, NOLVM_STORAGE_OPT, VG_NAME_OPT,
     NOMODIFY_ETCHOSTS_OPT, NOMODIFY_SSH_SETUP_OPT, SECONDARY_IP_OPT,
     MAINTAIN_NODE_HEALTH_OPT, UIDPOOL_OPT, DRBD_HELPER_OPT, NODRBD_STORAGE_OPT,
     DEFAULT_IALLOCATOR_OPT, PRIMARY_IP_VERSION_OPT, PREALLOC_WIPE_DISKS_OPT,
     NODE_PARAMS_OPT, GLOBAL_SHARED_FILEDIR_OPT, USE_EXTERNAL_MIP_SCRIPT,
     DISK_PARAMS_OPT, HV_STATE_OPT, DISK_STATE_OPT] + INSTANCE_POLICY_OPTS,
    "[opts...] <cluster_name>", "Initialises a new cluster configuration"),
  "destroy": (
    DestroyCluster, ARGS_NONE, [YES_DOIT_OPT],
    "", "Destroy cluster"),
  "rename": (
    RenameCluster, [ArgHost(min=1, max=1)],
    [FORCE_OPT, DRY_RUN_OPT],
    "<new_name>",
    "Renames the cluster"),
  "redist-conf": (
    RedistributeConfig, ARGS_NONE, [SUBMIT_OPT, DRY_RUN_OPT, PRIORITY_OPT],
    "", "Forces a push of the configuration file and ssconf files"
    " to the nodes in the cluster"),
  "verify": (
    VerifyCluster, ARGS_NONE,
    [VERBOSE_OPT, DEBUG_SIMERR_OPT, ERROR_CODES_OPT, NONPLUS1_OPT,
     DRY_RUN_OPT, PRIORITY_OPT, NODEGROUP_OPT, IGNORE_ERRORS_OPT],
    "", "Does a check on the cluster configuration"),
  "verify-disks": (
    VerifyDisks, ARGS_NONE, [PRIORITY_OPT],
    "", "Does a check on the cluster disk status"),
  "repair-disk-sizes": (
    RepairDiskSizes, ARGS_MANY_INSTANCES, [DRY_RUN_OPT, PRIORITY_OPT],
    "[instance...]", "Updates mismatches in recorded disk sizes"),
  "master-failover": (
    MasterFailover, ARGS_NONE, [NOVOTING_OPT, FORCE_FAILOVER],
    "", "Makes the current node the master"),
  "master-ping": (
    MasterPing, ARGS_NONE, [],
    "", "Checks if the master is alive"),
  "version": (
    ShowClusterVersion, ARGS_NONE, [],
    "", "Shows the cluster version"),
  "getmaster": (
    ShowClusterMaster, ARGS_NONE, [],
    "", "Shows the cluster master"),
  "copyfile": (
    ClusterCopyFile, [ArgFile(min=1, max=1)],
    [NODE_LIST_OPT, USE_REPL_NET_OPT, NODEGROUP_OPT],
    "[-n node...] <filename>", "Copies a file to all (or only some) nodes"),
  "command": (
    RunClusterCommand, [ArgCommand(min=1)],
    [NODE_LIST_OPT, NODEGROUP_OPT, SHOW_MACHINE_OPT, FAILURE_ONLY_OPT],
    "[-n node...] <command>", "Runs a command on all (or only some) nodes"),
  "info": (
    ShowClusterConfig, ARGS_NONE, [ROMAN_OPT],
    "[--roman]", "Show cluster configuration"),
  "list-tags": (
    ListTags, ARGS_NONE, [], "", "List the tags of the cluster"),
  "add-tags": (
    AddTags, [ArgUnknown()], [TAG_SRC_OPT, PRIORITY_OPT, SUBMIT_OPT],
    "tag...", "Add tags to the cluster"),
  "remove-tags": (
    RemoveTags, [ArgUnknown()], [TAG_SRC_OPT, PRIORITY_OPT, SUBMIT_OPT],
    "tag...", "Remove tags from the cluster"),
  "search-tags": (
    SearchTags, [ArgUnknown(min=1, max=1)], [PRIORITY_OPT], "",
    "Searches the tags on all objects on"
    " the cluster for a given pattern (regex)"),
  "queue": (
    QueueOps,
    [ArgChoice(min=1, max=1, choices=["drain", "undrain", "info"])],
    [], "drain|undrain|info", "Change queue properties"),
  "watcher": (
    WatcherOps,
    [ArgChoice(min=1, max=1, choices=["pause", "continue", "info"]),
     ArgSuggest(min=0, max=1, choices=["30m", "1h", "4h"])],
    [],
    "{pause <timespec>|continue|info}", "Change watcher properties"),
  "modify": (
    SetClusterParams, ARGS_NONE,
    [BACKEND_OPT, CP_SIZE_OPT, ENABLED_HV_OPT, HVLIST_OPT, MASTER_NETDEV_OPT,
     MASTER_NETMASK_OPT, NIC_PARAMS_OPT, NOLVM_STORAGE_OPT, VG_NAME_OPT,
     MAINTAIN_NODE_HEALTH_OPT, UIDPOOL_OPT, ADD_UIDS_OPT, REMOVE_UIDS_OPT,
     DRBD_HELPER_OPT, NODRBD_STORAGE_OPT, DEFAULT_IALLOCATOR_OPT,
     RESERVED_LVS_OPT, DRY_RUN_OPT, PRIORITY_OPT, PREALLOC_WIPE_DISKS_OPT,
     NODE_PARAMS_OPT, USE_EXTERNAL_MIP_SCRIPT, DISK_PARAMS_OPT, HV_STATE_OPT,
     DISK_STATE_OPT, SUBMIT_OPT] +
    INSTANCE_POLICY_OPTS,
    "[opts...]",
    "Alters the parameters of the cluster"),
  "renew-crypto": (
    RenewCrypto, ARGS_NONE,
    [NEW_CLUSTER_CERT_OPT, NEW_RAPI_CERT_OPT, RAPI_CERT_OPT,
     NEW_CONFD_HMAC_KEY_OPT, FORCE_OPT,
     NEW_CLUSTER_DOMAIN_SECRET_OPT, CLUSTER_DOMAIN_SECRET_OPT,
     NEW_SPICE_CERT_OPT, SPICE_CERT_OPT, SPICE_CACERT_OPT],
    "[opts...]",
    "Renews cluster certificates, keys and secrets"),
  "epo": (
    Epo, [ArgUnknown()],
    [FORCE_OPT, ON_OPT, GROUPS_OPT, ALL_OPT, OOB_TIMEOUT_OPT,
     SHUTDOWN_TIMEOUT_OPT, POWER_DELAY_OPT],
    "[opts...] [args]",
    "Performs an emergency power-off on given args"),
  "activate-master-ip": (
    ActivateMasterIp, ARGS_NONE, [], "", "Activates the master IP"),
  "deactivate-master-ip": (
    DeactivateMasterIp, ARGS_NONE, [CONFIRM_OPT], "",
    "Deactivates the master IP"),
  }


#: dictionary with aliases for commands
aliases = {
  "masterfailover": "master-failover",
  "show": "info",
}


def Main():
  return GenericMain(commands, override={"tag_type": constants.TAG_CLUSTER},
                     aliases=aliases)
