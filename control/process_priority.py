#!/usr/bin/env python3
"""
process_priority.py - apply nice/CPU-affinity tuning to mpv and glslViewer.

Rationale on Pi 5 (4x Cortex-A76):
  cores 0-1 -> Python control threads (UI, GPIO poll, MIDI, keyboard)
  cores 2-3 -> media processes (mpv playback, glslViewer rendering)
This keeps L1/L2 cache hot for the render path and prevents the chatty
Python threads from preempting decode work.

We use os.sched_setaffinity (no extra deps) and `nice` (a tiny syscall).
Requires CAP_SYS_NICE to raise priority, which the systemd unit grants via
AmbientCapabilities. If we can't get it (running interactively without
caps), we silently fall back to affinity only -- still a real win.
"""

import os
import logging
import resource

log = logging.getLogger("priority")

# core sets for Pi 5 (4 cores). On other Pis with fewer cores we degrade
# gracefully by intersecting with the actual online set.
PYTHON_CORES = {0, 1}
MEDIA_CORES  = {2, 3}

# nice values: more negative = higher priority. -10 is a reasonable boost
# without starving the kernel; -20 risks USB / network jitter.
MEDIA_NICE   = -10
PYTHON_NICE  = 0       # leave Python at default; we just pin its affinity


def _online_cores():
    """Cores actually available on this CPU (handles Pi 3 / Pi 4 / Pi 5)."""
    try:
        return os.sched_getaffinity(0)
    except (AttributeError, OSError):
        return set(range(os.cpu_count() or 1))


def _intersect(target, online):
    """Pick the intersection; if empty, fall back to whatever's online."""
    s = target & online
    return s if s else online


def boost_self_python():
    """Pin the current (Python orchestrator) process to control cores."""
    online = _online_cores()
    cores  = _intersect(PYTHON_CORES, online)
    try:
        os.sched_setaffinity(0, cores)
        log.info("python pinned to cores %s", sorted(cores))
    except (AttributeError, OSError) as e:
        log.debug("affinity set failed: %s", e)


def boost_media(pid: int, label: str = "media"):
    """Apply media-process tuning to the given PID (mpv or glslViewer)."""
    if pid <= 0:
        return
    online = _online_cores()
    cores  = _intersect(MEDIA_CORES, online)
    affinity_ok = False
    nice_ok     = False
    try:
        os.sched_setaffinity(pid, cores)
        affinity_ok = True
    except (AttributeError, OSError) as e:
        log.debug("%s affinity failed: %s", label, e)

    # nice can only be lowered (more negative) with CAP_SYS_NICE.
    # If we lack the cap, fall back to nice=0 which still > nothing.
    try:
        _setpriority(pid, MEDIA_NICE)
        nice_ok = True
    except PermissionError:
        log.debug("%s nice<0 denied (no CAP_SYS_NICE); leaving at default",
                  label)
    except Exception as e:
        log.debug("%s nice failed: %s", label, e)

    if affinity_ok or nice_ok:
        log.info("%s pid=%d: cores=%s, nice=%s",
                 label, pid, sorted(cores) if affinity_ok else "default",
                 MEDIA_NICE if nice_ok else "default")


def _setpriority(pid, nice_value):
    """os.setpriority wrapper that works on Linux for arbitrary PIDs."""
    # PRIO_PROCESS = 0
    os.setpriority(os.PRIO_PROCESS, pid, nice_value)


def can_renice() -> bool:
    """Quick check: do we have permission to lower our own nice?"""
    try:
        cur = os.getpriority(os.PRIO_PROCESS, 0)
        os.setpriority(os.PRIO_PROCESS, 0, cur - 1)
        os.setpriority(os.PRIO_PROCESS, 0, cur)
        return True
    except (PermissionError, OSError):
        return False
