#!/usr/bin/env python3
"""
UsbManager — on-demand mounting of removable USB drives and copying their
video files into internal storage.

Mount strategy (tried in order):
  1. If the OS (e.g. udisks2) already mounted the drive, use that mountpoint
     directly — no extra privileges needed, and we leave it mounted on exit.
  2. If the process holds CAP_SYS_ADMIN (granted to the systemd service via
     install-service.sh), mount read-only via the mount(2) syscall into
     /tmp/recur-usb.
  3. If pmount is installed (suid helper, available via apt), use it. The
     mountpoint is /media/<label>; pumount unmounts.

Mounts we create (cases 2 & 3) are always READ-ONLY. We never write to the
drive.

Flow used by the IMPORT menu page:
    list_drives()        -> removable partitions available to mount
    mount(dev, fstype)   -> mount one read-only, returns its mountpoint
    scan_videos(mp)      -> video files found on it
    copy_to_internal(f)  -> copy a file into cfg.clips_dir
    unmount(dev)         -> release it (no-op for OS-mounted drives)
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
_libc.mount.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p,
                        ctypes.c_ulong, ctypes.c_char_p]
_libc.mount.restype = ctypes.c_int
_libc.umount2.argtypes = [ctypes.c_char_p, ctypes.c_int]
_libc.umount2.restype = ctypes.c_int

VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".mpg", ".mpeg")
_SKIP_DIRS = {"System Volume Information", "$RECYCLE.BIN", ".Trash-1000", ".Spotlight-V100"}
_SYSTEM_MOUNTS = ("/", "/boot", "/boot/firmware")


def _has_cap_sys_admin():
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("CapEff:"):
                    return bool(int(line.split()[1], 16) & _CAP_SYS_ADMIN)
    except Exception:
        pass
    return False


def _pmount_bin():
    return shutil.which("pmount")


class UsbManager:
    def __init__(self, cfg):
        self.cfg = cfg
        self._mounts    = {}   # dev -> mountpoint (mounts WE created)
        self._pmounted  = set()  # devs we mounted via pmount (use pumount)
        self._ext_mounts = {}  # dev -> mountpoint already mounted by OS

    # ------------------------------------------------------------- availability
    def available(self):
        """True if we can access USB drives in any of the three mount modes."""
        return _has_cap_sys_admin() or bool(_pmount_bin())

    # ------------------------------------------------------------- discovery
    def list_drives(self):
        """Return [{'dev','label','fstype','size','mountpoint'}] for mountable
        partitions on removable/hot-plug USB disks."""
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
            if any((c.get("mountpoint") in _SYSTEM_MOUNTS) for c in children):
                continue
            for part in children:
                if part.get("type") != "part" or not part.get("fstype"):
                    continue
                if part.get("fstype") == "swap":
                    continue
                drives.append({
                    "dev":        part.get("path") or ("/dev/" + part.get("name", "")),
                    "label":      part.get("label") or part.get("name", "USB"),
                    "fstype":     part.get("fstype", ""),
                    "size":       part.get("size", ""),
                    "mountpoint": part.get("mountpoint") or None,
                })
        return drives

    # ------------------------------------------------------------- mount / unmount
    def mount(self, dev, fstype=None):
        """Return a usable mountpoint for dev, mounting it if necessary."""
        # Already tracked by us
        if dev in self._mounts:
            return self._mounts[dev]
        if dev in self._ext_mounts:
            return self._ext_mounts[dev]

        # Case 1: already mounted by the OS (udisks2 / udev auto-mount)
        existing = self._os_mountpoint(dev)
        if existing:
            log.info("using OS-mounted %s -> %s", dev, existing)
            self._ext_mounts[dev] = existing
            return existing

        # Case 2: CAP_SYS_ADMIN — mount(2) syscall directly
        if _has_cap_sys_admin():
            return self._mount_direct(dev, fstype)

        # Case 3: pmount suid helper
        pm = _pmount_bin()
        if pm:
            return self._mount_pmount(dev, pm)

        log.warning("cannot mount %s: no CAP_SYS_ADMIN and pmount not found", dev)
        return None

    def _os_mountpoint(self, dev):
        """Return the mountpoint if dev is already mounted, else None."""
        try:
            with open("/proc/mounts") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2 and parts[0] == dev:
                        return parts[1]
        except Exception:
            pass
        return None

    def _mount_direct(self, dev, fstype=None):
        name = os.path.basename(dev)
        mp = os.path.join(_MOUNT_BASE, name)
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
        rc = _libc.mount(dev.encode(), mp.encode(), fs.encode(), _MOUNT_FLAGS, data)
        if rc != 0:
            err = ctypes.get_errno()
            log.warning("mount(%s -> %s, %s) failed: %s", dev, mp, fs or "?", os.strerror(err))
            try:
                os.rmdir(mp)
            except OSError:
                pass
            return None
        self._mounts[dev] = mp
        log.info("mounted %s (%s) -> %s", dev, fs or "auto", mp)
        return mp

    def _mount_pmount(self, dev, pm_bin):
        label = os.path.basename(dev)
        try:
            r = subprocess.run([pm_bin, dev, label],
                               capture_output=True, text=True, timeout=10)
            if r.returncode != 0:
                log.warning("pmount %s failed: %s", dev, r.stderr.strip())
                return None
        except Exception as e:
            log.warning("pmount %s error: %s", dev, e)
            return None
        mp = f"/media/{label}"
        if not os.path.ismount(mp):
            # pmount may use /media/<user>/<label> on newer systems
            mp = f"/media/{os.getlogin()}/{label}"
        if not os.path.ismount(mp):
            mp = self._os_mountpoint(dev)
        if not mp:
            log.warning("pmount succeeded but mountpoint not found for %s", dev)
            return None
        self._mounts[dev] = mp
        self._pmounted.add(dev)
        log.info("pmounted %s -> %s", dev, mp)
        return mp

    def unmount(self, dev):
        # Never unmount drives the OS mounted — that's not ours to release
        if dev in self._ext_mounts:
            self._ext_mounts.pop(dev, None)
            log.info("released external mount %s (not unmounting)", dev)
            return

        mp = self._mounts.pop(dev, None)
        if not mp:
            return

        if dev in self._pmounted:
            self._pmounted.discard(dev)
            pu = shutil.which("pumount")
            if pu:
                subprocess.run([pu, dev], capture_output=True, timeout=5)
            else:
                subprocess.run(["umount", dev], capture_output=True, timeout=5)
        else:
            ctypes.set_errno(0)
            if _libc.umount2(mp.encode(), MNT_DETACH) != 0:
                log.warning("umount %s: %s", mp, os.strerror(ctypes.get_errno()))
            try:
                os.rmdir(mp)
            except OSError:
                pass
        log.info("unmounted %s", dev)

    def unmount_all(self):
        for dev in list(self._mounts) + list(self._ext_mounts):
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
        is 'copied' | 'exists' | 'error'."""
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
