# Rekall Memory Forensics
#
# Copyright 2016 Google Inc. All Rights Reserved.
#
# Authors:
# Michael Cohen <scudette@google.com>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or (at
# your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA 02111-1307 USA
#

"""This module adds support for incident response to Rekall."""

__author__ = "Michael Cohen <scudette@google.com>"
import platform
import os
import re
import stat

from efilter.protocols import associative
from efilter.protocols import structured

from rekall import addrspace
from rekall import obj
from rekall import registry
from rekall import utils
from rekall import plugin
from rekall.plugins.overlays import basic


# Will be registered by OS specific implementations.
APIProcessAddressSpace = None
APIProfile = None


class APIDummyPhysicalAddressSpace(addrspace.BaseAddressSpace):
    __image = True

    def __init__(self, base=None, session=None, **kwargs):
        self.as_assert(base is None)
        self.as_assert(session.GetParameter("live_mode") == "API")

        with session:
            session.SetParameter("profile", APIBaseProfile(session=session))

        super(APIDummyPhysicalAddressSpace, self).__init__(
            session=session, **kwargs)


class APIBaseProfile(obj.Profile):
    """A class representing the profile for IR (live) analysis."""

    # This profile is used for API based analysis.
    METADATA = dict(live=True, type="API",
                    os=platform.system())


class FileSpec(utils.AttributeDict):
    """Specification of a file path."""

    __metaclass__ = registry.UniqueObjectIdMetaclass

    def __init__(self, filename, filesystem=u"API", path_sep="/"):
        super(FileSpec, self).__init__()
        self.filesystem = filesystem
        self.path_sep = path_sep

        if isinstance(filename, FileSpec):
            # Copy the other file spec.
            self.name = filename.name
            self.filesystem = filename.filesystem
            self.path_sep = filename.path_sep

        elif isinstance(filename, basestring):
            self.name = unicode(filename)

        else:
            raise TypeError("Filename must be a string or file spec.")

    def components(self):
        return filter(None, self.name.split(self.path_sep))

    def os_path(self):
        # Handle canonical paths of the form /c:/windows/ -> c:/windows
        # So they can be opened by the OS APIs.

        # Try to split the path into a drive and path component.
        m = re.match("\\%s([a-zA-Z]):(.*)" % self.path_sep, self.name)
        if m:
            # Make sure that path is never relative. In this code we
            # always want to deal with absolute paths so they need leading "/".
            path = m.group(2)
            if not path.startswith(self.path_sep):
                path = self.path_sep + path

            drive = m.group(1)
            return "%s:%s" % (drive, path)

        return self.name

    def __str__(self):
        return self.name

    def add(self, component):
        if self.name == self.path_sep:
            path = self.name + component
        else:
            path = self.name + self.path_sep + component

        return FileSpec(filename=path, path_sep=self.path_sep,
                        filesystem=self.filesystem)


class User(utils.AttributeDict):
    """A class to represent a user."""

    @classmethod
    @registry.memoize
    def from_uid(cls, uid):
        result = cls()

        # Only available on Linux.
        try:
            import pwd

            record = pwd.getpwuid(uid)
            result.uid = uid
            result.username = record.pw_name
            result.homedir = record.pw_dir
            result.shell = record.pw_shell
        except (KeyError, ImportError):
            pass

        return result

    def __unicode__(self):
        if self.username:
            return u"%s (%s)" % (self.username, self.uid)
        elif self.uid is not None:
            return unicode(self.uid)

        return ""


class Group(utils.AttributeDict):
    """A class to represent a user."""

    @classmethod
    @registry.memoize
    def from_gid(cls, gid):
        result = cls()

        # Only available on Linux.
        try:
            import grp

            record = grp.getgrgid(gid)
            result.gid = gid
            result.group_name = record.gr_name
        except (KeyError, ImportError):
            pass

        return result

    def __unicode__(self):
        if self.group_name:
            return u"%s (%s)" % (self.group_name, self.gid)

        elif self.gid is not None:
            return unicode(self.gid)

        return ""


class FileInformation(utils.AttributeDict):
    """An object representing a file on disk.

    This FileInformation uses the API to read data about the file.
    """

    session = None

    def __init__(self, session=None, filename=None, **kwargs):
        super(FileInformation, self).__init__(**kwargs)
        # Ensure the filename is a filespec.
        self.filename = FileSpec(filename)
        self.session = session

    @classmethod
    def from_stat(cls, filespec, session=None):
        filespec = FileSpec(filespec)
        result = FileInformation(filename=filespec, session=session)

        try:
            s = os.stat(filespec.os_path())
        except (IOError, OSError) as e:
            return obj.NoneObject("Unable to stat %s", e)

        result.st_mode = Permissions(s.st_mode)
        result.st_ino = s.st_ino
        result.st_size = s.st_size
        result.st_dev = s.st_dev
        result.st_nlink = s.st_nlink
        result.st_uid = User.from_uid(s.st_uid)
        result.st_gid = Group.from_gid(s.st_gid)
        result.st_mtime = basic.UnixTimeStamp(
            name="st_mtime", value=s.st_mtime, session=session)
        result.st_atime = basic.UnixTimeStamp(
            name="st_atime", value=s.st_atime, session=session)
        result.st_ctime = basic.UnixTimeStamp(
            name="st_ctime", value=s.st_ctime, session=session)

        return result

    def open(self):
        try:
            return open(self.filename.os_path(), "rb")
        except (IOError, OSError) as e:
            return obj.NoneObject("Unable to open file: %s", e)

    def list(self):
        """If this is a directory return a list of children."""
        if not self.st_mode.is_dir():
            return

        filename = self.filename.os_path()
        try:
            for name in os.listdir(filename):
                full_path = os.path.join(filename, name)
                item = self.from_stat(full_path, session=self.session)
                if item:
                    yield item
        except (OSError, IOError):
            pass


class Permissions(object):

    """An object to represent permissions."""
    __metaclass__ = registry.UniqueObjectIdMetaclass

    # Taken from Python3.3's stat.filemode.
    _filemode_table = (
        ((stat.S_IFLNK, "l"),
         (stat.S_IFREG, "-"),
         (stat.S_IFBLK, "b"),
         (stat.S_IFDIR, "d"),
         (stat.S_IFCHR, "c"),
         (stat.S_IFIFO, "p")),

        ((stat.S_IRUSR, "r"),),
        ((stat.S_IWUSR, "w"),),
        ((stat.S_IXUSR|stat.S_ISUID, "s"),
         (stat.S_ISUID, "S"),
         (stat.S_IXUSR, "x")),

        ((stat.S_IRGRP, "r"),),
        ((stat.S_IWGRP, "w"),),
        ((stat.S_IXGRP|stat.S_ISGID, "s"),
         (stat.S_ISGID, "S"),
         (stat.S_IXGRP, "x")),

        ((stat.S_IROTH, "r"),),
        ((stat.S_IWOTH, "w"),),
        ((stat.S_IXOTH|stat.S_ISVTX, "t"),
         (stat.S_ISVTX, "T"),
         (stat.S_IXOTH, "x"))
    )

    def __init__(self, value):
        self.value = int(value)

    def filemode(self, mode):
        """Convert a file's mode to a string of the form '-rwxrwxrwx'."""
        perm = []
        for table in self._filemode_table:
            for bit, char in table:
                if mode & bit == bit:
                    perm.append(char)
                    break
            else:
                perm.append("-")
        return "".join(perm)

    def __int__(self):
        return self.value

    def __str__(self):
        return self.filemode(self.value)

    def is_dir(self):
        return stat.S_ISDIR(self.value)


class AbstractIRCommandPlugin(plugin.TypedProfileCommand,
                              plugin.ProfileCommand):
    """A base class for all IR plugins.

    IR Plugins are only active when the session is live.
    """

    __abstract = True

    PROFILE_REQUIRED = False

    @classmethod
    def is_active(cls, session):
        return (super(AbstractIRCommandPlugin, cls).is_active(session) and
                session.GetParameter("live_mode") in ["API", "Memory"])


class AbstractAPICommandPlugin(plugin.TypedProfileCommand,
                               plugin.ProfileCommand):
    """A base class for all API access plugins.

    IR Plugins are only active when the session is live.
    """

    __abstract = True

    PROFILE_REQUIRED = False

    @classmethod
    def is_active(cls, session):
        return (super(AbstractAPICommandPlugin, cls).is_active(session) and
                session.GetParameter("live_mode") == "API")


FILE_SPEC_DISPATCHER = dict(API=FileInformation)

def FileFactory(filename, session=None):
    """Return the correct FileInformation class from the filename.

    Currently we only support OS API accessible files, but in the future we will
    also support NTFS files.
    """
    # Ensure this is a valid file spec.
    filespec = FileSpec(filename)

    handler = FILE_SPEC_DISPATCHER.get(filespec.filesystem)
    if handler:
        try:
            return handler.from_stat(filespec, session=session)
        except (IOError, OSError):

            return obj.NoneObject("Cant open %s", filespec)

    raise RuntimeError("Unsupported file spec type %s" %
                       filespec.filesystem)


# Efilter Glue code.
associative.IAssociative.implement(
    for_type=FileInformation,
    implementations={
        associative.select: getattr,
        associative.reflect_runtime_key: structured.reflect_runtime_member,
        associative.getkeys_runtime: structured.getmembers_runtime
    }
)

associative.IAssociative.implement(
    for_type=FileSpec,
    implementations={
        associative.select: getattr,
        associative.reflect_runtime_key: lambda c: str,
        associative.getkeys_runtime: lambda _: ["name"]
    }
)
