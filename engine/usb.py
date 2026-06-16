#!/usr/bin/env python3
"""
UsbManager — on-demand mounting of removable USB drives and copying their
video files into internal storage.

The Pi runs headless (no desktop auto-mount). The systemd service is granted
ambient CAP_SYS_ADMIN (see install-service.sh), so the app mounts removable
drives directly via the mount(2) syscall — no sudo, no helper. Mounts are
always READ-ONLY (and nosuid/nodev/noexec) into /tmp/recur-usb, so a drive is
never written to.

Flow used by the IMPORT menu page:
    list_drives()        -> removable partitions available to mount
    mount(dev, fstype)   -> mount one read-only, returns its mountpoint
    scan_videos(mp)      -> video files found on it
    copy_to_internal(f)  -> copy a file into cfg.clips_dir
    unmount(dev)         -> release it
"""

import ctypes
import ctypes.util
import json
import os
import shutil
import subprocess
import logging

log = logging.getLogger("usb")

# mount(2) flags
MS_RDONLY, MS_NOSUID, MS_NODEV, MS_NOEXEC = 1, 2, 4, 8
MNT_DETACH = 2
_MOUNT_FLAGS = MS_RDONLY | MS_NOSUID | MS_NODEV | MS_NOEXEC
_MOUNT_BASE = "/tmp/recur-usb"
_CAP_SYS_ADMIN = 1 << 21

_libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
# int mount(const char *src, const char *tgt, const char *fstype,
#           unsigned long flags, const void *data);
_libc.mount.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p,
                        ctypes.c_ulong, ctypes.c_char_p]
_libc.mount.restype = ctypes.c_int
_libc.umount2.argtypes = [ctypes.c_char_p, ctypes.c_int]
_libc.umount2.restype = ctypes.c_int

VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".mpg", ".mpeg")
_SKIP_DIRS = {"System Volume Information", "$RECYCLE.BIN", ".Trash-1000", ".Spotlight-V100"}
# A disk with a partition mounted at any of these hosts the OS — never import it.
_SYSTEM_MOUNTS = ("/", "/boot", "/boot/firmware")


class UsbManager:
    def __init__(self, cfg):
        self.cfg = cfg
        self._mounts = {}            # dev -> mountpoint we mounted

    # ------------------------------------------------------------- availability
    def available(self):
        """True if the process can mount — i.e. it holds CAP_SYS_ADMIN (granted
        to the systemd service via install-service.sh)."""
        try:
            with open("/proc/self/status") as f:
                for line in f:
                    if line.startswith("CapEff:"):
                        return bool(int(line.split()[1], 16) & _CAP_SYS_ADMIN)
        except Exception:
            pass
        return False

    # ------------------------------------------------------------- discovery
    def list_drives(self):
        """Return [{'dev','label','fstype','size'}] for mountable partitions on
        removable/hot-plug USB disks. Does not require mounting."""
        try:
            out = subprocess.run(
                ["lsblk", "-J", "-o", "NAME,PATH,TYPE,RM,HOTPLUG,FSTYPE,LABEL,SIZE,MOUNTPOINT"],
                capture_output=True, text=True, timeout=5).stdout
            tree = json.loads(out).get("blockdevices", [])
        except Exception as e:
            log.warning("lsblk failed: %s", e)
            return []
        drives = []
        for disk in tree:
            if disk.get("type") != "disk":
                continue
            if not (disk.get("rm") or disk.get("hotplug")):
                continue
            children = disk.get("children", []) or []
            # Skip the disk that hosts the OS — the Pi flags its SD/eMMC card
            # as removable, but it carries / and /boot and must never be touched.
            if any((c.get("mountpoint") in _SYSTEM_MOUNTS) for c in children):
                continue
            for part in children:
                if part.get("type") != "part" or not part.get("fstype"):
                    continue
                if part.get("fstype") == "swap":
                    continue
                drives.append({
                    "dev":     part.get("path") or ("/dev/" + part.get("name", "")),
                    "label":   part.get("label") or part.get("name", "USB"),
                    "fstype":  part.get("fstype", ""),
                    "size":    part.get("size", ""),
                    "mounted": bool(part.get("mountpoint")),
                })
        return drives

    # ------------------------------------------------------------- mount / unmount
    def mount(self, dev, fstype=None):
        """Mount dev read-only via the mount(2) syscall. Returns the mountpoint,
        or None. fstype comes from list_drives(); the kernel needs it explicitly."""
        if dev in self._mounts:
            return self._mounts[dev]
        name = os.path.basename(dev)
        mp = os.path.join(_MOUNT_BASE, name)
        # FAT/exFAT carry no ownership, so map files to this user; the kernel
        # ntfs driver is "ntfs3"; everything else mounts with its own fstype.
        fs = fstype or ""
        data = None
        if fs in ("vfat", "msdos", "exfat"):
            data = f"uid={os.getuid()},gid={os.getgid()}".encode()
        elif fs == "ntfs":
            fs = "ntfs3"
        try:
            os.makedirs(mp, exist_ok=True)
        except OSError as e:
            log.warning("mkdir %s failed: %s", mp, e)
            return None
        ctypes.set_errno(0)
        rc = _libc.mount(dev.encode(), mp.encode(), fs.encode(),
                         _MOUNT_FLAGS, data)
        if rc != 0:
            err = ctypes.get_errno()
            log.warning("mount(%s -> %s, %s) failed: %s",
                        dev, mp, fs or "?", os.strerror(err))
            try:
                os.rmdir(mp)
            except OSError:
                pass
            return None
        self._mounts[dev] = mp
        log.info("mounted %s (%s) -> %s", dev, fs or "auto", mp)
        return mp

    def unmount(self, dev):
        mp = self._mounts.get(dev)
        if mp:
            ctypes.set_errno(0)
            if _libc.umount2(mp.encode(), MNT_DETACH) != 0:
                log.warning("umount %s: %s", mp, os.strerror(ctypes.get_errno()))
            try:
                os.rmdir(mp)
            except OSError:
                pass
        self._mounts.pop(dev, None)
        log.info("unmounted %s", dev)

    def unmount_all(self):
        for dev in list(self._mounts):
            self.unmount(dev)

    # ------------------------------------------------------------- scan / copy
    def scan_videos(self, mountpoint, max_depth=4):
        """Find video files under a mountpoint (bounded depth)."""
        found = []
        base = mountpoint.rstrip("/")
        try:
            for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
                rel = os.path.relpath(dirpath, base)
                depth = 0 if rel == "." else rel.count(os.sep) + 1
                if depth >= max_depth:
                    dirnames[:] = []
                else:
                    dirnames[:] = sorted(d for d in dirnames
                                         if not d.startswith(".") and d not in _SKIP_DIRS)
                for f in sorted(filenames):
                    if not f.startswith(".") and f.lower().endswith(VIDEO_EXTS):
                        found.append(os.path.join(dirpath, f))
        except (OSError, PermissionError) as e:
            log.warning("scan %s: %s", mountpoint, e)
        return found

    def is_internal(self, src):
        """True if a file with this basename already exists in internal clips."""
        dest = os.path.join(self.cfg.clips_dir, os.path.basename(src))
        return os.path.exists(dest)

    def copy_to_internal(self, src, progress=None):
        """Copy src into cfg.clips_dir. Returns (dest_path, status) where status
        is 'copied' | 'exists' | 'error'. Skips if a same-named file exists."""
        tmp = None
        try:
            os.makedirs(self.cfg.clips_dir, exist_ok=True)
            dest = os.path.join(self.cfg.clips_dir, os.path.basename(src))
            if os.path.exists(dest):
                return dest, "exists"
            tmp = dest + ".part"
            shutil.copy2(src, tmp)
            os.replace(tmp, dest)
            log.info("imported %s -> %s", src, dest)
            return dest, "copied"
        except Exception as e:
            log.warning("copy %s failed: %s", src, e)
            try:
                if tmp and os.path.exists(tmp):
                    os.unlink(tmp)
            except Exception:
                pass
            return None, "error"
