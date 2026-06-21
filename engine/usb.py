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
import threading
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
_MEDIA_CORES   = {2, 3}   # pin ffmpeg here; Python lives on {0, 1}


def _probe_video(path):
    """Return a dict of video stream properties, or None on error."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-of", "json",
             "-show_streams", "-show_format", path],
            capture_output=True, text=True, timeout=15)
        data = json.loads(r.stdout)
        streams = data.get("streams", [])
        fmt     = data.get("format", {})
        video   = next((s for s in streams if s.get("codec_type") == "video"), None)
        has_audio = any(s.get("codec_type") == "audio" for s in streams)
        if not video:
            return None

        def _fps(frac):
            try:
                n, d = frac.split("/")
                return float(n) / float(d) if float(d) != 0 else 0.0
            except Exception:
                return 0.0

        r_fps = _fps(video.get("r_frame_rate", "0/0"))
        a_fps = _fps(video.get("avg_frame_rate", "0/0"))
        is_cfr = (r_fps > 0 and a_fps > 0
                  and abs(r_fps - a_fps) / max(r_fps, a_fps) < 0.02)
        # prefer stream-level duration; fall back to container-level (AVI, MKV)
        duration = float(video.get("duration") or fmt.get("duration") or 0)
        return {
            "codec":     video.get("codec_name", ""),
            "pix_fmt":   video.get("pix_fmt", ""),
            "a_fps":     a_fps,
            "is_cfr":    is_cfr,
            "has_audio": has_audio,
            "duration":  duration,
        }
    except FileNotFoundError:
        log.error("ffprobe not found — sudo apt install ffmpeg")
        return None
    except Exception:
        return None


def _classify(info, fallback_fps=30.0):
    """Return (action, target_fps) where action is 'remux' | 'transcode'.

    fallback_fps is used when probe data is absent or the detected fps is
    implausibly low (< 10); callers should pass cfg.fps so the transcode
    matches the display's frame cadence.
    """
    if not info:
        return "transcode", fallback_fps
    fps = info["a_fps"] if info["a_fps"] >= 10.0 else fallback_fps
    ideal = (info["codec"] == "h264"
             and info["pix_fmt"] == "yuv420p"
             and info["is_cfr"])
    if ideal:
        return "remux", fps   # -c:v copy -an: strips audio, keeps video stream
    return "transcode", fps


def _run_ffmpeg(cmd, duration=0.0, progress=None, cancel=None):
    """Run an ffmpeg command, calling progress(frac) from stderr if provided.

    cancel is an optional threading.Event; set it to kill ffmpeg and abort.
    Pins the ffmpeg process to _MEDIA_CORES so it doesn't compete with the
    Python render loop on cores 0-1.
    """
    proc = subprocess.Popen(cmd, stderr=subprocess.PIPE,
                            stdout=subprocess.DEVNULL, text=True)
    try:
        os.sched_setaffinity(proc.pid, _MEDIA_CORES)
    except OSError:
        pass

    def _drain():
        try:
            for line in proc.stderr:
                if progress and duration > 0 and "time=" in line:
                    try:
                        t = line.split("time=")[1].split()[0]
                        h, m, s = t.split(":")
                        elapsed = max(0.0, float(h) * 3600 + float(m) * 60 + float(s))
                        progress(min(elapsed / duration, 1.0))
                    except Exception:
                        pass
        except Exception:
            pass

    drain = threading.Thread(target=_drain, daemon=True)
    drain.start()
    cancelled = False
    try:
        while True:
            try:
                proc.wait(timeout=1.0)
                break
            except subprocess.TimeoutExpired:
                if cancel and cancel.is_set():
                    proc.kill()
                    proc.wait()
                    cancelled = True
                    break
    finally:
        proc.stderr.close()
        drain.join(timeout=5)

    if cancelled:
        raise RuntimeError("ffmpeg cancelled")
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg exited {proc.returncode}")


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
            try:
                user = os.getlogin()
            except OSError:
                user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
            mp = f"/media/{user}/{label}"
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
        """True if this file (as .mp4) already exists in internal clips."""
        base = os.path.splitext(os.path.basename(src))[0] + ".mp4"
        return os.path.exists(os.path.join(self.cfg.clips_dir, base))

    def copy_to_internal(self, src, progress=None, cancel=None):
        """Import src into cfg.clips_dir, transcoding to H.264/yuv420p/CFR/MP4
        as needed for low CPU playback and full effects compatibility.

        Fast path (no re-encode): already H.264 + yuv420p + CFR → remux
        (ffmpeg -c:v copy -an) to strip any audio and rewrap in MP4.

        cancel is an optional threading.Event; set it to abort an in-progress
        ffmpeg run (e.g. when the user ejects the drive).

        Returns (dest_path, status) where status is 'copied' | 'exists' | 'error'.
        progress(frac) is called with 0.0–1.0 during transcode if provided.
        """
        tmp = None
        try:
            os.makedirs(self.cfg.clips_dir, exist_ok=True)
            base = os.path.splitext(os.path.basename(src))[0] + ".mp4"
            dest = os.path.join(self.cfg.clips_dir, base)
            if os.path.exists(dest):
                return dest, "exists"
            tmp = dest + ".part"

            info = _probe_video(src)
            action, target_fps = _classify(info, fallback_fps=self.cfg.fps)

            cmd = ["ffmpeg", "-y", "-i", src]
            if action == "remux":
                cmd += ["-c:v", "copy", "-an"]
            else:
                cmd += [
                    "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                    "-pix_fmt", "yuv420p",
                    "-r", f"{target_fps:.3f}",
                    "-an",
                ]
            cmd += ["-f", "mp4", tmp]
            _run_ffmpeg(cmd, duration=info["duration"] if info else 0.0,
                        progress=progress, cancel=cancel)

            os.replace(tmp, dest)
            log.info("imported %s -> %s (%s)", os.path.basename(src),
                     os.path.basename(dest), action)
            return dest, "copied"
        except FileNotFoundError:
            log.error("ffmpeg not found — sudo apt install ffmpeg")
            try:
                if tmp and os.path.exists(tmp):
                    os.unlink(tmp)
            except Exception:
                pass
            return None, "error"
        except Exception as e:
            log.warning("import %s failed: %s", src, e)
            try:
                if tmp and os.path.exists(tmp):
                    os.unlink(tmp)
            except Exception:
                pass
            return None, "error"
