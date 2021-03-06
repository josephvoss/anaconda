#
# Copyright (C) 2009-2017  Red Hat, Inc.
#
# This copyrighted material is made available to anyone wishing to use,
# modify, copy, or redistribute it subject to the terms and conditions of
# the GNU General Public License v.2, or (at your option) any later version.
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY expressed or implied, including the implied warranties of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General
# Public License for more details.  You should have received a copy of the
# GNU General Public License along with this program; if not, write to the
# Free Software Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA
# 02110-1301, USA.  Any Red Hat trademarks that are incorporated in the
# source code or documentation are not subject to the GNU General Public
# License and may only be used or replicated with the express permission of
# Red Hat, Inc.
#
# Red Hat Author(s): David Lehman <dlehman@redhat.com>
#

"""This module provides storage functions related to OS installation."""

import os
import parted

import gi
gi.require_version("BlockDev", "2.0")

from gi.repository import BlockDev as blockdev

from pykickstart.constants import AUTOPART_TYPE_LVM, NVDIMM_ACTION_USE, NVDIMM_ACTION_RECONFIGURE

from blivet import arch, udev
from blivet import util as blivet_util
from blivet.blivet import Blivet
from blivet.storage_log import log_exception_info
from blivet.devices import MDRaidArrayDevice, PartitionDevice, BTRFSSubVolumeDevice, TmpFSDevice, \
    LVMLogicalVolumeDevice, LVMVolumeGroupDevice, BTRFSDevice
from blivet.errors import StorageError, UnknownSourceDeviceError
from blivet.formats import get_format
from blivet.flags import flags as blivet_flags
from blivet.iscsi import iscsi
from blivet.static_data import nvdimm
from blivet.size import Size
from blivet.devicelibs.crypto import DEFAULT_LUKS_VERSION

from pyanaconda.core import util
from pyanaconda.anaconda_logging import program_log_lock
from pyanaconda.bootloader import get_bootloader
from pyanaconda.core.configuration.anaconda import conf
from pyanaconda.core.constants import shortProductName, CLEAR_PARTITIONS_NONE, \
    CLEAR_PARTITIONS_LINUX, CLEAR_PARTITIONS_ALL, CLEAR_PARTITIONS_LIST, CLEAR_PARTITIONS_DEFAULT
from pyanaconda.errors import errorHandler as error_handler, ERROR_RAISE
from pyanaconda.flags import flags
from pyanaconda.bootloader.execution import BootloaderExecutor
from pyanaconda.platform import platform as _platform
from pyanaconda.storage.fsset import FSSet
from pyanaconda.storage.partitioning import get_full_partitioning_requests
from pyanaconda.storage.root import find_existing_installations
from pyanaconda.modules.common.constants.services import NETWORK, STORAGE
from pyanaconda.modules.common.constants.objects import DISK_SELECTION, DISK_INITIALIZATION, \
    AUTO_PARTITIONING, ZFCP, FCOE

import logging
log = logging.getLogger("anaconda.storage")


def enable_installer_mode():
    """ Configure the module for use by anaconda (OS installer). """
    blivet_util.program_log_lock = program_log_lock

    # always enable the debug mode when in the installer mode so that we
    # have more data in the logs for rare cases that are hard to reproduce
    blivet_flags.debug = True

    # We don't want image installs writing backups of the *image* metadata
    # into the *host's* /etc/lvm. This can get real messy on build systems.
    if conf.target.is_image:
        blivet_flags.lvm_metadata_backup = False

    blivet_flags.auto_dev_updates = True
    blivet_flags.selinux_reset_fcon = True
    blivet_flags.keep_empty_ext_partitions = False
    blivet_flags.discard_new = True

    udev.device_name_blacklist = [r'^mtd', r'^mmcblk.+boot', r'^mmcblk.+rpmb', r'^zram', '^ndblk']


def update_blivet_flags():
    """
    Set installer-specific flags. This changes blivet default flags by
    either flipping the original value, or it assigns the flag value
    based on anaconda settings that are passed in.
    """
    blivet_flags.selinux = conf.security.selinux
    blivet_flags.dmraid = conf.storage.dmraid
    blivet_flags.ibft = conf.storage.ibft
    blivet_flags.multipath_friendly_names = conf.storage.multipath_friendly_names
    blivet_flags.allow_imperfect_devices = conf.storage.allow_imperfect_devices


class StorageDiscoveryConfig(object):

    """ Class to encapsulate various detection/initialization parameters. """

    def __init__(self):

        # storage configuration variables
        self.clear_part_type = CLEAR_PARTITIONS_DEFAULT
        self.clear_part_disks = []
        self.clear_part_devices = []
        self.initialize_disks = False
        self.protected_dev_specs = []
        self.zero_mbr = False

        # Whether clear_partitions removes scheduled/non-existent devices and
        # disklabels depends on this flag.
        self.clear_non_existent = False

    def update(self, *args, **kwargs):
        """Update configuration."""
        disk_init_proxy = STORAGE.get_proxy(DISK_INITIALIZATION)

        self.clear_part_type = disk_init_proxy.InitializationMode
        self.clear_part_disks = disk_init_proxy.DrivesToClear
        self.clear_part_devices = disk_init_proxy.DevicesToClear
        self.initialize_disks = disk_init_proxy.InitializeLabelsEnabled
        self.zero_mbr = disk_init_proxy.FormatUnrecognizedEnabled


class InstallerStorage(Blivet):
    """ Top-level class for managing installer-related storage configuration. """
    def __init__(self, ksdata=None):
        """
            :keyword ksdata: kickstart data store
            :type ksdata: :class:`pykickstart.Handler`
        """
        super().__init__()
        self.do_autopart = False
        self.encrypted_autopart = False
        self.encryption_cipher = None
        self.escrow_certificates = {}

        self.autopart_escrow_cert = None
        self.autopart_add_backup_passphrase = False
        self.autopart_requests = []

        self._default_boot_fstype = None

        self.ksdata = ksdata
        self._bootloader = None
        self.config = StorageDiscoveryConfig()
        self.autopart_type = AUTOPART_TYPE_LVM

        self.__luks_devs = {}
        self.fsset = FSSet(self.devicetree)
        self._free_space_snapshot = None
        self.live_backing_device = None

        self._short_product_name = shortProductName
        self._default_luks_version = DEFAULT_LUKS_VERSION

        self._autopart_luks_version = None
        self.autopart_pbkdf_args = None

    def copy(self):
        """Copy the storage.

        Kickstart data are not copied.
        """
        # Disable the kickstart data.
        old_data = self.ksdata
        self.ksdata = None

        # Create the copy.
        new_storage = super().copy()

        # Recover the kickstart data.
        self.ksdata = old_data
        new_storage.ksdata = old_data
        return new_storage

    def do_it(self, callbacks=None):
        """
        Commit queued changes to disk.

        :param callbacks: callbacks to be invoked when actions are executed
        :type callbacks: return value of the :func:`blivet.callbacks.create_new_callbacks_

        """
        super().do_it(callbacks=callbacks)

        # now set the boot partition's flag
        if self.bootloader and not self.bootloader.skip_bootloader:
            if self.bootloader.stage2_bootable:
                boot = self.boot_device
            else:
                boot = self.bootloader_device

            if boot.type == "mdarray":
                boot_devs = boot.parents
            else:
                boot_devs = [boot]

            for dev in boot_devs:
                if not hasattr(dev, "bootable"):
                    log.info("Skipping %s, not bootable", dev)
                    continue

                # Dos labels can only have one partition marked as active
                # and unmarking ie the windows partition is not a good idea
                skip = False
                if dev.disk.format.parted_disk.type == "msdos":
                    for p in dev.disk.format.parted_disk.partitions:
                        if p.type == parted.PARTITION_NORMAL and \
                           p.getFlag(parted.PARTITION_BOOT):
                            skip = True
                            break

                # GPT labeled disks should only have bootable set on the
                # EFI system partition (parted sets the EFI System GUID on
                # GPT partitions with the boot flag)
                if dev.disk.format.label_type == "gpt" and \
                   dev.format.type not in ["efi", "macefi"]:
                    skip = True

                if skip:
                    log.info("Skipping %s", dev.name)
                    continue

                # hfs+ partitions on gpt can't be marked bootable via parted
                if dev.disk.format.parted_disk.type != "gpt" or \
                        dev.format.type not in ["hfs+", "macefi"]:
                    log.info("setting boot flag on %s", dev.name)
                    dev.bootable = True

                # Set the boot partition's name on disk labels that support it
                if dev.parted_partition.disk.supportsFeature(parted.DISK_TYPE_PARTITION_NAME):
                    ped_partition = dev.parted_partition.getPedPartition()
                    ped_partition.set_name(dev.format.name)
                    log.info("Setting label on %s to '%s'", dev, dev.format.name)

                dev.disk.setup()
                dev.disk.format.commit_to_disk()

        self.dump_state("final")

    def write(self):
        sysroot = util.getSysroot()
        if not os.path.isdir("%s/etc" % sysroot):
            os.mkdir("%s/etc" % sysroot)

        self.make_mtab()
        self.fsset.write()
        iscsi.write(sysroot, self)

        fcoe_proxy = STORAGE.get_proxy(FCOE)
        fcoe_proxy.WriteConfiguration(sysroot)

        if arch.is_s390():
            zfcp_proxy = STORAGE.get_proxy(ZFCP)
            zfcp_proxy.WriteConfiguration(sysroot)

        self.write_dasd_conf(sysroot)

    @property
    def bootloader(self):
        if self._bootloader is None:
            self._bootloader = get_bootloader()

        return self._bootloader

    def update_bootloader_disk_list(self):
        if not self.bootloader:
            return

        boot_disks = [d for d in self.disks if d.partitioned]
        boot_disks.sort(key=self.compare_disks_key)
        self.bootloader.set_disk_list(boot_disks)

    @property
    def boot_device(self):
        dev = None
        root_device = self.mountpoints.get("/")

        dev = self.mountpoints.get("/boot", root_device)
        return dev

    @property
    def default_boot_fstype(self):
        """The default filesystem type for the boot partition."""
        if self._default_boot_fstype:
            return self._default_boot_fstype

        fstype = None
        if self.bootloader:
            fstype = self.boot_fstypes[0]
        return fstype

    def set_default_boot_fstype(self, newtype):
        """ Set the default /boot fstype for this instance.

            Raise ValueError on invalid input.
        """
        log.debug("trying to set new default /boot fstype to '%s'", newtype)
        # This will raise ValueError if it isn't valid
        self._check_valid_fstype(newtype)
        self._default_boot_fstype = newtype

    @property
    def default_luks_version(self):
        """The default LUKS version."""
        return self._default_luks_version

    def set_default_luks_version(self, version):
        """Set the default LUKS version.

        :param version: a string with LUKS version
        :raises: ValueError on invalid input
        """
        log.debug("trying to set new default luks version to '%s'", version)
        self._check_valid_luks_version(version)
        self._default_luks_version = version

    @property
    def autopart_luks_version(self):
        """The autopart LUKS version."""
        return self._autopart_luks_version or self._default_luks_version

    @autopart_luks_version.setter
    def autopart_luks_version(self, version):
        """Set the autopart LUKS version.

        :param version: a string with LUKS version
        :raises: ValueError on invalid input
        """
        self._check_valid_luks_version(version)
        self._autopart_luks_version = version

    def _check_valid_luks_version(self, version):
        get_format("luks", luks_version=version)

    def set_default_partitioning(self, requests):
        """Set the default partitioning.

        :param requests: a list of partitioning specs
        """
        self.autopart_requests = get_full_partitioning_requests(self, _platform, requests)

    def set_up_bootloader(self, early=False):
        """ Propagate ksdata into BootLoader.

            :keyword bool early: Set to True to skip stage1_device setup

            :raises BootloaderError: if stage1 setup fails

            If this needs to be run early, eg. to setup stage1_disk but
            not stage1_device 'early' should be set True to prevent
            it from raising BootloaderError
        """
        if not self.bootloader or not self.ksdata:
            log.warning("either ksdata or bootloader data missing")
            return

        if self.bootloader.skip_bootloader:
            log.info("user specified that bootloader install be skipped")
            return

        # Need to make sure that boot drive has been setup from the latest information.
        # This will also set self.bootloader.stage1_disk.
        BootloaderExecutor().execute(self, dry_run=False)

        self.bootloader.stage2_device = self.boot_device
        if not early:
            self.bootloader.set_stage1_device(self.devices)

    @property
    def bootloader_device(self):
        return getattr(self.bootloader, "stage1_device", None)

    @property
    def boot_fstypes(self):
        """A list of all valid filesystem types for the boot partition."""
        fstypes = []
        if self.bootloader:
            fstypes = self.bootloader.stage2_format_types
        return fstypes

    def get_fstype(self, mountpoint=None):
        """ Return the default filesystem type based on mountpoint. """
        fstype = super().get_fstype(mountpoint=mountpoint)

        if mountpoint == "/boot":
            fstype = self.default_boot_fstype

        return fstype

    @property
    def mountpoints(self):
        return self.fsset.mountpoints

    @property
    def root_device(self):
        return self.fsset.root_device

    @property
    def file_system_free_space(self):
        """ Combined free space in / and /usr as :class:`blivet.size.Size`. """
        mountpoints = ["/", "/usr"]
        free = Size(0)
        btrfs_volumes = []
        for mountpoint in mountpoints:
            device = self.mountpoints.get(mountpoint)
            if not device:
                continue

            # don't count the size of btrfs volumes repeatedly when multiple
            # subvolumes are present
            if isinstance(device, BTRFSSubVolumeDevice):
                if device.volume in btrfs_volumes:
                    continue
                else:
                    btrfs_volumes.append(device.volume)

            if device.format.exists:
                free += device.format.free
            else:
                free += device.format.free_space_estimate(device.size)

        return free

    @property
    def free_space_snapshot(self):
        # if no snapshot is available, do it now and return it
        self._free_space_snapshot = self._free_space_snapshot or self.get_free_space()

        return self._free_space_snapshot

    def create_free_space_snapshot(self):
        self._free_space_snapshot = self.get_free_space()

        return self._free_space_snapshot

    def get_free_space(self, disks=None, clear_part_type=None):  # pylint: disable=arguments-differ
        """ Return a dict with free space info for each disk.

             The dict values are 2-tuples: (disk_free, fs_free). fs_free is
             space available by shrinking filesystems. disk_free is space not
             allocated to any partition.

             disks and clear_part_type allow specifying a set of disks other than
             self.disks and a clear_part_type value other than
             self.config.clear_part_type.

             :keyword disks: overrides :attr:`disks`
             :type disks: list
             :keyword clear_part_type: overrides :attr:`self.config.clear_part_type`
             :type clear_part_type: int
             :returns: dict with disk name keys and tuple (disk, fs) free values
             :rtype: dict

            .. note::

                The free space values are :class:`blivet.size.Size` instances.

        """

        # FIXME: we should definitely do something with this method -- it takes
        # different parameters than get_free_space from Blivet and does
        # different things too

        if disks is None:
            disks = self.disks

        if clear_part_type is None:
            clear_part_type = self.config.clear_part_type

        free = {}
        for disk in disks:
            should_clear = self.should_clear(disk, clear_part_type=clear_part_type,
                                             clear_part_disks=[disk.name])
            if should_clear:
                free[disk.name] = (disk.size, Size(0))
                continue

            disk_free = Size(0)
            fs_free = Size(0)
            if disk.partitioned:
                disk_free = disk.format.free
                for partition in (p for p in self.partitions if p.disk == disk):
                    # only check actual filesystems since lvm &c require a bunch of
                    # operations to translate free filesystem space into free disk
                    # space
                    should_clear = self.should_clear(partition,
                                                     clear_part_type=clear_part_type,
                                                     clear_part_disks=[disk.name])
                    if should_clear:
                        disk_free += partition.size
                    elif hasattr(partition.format, "free"):
                        fs_free += partition.format.free
            elif hasattr(disk.format, "free"):
                fs_free = disk.format.free
            elif disk.format.type is None:
                disk_free = disk.size

            free[disk.name] = (disk_free, fs_free)

        return free

    def update_ksdata(self):
        """ Update ksdata to reflect the settings of this Blivet instance. """
        if not self.ksdata or not self.mountpoints:
            return

        # clear out whatever was there before
        self.ksdata.partition.partitions = []
        self.ksdata.logvol.lvList = []
        self.ksdata.raid.raidList = []
        self.ksdata.volgroup.vgList = []
        self.ksdata.btrfs.btrfsList = []

        # iscsi?
        # fcoe?
        # zfcp?
        # dmraid?

        # bootloader

        # disk selection
        disk_select_proxy = STORAGE.get_proxy(DISK_SELECTION)

        if self.ignored_disks:
            disk_select_proxy.SetIgnoredDisks(self.ignored_disks)
        elif self.exclusive_disks:
            disk_select_proxy.SetSelectedDisks(self.exclusive_disks)

        # autopart
        auto_part_proxy = STORAGE.get_proxy(AUTO_PARTITIONING)
        auto_part_proxy.SetEnabled(self.do_autopart)
        auto_part_proxy.SetType(self.autopart_type)
        auto_part_proxy.SetEncrypted(self.encrypted_autopart)

        if self.encrypted_autopart:
            auto_part_proxy.SetLUKSVersion(self.autopart_luks_version)

            if self.autopart_pbkdf_args:
                auto_part_proxy.SetPBKDF(self.autopart_pbkdf_args.type or "")
                auto_part_proxy.SetPBKDFMemory(self.autopart_pbkdf_args.max_memory_kb)
                auto_part_proxy.SetPBKDFIterations(self.autopart_pbkdf_args.iterations)
                auto_part_proxy.SetPBKDFTime(self.autopart_pbkdf_args.time_ms)

        # clearpart
        disk_init_proxy = STORAGE.get_proxy(DISK_INITIALIZATION)
        disk_init_proxy.SetInitializationMode(self.config.clear_part_type)
        disk_init_proxy.SetDrivesToClear(self.config.clear_part_disks)
        disk_init_proxy.SetDevicesToClear(self.config.clear_part_devices)
        disk_init_proxy.SetInitializeLabelsEnabled(self.config.initialize_disks)

        if disk_init_proxy.InitializationMode == CLEAR_PARTITIONS_NONE:
            # Make a list of initialized disks and of removed partitions. If any
            # partitions were removed from disks that were not completely
            # cleared we'll have to use CLEAR_PARTITIONS_LIST and provide a list
            # of all removed partitions. If no partitions were removed from a
            # disk that was not cleared/reinitialized we can use
            # CLEAR_PARTITIONS_ALL.
            disk_init_proxy.SetDrivesToClear([])
            disk_init_proxy.SetDevicesToClear([])

            fresh_disks = [d.name for d in self.disks if d.partitioned and
                           not d.format.exists]

            destroy_actions = self.devicetree.actions.find(action_type="destroy",
                                                           object_type="device")

            cleared_partitions = []
            partial = False
            for action in destroy_actions:
                if action.device.type == "partition":
                    if action.device.disk.name not in fresh_disks:
                        partial = True

                    cleared_partitions.append(action.device.name)

            if not destroy_actions:
                pass
            elif partial:
                # make a list of removed partitions
                disk_init_proxy.SetInitializationMode(CLEAR_PARTITIONS_LIST)
                disk_init_proxy.SetDevicesToClear(cleared_partitions)
            else:
                # if they didn't partially clear any disks, use the shorthand
                disk_init_proxy.SetInitializationMode(CLEAR_PARTITIONS_ALL)
                disk_init_proxy.SetDrivesToClear(fresh_disks)

        if self.do_autopart:
            return

        self._update_custom_storage_ksdata()

    def _update_custom_storage_ksdata(self):
        """ Update KSData for custom storage. """

        # custom storage
        ks_map = {PartitionDevice: ("PartData", "partition"),
                  TmpFSDevice: ("PartData", "partition"),
                  LVMLogicalVolumeDevice: ("LogVolData", "logvol"),
                  LVMVolumeGroupDevice: ("VolGroupData", "volgroup"),
                  MDRaidArrayDevice: ("RaidData", "raid"),
                  BTRFSDevice: ("BTRFSData", "btrfs")}

        # list comprehension that builds device ancestors should not get None as a member
        # when searching for bootloader devices
        bootloader_devices = []
        if self.bootloader_device is not None:
            bootloader_devices.append(self.bootloader_device)

        # biosboot is a special case
        for device in self.devices:
            if device.format.type == 'biosboot':
                bootloader_devices.append(device)

        # make a list of ancestors of all used devices
        devices = list(set(a for d in list(self.mountpoints.values()) + self.swaps + bootloader_devices
                           for a in d.ancestors))

        # devices which share information with their distinct raw device
        complementary_devices = [d for d in devices if d.raw_device is not d]

        devices.sort(key=lambda d: len(d.ancestors))
        for device in devices:
            cls = next((c for c in ks_map if isinstance(device, c)), None)
            if cls is None:
                log.info("omitting ksdata: %s", device)
                continue

            class_attr, list_attr = ks_map[cls]

            cls = getattr(self.ksdata, class_attr)
            data = cls()    # all defaults

            complements = [d for d in complementary_devices if d.raw_device is device]

            if len(complements) > 1:
                log.warning("omitting ksdata for %s, found too many (%d) complementary devices", device, len(complements))
                continue

            device = complements[0] if complements else device

            device.populate_ksdata(data)

            parent = getattr(self.ksdata, list_attr)
            parent.dataList().append(data)

    def shutdown(self):
        """ Deactivate all devices. """
        try:
            self.devicetree.teardown_all()
        except Exception:  # pylint: disable=broad-except
            log_exception_info(log.error, "failure tearing down device tree")

    def reset(self, cleanup_only=False):
        """ Reset storage configuration to reflect actual system state.

            This will cancel any queued actions and rescan from scratch but not
            clobber user-obtained information like passphrases, iscsi config, &c

            :keyword cleanup_only: prepare the tree only to deactivate devices
            :type cleanup_only: bool

            See :meth:`devicetree.Devicetree.populate` for more information
            about the cleanup_only keyword argument.
        """
        # save passphrases for luks devices so we don't have to reprompt
        self.encryption_passphrase = None
        for device in self.devices:
            if device.format.type == "luks" and device.format.exists:
                self.save_passphrase(device)

        if self.ksdata:
            nvdimm_ksdata = self.ksdata.nvdimm
        else:
            nvdimm_ksdata = None
        ignored_nvdimm_devs = get_ignored_nvdimm_blockdevs(nvdimm_ksdata)
        if ignored_nvdimm_devs:
            log.debug("adding NVDIMM devices %s to ignored disks",
                        ",".join(ignored_nvdimm_devs))

        if self.ksdata:
            disk_select_proxy = STORAGE.get_proxy(DISK_SELECTION)
            if ignored_nvdimm_devs:
                ignored_disks = disk_select_proxy.IgnoredDisks
                ignored_disks.extend(ignored_nvdimm_devs)
                disk_select_proxy.SetIgnoredDisks(ignored_disks)
            self.config.update()

            self.ignored_disks = disk_select_proxy.IgnoredDisks
            self.exclusive_disks = disk_select_proxy.SelectedDisks
        else:
            self.ignored_disks.extend(ignored_nvdimm_devs)

        if not conf.target.is_image:
            iscsi.startup()

            fcoe_proxy = STORAGE.get_proxy(FCOE)
            fcoe_proxy.ReloadModule()

            if arch.is_s390():
                zfcp_proxy = STORAGE.get_proxy(ZFCP)
                zfcp_proxy.ReloadModule()

        super().reset(cleanup_only=cleanup_only)

        self.fsset = FSSet(self.devicetree)

        if self.bootloader:
            # clear out bootloader attributes that refer to devices that are
            # no longer in the tree
            self.bootloader.reset()

        self.update_bootloader_disk_list()

        # protected device handling
        self.protected_dev_names = []
        self._resolve_protected_device_specs()
        self._find_live_backing_device()
        for devname in self.protected_dev_names:
            dev = self.devicetree.get_device_by_name(devname, hidden=True)
            self._mark_protected_device(dev)

        self.roots = []
        self.roots = find_existing_installations(self.devicetree)
        self.dump_state("initial")

    def _resolve_protected_device_specs(self):
        """ Resolve the protected device specs to device names. """
        for spec in self.config.protected_dev_specs:
            dev = self.devicetree.resolve_device(spec)
            if dev is not None:
                log.debug("protected device spec %s resolved to %s", spec, dev.name)
                self.protected_dev_names.append(dev.name)

    def _find_live_backing_device(self):
        # FIXME: the backing dev for the live image can't be used as an
        # install target.  note that this is a little bit of a hack
        # since we're assuming that /run/initramfs/live will exist
        for mnt in open("/proc/mounts").readlines():
            if " /run/initramfs/live " not in mnt:
                continue

            live_device_path = mnt.split()[0]
            udev_device = udev.get_device(device_node=live_device_path)
            if udev_device and udev.device_is_partition(udev_device):
                live_device_name = udev.device_get_partition_disk(udev_device)
            else:
                live_device_name = live_device_path.split("/")[-1]

            log.info("resolved live device to %s", live_device_name)
            if live_device_name:
                log.info("marking live device %s protected", live_device_name)
                self.protected_dev_names.append(live_device_name)
                self.live_backing_device = live_device_name

            break

    def _mark_protected_device(self, device):
        """
          If this device is protected, mark it as such now. Once the tree
          has been populated, devices' protected attribute is how we will
          identify protected devices.

         :param :class: `blivet.devices.storage.StorageDevice` device: device to
          mark as protected
        """
        if device.name in self.protected_dev_names:
            device.protected = True
            # if this is the live backing device we want to mark its parents
            # as protected also
            if device.name == self.live_backing_device:
                for parent in device.parents:
                    parent.protected = True

    def empty_device(self, device):
        empty = True
        if device.partitioned:
            partitions = device.children
            empty = all([p.is_magic for p in partitions])
        else:
            empty = (device.format.type is None)

        return empty

    @property
    def unused_devices(self):
        used_devices = []
        for root in self.roots:
            for device in list(root.mounts.values()) + root.swaps:
                if device not in self.devices:
                    continue

                used_devices.extend(device.ancestors)

        for new in [d for d in self.devicetree.leaves if not d.format.exists]:
            if new.format.mountable and not new.format.mountpoint:
                continue

            used_devices.extend(new.ancestors)

        for device in self.partitions:
            if getattr(device, "is_logical", False):
                extended = device.disk.format.extended_partition.path
                used_devices.append(self.devicetree.get_device_by_path(extended))

        used = set(used_devices)
        _all = set(self.devices)
        return list(_all.difference(used))

    def should_clear(self, device, **kwargs):
        """ Return True if a clearpart settings say a device should be cleared.

            :param device: the device (required)
            :type device: :class:`blivet.devices.StorageDevice`
            :keyword clear_part_type: overrides :attr:`self.config.clear_part_type`
            :type clear_part_type: int
            :keyword clear_part_disks: overrides
                                     :attr:`self.config.clear_part_disks`
            :type clear_part_disks: list
            :keyword clear_part_devices: overrides
                                       :attr:`self.config.clear_part_devices`
            :type clear_part_devices: list
            :returns: whether or not clear_partitions should remove this device
            :rtype: bool
        """
        clear_part_type = kwargs.get("clear_part_type", self.config.clear_part_type)
        clear_part_disks = kwargs.get("clear_part_disks",
                                      self.config.clear_part_disks)
        clear_part_devices = kwargs.get("clear_part_devices",
                                        self.config.clear_part_devices)

        for disk in device.disks:
            # this will not include disks with hidden formats like multipath
            # and firmware raid member disks
            if clear_part_disks and disk.name not in clear_part_disks:
                return False

        if not self.config.clear_non_existent:
            if (device.is_disk and not device.format.exists) or \
               (not device.is_disk and not device.exists):
                return False

        # the only devices we want to clear when clear_part_type is
        # CLEAR_PARTITIONS_NONE are uninitialized disks, or disks with no
        # partitions, in clear_part_disks, and then only when we have been asked
        # to initialize disks as needed
        if clear_part_type in [CLEAR_PARTITIONS_NONE, CLEAR_PARTITIONS_DEFAULT]:
            if not self.config.initialize_disks or not device.is_disk:
                return False

            if not self.empty_device(device):
                return False

        if isinstance(device, PartitionDevice):
            # Never clear the special first partition on a Mac disk label, as
            # that holds the partition table itself.
            # Something similar for the third partition on a Sun disklabel.
            if device.is_magic:
                return False

            # We don't want to fool with extended partitions, freespace, &c
            if not device.is_primary and not device.is_logical:
                return False

            if clear_part_type == CLEAR_PARTITIONS_LINUX and \
               not device.format.linux_native and \
               not device.get_flag(parted.PARTITION_LVM) and \
               not device.get_flag(parted.PARTITION_RAID) and \
               not device.get_flag(parted.PARTITION_SWAP):
                return False
        elif device.is_disk:
            if device.partitioned and clear_part_type != CLEAR_PARTITIONS_ALL:
                # if clear_part_type is not CLEAR_PARTITIONS_ALL but we'll still be
                # removing every partition from the disk, return True since we
                # will want to be able to create a new disklabel on this disk
                if not self.empty_device(device):
                    return False

            # Never clear disks with hidden formats
            if device.format.hidden:
                return False

            # When clear_part_type is CLEAR_PARTITIONS_LINUX and a disk has non-
            # linux whole-disk formatting, do not clear it. The exception is
            # the case of an uninitialized disk when we've been asked to
            # initialize disks as needed
            if (clear_part_type == CLEAR_PARTITIONS_LINUX and
                not ((self.config.initialize_disks and
                      self.empty_device(device)) or
                     (not device.partitioned and device.format.linux_native))):
                return False

        # Don't clear devices holding install media.
        descendants = self.devicetree.get_dependent_devices(device)
        if device.protected or any(d.protected for d in descendants):
            return False

        if clear_part_type == CLEAR_PARTITIONS_LIST and \
           device.name not in clear_part_devices:
            return False

        return True

    def clear_partitions(self):
        """ Clear partitions and dependent devices from disks.

            This is also where zerombr is handled.
        """
        # Sort partitions by descending partition number to minimize confusing
        # things like multiple "destroy sda5" actions due to parted renumbering
        # partitions. This can still happen through the UI but it makes sense to
        # avoid it where possible.
        partitions = sorted(self.partitions,
                            key=lambda p: getattr(p.parted_partition, "number", 1),
                            reverse=True)
        for part in partitions:
            log.debug("clearpart: looking at %s", part.name)
            if not self.should_clear(part):
                continue

            self.recursive_remove(part)
            log.debug("partitions: %s", [p.name for p in part.disk.children])

        # now remove any empty extended partitions
        self.remove_empty_extended_partitions()

        # ensure all disks have appropriate disklabels
        for disk in self.disks:
            zerombr = (self.config.zero_mbr and disk.format.type is None)
            should_clear = self.should_clear(disk)
            if should_clear:
                self.recursive_remove(disk)

            if zerombr or should_clear:
                if disk.protected:
                    log.warning("cannot clear '%s': disk is protected or read only", disk.name)
                else:
                    log.debug("clearpart: initializing %s", disk.name)
                    self.initialize_disk(disk)

        self.update_bootloader_disk_list()

    def _get_hostname(self):
        """Return a hostname."""
        ignored_hostnames = {None, "", 'localhost', 'localhost.localdomain'}

        network_proxy = NETWORK.get_proxy()
        hostname = network_proxy.Hostname

        if hostname in ignored_hostnames:
            hostname = network_proxy.GetCurrentHostname()

        if hostname in ignored_hostnames:
            hostname = None

        return hostname

    def _get_container_name_template(self, prefix=None):
        """Return a template for suggest_container_name method."""
        prefix = prefix or ""  # make sure prefix is a string instead of None

        # try to create a device name incorporating the hostname
        hostname = self._get_hostname()

        if hostname:
            template = "%s_%s" % (prefix, hostname.split('.')[0].lower())
            template = self.safe_device_name(template)
        else:
            template = prefix

        if conf.target.is_image:
            template = "%s_image" % template

        return template

    def turn_on_swap(self):
        self.fsset.turn_on_swap(root_path=util.getSysroot())

    def mount_filesystems(self, read_only=None, skip_root=False):
        self.fsset.mount_filesystems(root_path=util.getSysroot(),
                                     read_only=read_only, skip_root=skip_root)

    def umount_filesystems(self, swapoff=True):
        self.fsset.umount_filesystems(swapoff=swapoff)

    def parse_fstab(self, chroot=None):
        self.fsset.parse_fstab(chroot=chroot)

    def mk_dev_root(self):
        self.fsset.mk_dev_root()

    def create_swap_file(self, device, size):
        self.fsset.create_swap_file(device, size)

    def write_dasd_conf(self, root):
        """ Write /etc/dasd.conf to target system for all DASD devices
            configured during installation.
        """
        dasds = [d for d in self.devices if d.type == "dasd"]
        dasds.sort(key=lambda d: d.name)
        if not (arch.is_s390() and dasds):
            return

        with open(os.path.realpath(root + "/etc/dasd.conf"), "w") as f:
            for dasd in dasds:
                fields = [dasd.busid] + dasd.get_opts()
                f.write("%s\n" % " ".join(fields),)

        # check for hyper PAV aliases; they need to get added to dasd.conf as well
        sysfs = "/sys/bus/ccw/drivers/dasd-eckd"

        # in the case that someone is installing with *only* FBA DASDs,the above
        # sysfs path will not exist; so check for it and just bail out of here if
        # that's the case
        if not os.path.exists(sysfs):
            return

        # this does catch every DASD, even non-aliases, but we're only going to be
        # checking for a very specific flag, so there won't be any duplicate entries
        # in dasd.conf
        devs = [d for d in os.listdir(sysfs) if d.startswith("0.0")]
        with open(os.path.realpath(root + "/etc/dasd.conf"), "a") as f:
            for d in devs:
                aliasfile = "%s/%s/alias" % (sysfs, d)
                with open(aliasfile, "r") as falias:
                    alias = falias.read().strip()

                # if alias == 1, then the device is an alias; otherwise it is a
                # normal dasd (alias == 0) and we can skip it, since it will have
                # been added to dasd.conf in the above block of code
                if alias == "1":
                    f.write("%s\n" % d)

    def make_mtab(self):
        path = "/etc/mtab"
        target = "/proc/self/mounts"
        path = os.path.normpath("%s/%s" % (util.getSysroot(), path))

        if os.path.islink(path):
            # return early if the mtab symlink is already how we like it
            current_target = os.path.normpath(os.path.dirname(path) +
                                              "/" + os.readlink(path))
            if current_target == target:
                return

        if os.path.exists(path):
            os.unlink(path)

        os.symlink(target, path)

    def add_fstab_swap(self, device):
        """
        Add swap device to the list of swaps that should appear in the fstab.

        :param device: swap device that should be added to the list
        :type device: blivet.devices.StorageDevice instance holding a swap format

        """

        self.fsset.add_fstab_swap(device)

    def remove_fstab_swap(self, device):
        """
        Remove swap device from the list of swaps that should appear in the fstab.

        :param device: swap device that should be removed from the list
        :type device: blivet.devices.StorageDevice instance holding a swap format

        """

        self.fsset.remove_fstab_swap(device)

    def set_fstab_swaps(self, devices):
        """
        Set swap devices that should appear in the fstab.

        :param devices: iterable providing devices that should appear in the fstab
        :type devices: iterable providing blivet.devices.StorageDevice instances holding
                       a swap format

        """

        self.fsset.set_fstab_swaps(devices)


def get_ignored_nvdimm_blockdevs(nvdimm_ksdata):
    """Return names of nvdimm devices to be ignored.

    By default nvdimm devices are ignored. To become available for installation,
    the device(s) must be specified by nvdimm kickstart command.
    Also, only devices in sector mode are allowed.

    :param nvdimm_ksdata: nvdimm kickstart data
    :type nvdimm_ksdata: Nvdimm kickstart command
    :returns: names of nvdimm block devices that should be ignored for installation
    :rtype: set(str)
    """

    ks_allowed_namespaces = set()
    ks_allowed_blockdevs = set()
    if nvdimm_ksdata:
        # Gather allowed blockdev names and namespaces
        for action in nvdimm_ksdata.actionList:
            if action.action == NVDIMM_ACTION_USE:
                if action.namespace:
                    ks_allowed_namespaces.add(action.namespace)
                if action.blockdevs:
                    ks_allowed_blockdevs.update(action.blockdevs)
            if action.action == NVDIMM_ACTION_RECONFIGURE:
                ks_allowed_namespaces.add(action.namespace)

    ignored_blockdevs = set()
    for ns_name, ns_info in nvdimm.namespaces.items():
        if ns_info.mode != blockdev.NVDIMMNamespaceMode.SECTOR:
            log.debug("%s / %s will be ignored - NVDIMM device is not in sector mode",
                      ns_name, ns_info.blockdev)
        else:
            if ns_name in ks_allowed_namespaces or \
                    ns_info.blockdev in ks_allowed_blockdevs:
                continue
            else:
                log.debug("%s / %s will be ignored - NVDIMM device has not been configured to be used",
                          ns_name, ns_info.blockdev)
        if ns_info.blockdev:
            ignored_blockdevs.add(ns_info.blockdev)

    return ignored_blockdevs


def storage_initialize(storage, ksdata, protected):
    """ Perform installer-specific storage initialization. """
    update_blivet_flags()

    # Platform class setup depends on flags, re-initialize it.
    _platform.update_from_flags()

    storage.shutdown()

    # Set up the protected partitions list now.
    if protected:
        storage.config.protected_dev_specs.extend(protected)

    while True:
        try:
            # This also calls storage.config.update().
            storage.reset()
        except StorageError as e:
            if error_handler.cb(e) == ERROR_RAISE:
                raise
            else:
                continue
        else:
            break

    # FIXME: This is a temporary workaround for live OS.
    if protected and not conf.system._is_live_os and \
       not any(d.protected for d in storage.devices):
        raise UnknownSourceDeviceError(protected)

    # kickstart uses all the disks
    if flags.automatedInstall:
        disk_select_proxy = STORAGE.get_proxy(DISK_SELECTION)
        selected_disks = disk_select_proxy.SelectedDisks
        ignored_disks = disk_select_proxy.IgnoredDisks

        if not selected_disks:
            selected_disks = [d.name for d in storage.disks if d.name not in ignored_disks]
            disk_select_proxy.SetSelectedDisks(selected_disks)
            log.debug("onlyuse is now: %s", ",".join(selected_disks))
