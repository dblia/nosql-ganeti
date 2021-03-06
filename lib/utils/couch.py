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

"""Utility functions for I/O with CouchDB.

"""

# pylint: disable=W0703
# W0703: Catching too general exception Exception

import logging
import couchdb.client

from ganeti import errors
from ganeti import constants


def URIAuth(user_info, reg_name, port):
  """Creates the authority value within a uri.

  URIAuth, example://anonymous@www.haskell.org:42

  @type user_info: string
  @param user_info: user info (e.g. anonymous)
  @type reg_name: string
  @param reg_name: registration name (e.g. www.google.com)
  @type port: int
  @param port: port number (e.g. 5984)
  @rtype: string
  @return: a URI authority value (e.g. //anonymous@www.google.com:5984)

  """
  if user_info:
    user_info = "".join((user_info, "@"))

  assert reg_name and port, \
      "Registration name and port number must be given"

  port = "".join((":", str(port)))

  return "".join(("//", user_info, reg_name, port))


def URICreate(scheme, auth, path="", query="", fragment=""):
  """Returns a general universal resource identifier.

  URI, example: foo://anonymous@www.haskell.org:42/ghc?query#frag

  @type scheme: string
  @param scheme: uri scheme (e.g. "http", "ftp" etc)
  @type auth: string
  @param auth: uri authentication token
  @type path: string
  @param path: absolute path (e.g. /path/to/resource.txt)
  @type query: string
  @param query: query (e.g. ?query)
  @type fragment: string
  @param fragment: references (e.g. #frag)
  @rtype: string
  @return: the URI absolute path

  """
  assert scheme and auth, \
      "Scheme and URIAuth must be given"

  scheme = "".join((scheme, ":"))

  return "".join((scheme, auth, path, query, fragment))


def DeleteDB(db_name, host_ip, port):
  """Delete a database.

  This function deletes the database for the host ip
  given. Throws an exception if the database doesn't
  exists.

  @type db_name: str
  @param db_name: the database name which i will create
  @type host_ip: str
  @param host_ip: the host ip of the couchdb server
  @type port: int
  @param port: port number

  """
  auth = URIAuth("", host_ip, port)
  uri = URICreate("http", auth)

  try:
    server = couchdb.client.Server(uri)
    server.delete(db_name)
  except Exception:
    msg = ("The database (%s) in the following host IP (%s)"
           " could not be deleted: %s" % (db_name, host_ip,
           errors.ECODE_NOENT))
    raise errors.OpPrereqError(msg)


def CreateDB(db_name, host_ip, port):
  """Creates a new database instance.

  This function returns a database for the host ip given.
  Throws an exception if the database already exists.

  @type host_ip: str
  @param host_ip: the host ip of the couchdb server
  @type port: int
  @param port: port number
  @type db_name: str
  @param db_name: the database name which i will create
  @rtype: L{couchdb.client.Database}
  @return: the database instance

  """
  auth = URIAuth("", host_ip, port)
  uri = URICreate("http", auth)
  server = couchdb.client.Server(uri)

  try:
    db = server.create(db_name)
  except Exception:
    msg = ("The database (%s) could not be created in the following"
           " host IP (%s): %s" % (db_name, host_ip, errors.ECODE_EXISTS))
    raise errors.OpPrereqError(msg)
  else:
    return db


def GetDBInstance(db_name, host_ip, port):
  """Returns a database instance.

  If the database doesn't exists throws an exception.

  @type host_ip: str
  @param host_ip: the host ip of the couchdb server
  @type port: int
  @param port: port number
  @type db_name: str
  @param db_name: the database name which i will create
  @rtype: L{couchdb.client.Database}
  @return: the database instance

  """
  auth = URIAuth("", host_ip, port)
  uri = URICreate("http", auth)
  server = couchdb.client.Server(uri)

  try:
    db = server[db_name]
  except Exception:
    msg = ("The database name given (%s), does not belong to the host"
           " IP (%s) or CouchDB server is down: %s" % (db_name, host_ip,
           errors.ECODE_NOENT))
    raise errors.OpPrereqError(msg)
  else:
    return db


def GetDocument(db_name, doc_id):
  """Return the document with the specified ID.

  @type db_name: L{couchdb.client.Database} instance
  @param db_name: the database name
  @type doc_id: str
  @param doc_id: document's ID
  @rtype: L{couchdb.client.Document} instance
  @return: the requested document

  """
  try:
    result = db_name.get(doc_id)
  except Exception:
    # This exception happens due to a hard CouchDB server shutdown
    raise errors.JobQueueError("CouchDB is down, refusing job")

  # The result can be either the requested document, or 'None' if no document
  # with the ID was found
  if not result:
    msg = ("The document %s does not exist in database %s." % (doc_id,
           db_name.name))
    logging.info(msg)

  return result


def WriteDocument(db_name, data):
  """(Over)write a document in the database given.

  @type db_name: L{couchdb.client.Database} instance
  @param db_name: the database name
  @type data: str
  @param data: contents of the document
  @rtype: L{couchdb.client.Document}'s '_rev' field
  @return: document's '_rev' field

  """
  try:
    (_, doc_rev) = db_name.save(data)
  except Exception:
    # Handle conflict when document exists in the db given
    try:
      new_doc = GetDocument(db_name, data["_id"])
      # Update data '_rev' field
      data["_rev"] = new_doc["_rev"]
      # Save it
      (_, doc_rev) = db_name.save(data)
    except Exception:
      # This exception happens due to a hard CouchDB server shutdown
      raise errors.JobQueueError("CouchDB is down, refusing job")

  return doc_rev


def DeleteDocument(db_name, data, purge=False):
  """Deletes a document from the database given.

  NOTE: delete does not remove the document completely from the database. It
  simply adds a new document with a new revision and the '_deleted' attribute
  set to True to the database. Old revisions are still accessible and the
  document will be replicated.
  On contrast, purged documents do not leave any meta-data in the storage and
  are not replicated.

  @type db_name: L{couchdb.client.Database} instance
  @param db_name: the database name
  @type data: str
  @param data: contents of the document
  @type purge: boolean
  @param purge: performs complete removing of the given document

  """
  try:
    if purge:
      db_name.purge([data])
      # In case of later checks in purged docs keep the output format.
      #result = db_name.purge([data])
      #result["purge_seq"]
      #purged = result["purged"]
      #_id = purged.keys()[0]
      #_rev = purged[_id]
    else:
      db_name.delete(data)
  except Exception, err:
    msg = ("The document '%s' hasn't found in database '%s', or a conflict"
           " arise: %s" % (data["_id"], db_name, err))
    raise errors.OpPrereqError(msg)


def ViewExec(db_name, view_name, include_docs=False):
  """Execute a predefined view.

  @type db_name: L{couchdb.client.Database} instance
  @param db_name: the database name
  @type view_name: str
  @param view_name: the name of the view
  @type include_docs: boolean
  @param include_docs: fetch and include the documents in the view result
  @rtype: L{couchdb.client.ViewResults}
  @return: the view results

  """
  try:
    return db_name.view(view_name, include_docs=include_docs)
  except Exception:
    # This exception happens due to a hard CouchDB server shutdown
    raise errors.OpPrereqError("Error while executing a view function.")


def BulkUpdateDocs(db_name, documents):
  """Perform a bulk update or insertion of the given documents using a single
  HTTP request.

  Any check to the result of the bulk update is left to the caller method.

  @type db_name: L{couchdb.client.Database} instance
  @param db_name: the database name
  @type documents: list of documents
  @param documents: documents to be updated
  @rtype: list of tuples '(boolean, _id, _rev)'

  """
  try:
    return db_name.update(documents)
  except Exception:
    # This exception happens due to a hard CouchDB server shutdown
    raise errors.JobQueueError("Error during bulk update")


def InstRename(db_name, new_doc, old_doc):
  """Update the data of the old document to that of the new one.

  @type db_name: L{couchdb.client.Database} instance
  @param db_name: the database name
  @type new_doc: str
  @param new_doc: contents of the document
  @type old_doc: str
  @param old_doc: contents of the document

  """
  # Remove previous revision to avoid conflicts.
  new_doc.pop("_rev")
  # Mark the old document for removal.
  old_doc["_deleted"] = True

  try:
    result = BulkUpdateDocs(db_name, [new_doc, old_doc])
  except Exception:
    # This exception happens due to a hard CouchDB server shutdown
    raise errors.JobQueueError("CouchDB error while renaming an instance")

  for success, _id, _rev in result:
    if success and _id == new_doc["_id"]:
      return _rev

  raise errors.OpPrereqError("Error while renaming instance '%s' to '%s'." %
                             (old_doc["_id"], new_doc["_id"]))


def UnlockedReplicateSetup(host_ip, node_ip, db_name, replicate):
  """This function enables or disables the replication
  between the master node and a new master candidate,
  depending in the replicate value given.

  This function should only be called when the config or
  queue lock are held.

  @type host_ip: string
  @param host_ip: master node's ip
  @type node_ip: string
  @param node_ip: candidate node's ip
  @type db_name: string
  @param db_name: database name to be replicated
  @type replicate: bool
  @param replicate: enable or disable continous replication

  """
  cand_auth = URIAuth("", node_ip, constants.DEFAULT_COUCHDB_PORT)
  master_auth = URIAuth("", host_ip, constants.DEFAULT_COUCHDB_PORT)
  cand_url = URICreate("http", cand_auth, db_name)
  master_url = URICreate("http", master_auth, db_name)

  # CouchDB replication documents have a special format,
  # (e.g from_<source>_to_<target>) in order to be clear
  # and to be handled easily.
  repl_doc_id = "".join(("from_", master_url, "_to_", cand_url))
  try:
    repl_db = GetDBInstance("_replicator", host_ip,
                            constants.DEFAULT_COUCHDB_PORT)
    # replicate: true.
    # That means that the candidate role has changes, so we have to
    # delete the replication document from the _replicator db.
    if replicate:
      doc = GetDocument(repl_db, repl_doc_id)
      DeleteDocument(repl_db, doc)
    else:
      repl_doc = {"source": master_url, "target": cand_url,
                  "continuous": True, "create_target": True}
      repl_db[repl_doc_id] = repl_doc
  except Exception, err:
    msg = ("Replication from source host %s, to target host %s failed: %s. %s"
           % (master_url, cand_url, err, errors.ECODE_FAULT))
    raise errors.OpPrereqError(msg)
  else:
    return True


def MasterFailoverDbs(old_master_ip, new_master_ip, db_name):
  """Moves replication tasks for the db name given from the
  old master node to the new master node.

  @type old_master_ip: str
  @param old_master_ip: old master's ip
  @type new_master_ip: str
  @param new_master_ip: new master's ip
  @type db_name: L{couchdb.client.Database} instance
  @param db_name: database name to be replicated

  """
  # FIXME: use util functions to build the uris and get the dbs
  old_url = ["http://", old_master_ip, ":", str(constants.DEFAULT_COUCHDB_PORT)]
  new_url = ["http://", new_master_ip, ":", str(constants.DEFAULT_COUCHDB_PORT)]
  old_server = couchdb.client.Server("".join(old_url))
  new_server = couchdb.client.Server("".join(new_url))

  old_url.append(db_name)
  new_url.append(db_name)

  old_repl_db = old_server[constants.REPLICATOR_DB]
  new_repl_db = new_server[constants.REPLICATOR_DB]

  new_source = "".join(new_url)
  old_source = "".join(old_url)

  for task in old_server.tasks():
    source = task["source"]
    target = task["target"]
    if (db_name in source) and (db_name in target):
      # Delete old replication document
      old_repl_doc_id = "".join(["from_", source, "_to_", target])
      doc = GetDocument(old_repl_db, old_repl_doc_id)
      DeleteDocument(old_repl_db, doc)

      # Create a new replication document
      if target == new_source:
        target = old_source
      new_repl_doc_id = "".join(["from_", new_source, "_to_", target])
      repl_doc = {"source": new_source, "target": target,
                  "continuous": True, "create_target": True}
      new_repl_db[new_repl_doc_id] = repl_doc
