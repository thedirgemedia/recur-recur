#!/usr/bin/env python3
"""
SamplerEngine — drives mpv over its JSON IPC socket.

Replicates r_e_c_u_r sampler behaviour: loop / one-shot / gated / random /
fixed-length / random-start, with dynamic in/out points and speed/direction.

Architecture (revised for smoothness):
  * ONE reader thread owns recv() and parses every line mpv sends.
  * A single _send_lock serializes writes from all controller threads.
  * Position updates come from mpv via observe_property — no polling.
  * Out-point enforcement lives in the property-change callback, so it
    fires the instant mpv reports a new position, not on a 50ms timer.
  * Synchronous get() is kept for rare cases; it uses a request-id -> Event
    map so reads can't lose replies under contention.
"""

import os
import json
import time
import socket
import random
import logging
import subprocess
import threading
import glob

log = logging.getLogger("sampler")

MODES = ["loop", "oneshot", "playlist", "random", "fixed", "randstart"]

# property IDs we observe (any unique ints; mpv echoes them back)
OBS_TIME_POS = 1
OBS_DURATION = 2
OBS_EOF      = 3


class SamplerEngine:
    def __init__(self, cfg):
        self.cfg       = cfg
        self.sock_path = cfg.mpv_socket
        self.proc      = None
        self._sock     = None
        self._reqid    = 0
        self._send_lock= threading.Lock()
        self._pending  = {}                   # reqid -> threading.Event
        self._results  = {}                   # reqid -> result
        self._reader   = None
        self._stop     = threading.Event()

        self.mode      = "loop"
        self.clips     = []
        self.idx       = 0
        self.in_pt     = 0.0
        self.out_pt    = None                 # None = end of file
        self.speed     = 1.0
        self.time_pos  = 0.0                  # live, observed
        self.duration  = 0.0                  # live, observed

        # Tracks what is currently loaded into mpv so mode switches can avoid
        # issuing a redundant 'loadfile' (which would seek to 0).
        # Values: 'clip' | 'blank' | 'camera' | None
        self._active_source = None
        self.removable_paths: set = set()   # full paths of clips from /media or /mnt

    # ------------------------------------------------------------- lifecycle
    def start(self):
        self._scan_clips()
        self._launch_mpv()
        self._connect()
        self._reader = threading.Thread(target=self._reader_loop,
                                        daemon=True, name="mpv-reader")
        self._reader.start()
        # Ask mpv to push us position + duration on every change.
        # No polling needed after this.
        self._cmd_async("observe_property", OBS_TIME_POS, "time-pos")
        self._cmd_async("observe_property", OBS_DURATION, "duration")
        self._cmd_async("observe_property", OBS_EOF,      "eof-reached")
        if self.clips:
            self.load(0)
        log.info("sampler started (%d clips)", len(self.clips))

    def stop(self):
        self._stop_cam_proc()   # kill any rpicam-vid first
        self._stop.set()
        if self._sock:
            try: self._sock.shutdown(socket.SHUT_RDWR)
            except Exception: pass
            try: self._sock.close()
            except Exception: pass
        if self.proc:
            self.proc.terminate()
            try: self.proc.wait(timeout=2)
            except Exception: self.proc.kill()

    def pause(self):  self._cmd_async("set_property", "pause", True)
    def resume(self): self._cmd_async("set_property", "pause", False)

    def stop_playback(self):
        """Stop mpv playback. In v2 we usually don't need this since mpv
        keeps owning the screen across modes — but it's still useful when
        going totally idle."""
        self._cmd_async("stop")

    def start_playback(self):
        """Load the current clip into mpv, or resume if it is already playing.

        When _active_source is already 'clip' we skip loadfile entirely —
        this preserves the current playhead position so mode changes (e.g.
        SAMPLER ↔ FX) do not restart the video.  Any other source (blank,
        camera, nothing) triggers a normal loadfile.
        """
        if not self.cfg.current_clip:
            return
        if self._active_source == 'clip':
            # Already playing a clip — just make sure it is not paused.
            self.resume()
            return
        self._cmd_async("loadfile", self.cfg.current_clip, "replace")
        self._apply_loop_mode()
        self._active_source = 'clip'
    def _rebuild_vf(self):
        """Atomically replace the VF chain with the current overlay+trail state.

        Using a single 'vf set' avoids the one-frame flash that occurs when
        'vf remove' + 'vf add' are sent as two separate commands — mpv renders
        one unfiltered frame in the gap between them.
        """
        parts = []
        if getattr(self.cfg, 'overlay_on', False):
            decay   = max(0.0, min(0.99, 1.0 - 1.0 / max(1, self.cfg.overlay_offset_frames)))
            opacity = max(0.0, min(1.0, getattr(self.cfg, 'overlay_blend_amount', 1.0)))
            parts.append(
                f"@overlay:lavfi=["
                f"split[a][b];"
                f"[b]lagfun=decay={decay:.3f}[t];"
                f"[a][t]blend=c0_mode={self.cfg.overlay_mode}:"
                f"c1_mode=normal:c2_mode=normal:"
                f"all_opacity={opacity:.3f}:shortest=1"
                f"]"
            )
        if getattr(self.cfg, 'trail_on', False):
            delay_f = max(1, int(round(
                getattr(self.cfg, 'trail_delay_s', 2.0) * self.cfg.fps)))
            blend_type = getattr(self.cfg, 'trail_blend_type', 'mode')
            if blend_type == 'opacity':
                # 4-echo step trail: evenly-spaced tpad delays blended via
                # normal/opacity mode (no lagfun, no chroma risk).  Current
                # frame is input1 for each blend; echoes are layered on top
                # from most-recent (highest opacity) to oldest (lowest).
                # step_opacities: [0.5s ago, 1.0s ago, 1.5s ago, 2.0s ago]
                step_f = max(1, delay_f // 4)
                opacs = getattr(self.cfg, 'trail_step_opacities',
                                (0.75, 0.50, 0.25, 0.15))
                g = (
                    f"split=5[_cur][_s1][_s2][_s3][_s4];"
                    f"[_s1]tpad=start_mode=clone:start={step_f}[_d1];"
                    f"[_s2]tpad=start_mode=clone:start={step_f*2}[_d2];"
                    f"[_s3]tpad=start_mode=clone:start={step_f*3}[_d3];"
                    f"[_s4]tpad=start_mode=clone:start={step_f*4}[_d4];"
                    f"[_cur][_d1]blend=all_mode=normal:all_opacity={opacs[0]:.3f}:shortest=1[_m0];"
                    f"[_m0][_d2]blend=all_mode=normal:all_opacity={opacs[1]:.3f}:shortest=1[_m1];"
                    f"[_m1][_d3]blend=all_mode=normal:all_opacity={opacs[2]:.3f}:shortest=1[_m2];"
                    f"[_m2][_d4]blend=all_mode=normal:all_opacity={opacs[3]:.3f}:shortest=1"
                )
                parts.append(f"@trail:lavfi=[{g}]")
            else:
                # Mode blend: tpad delay + lagfun fading repeats, blended on
                # luma only (c0) — all_mode zeroes chroma on modes like
                # difference (U=V=0 = green screen); c1/c2 normal keep chroma.
                decay_t = getattr(self.cfg, 'trail_decay', 0.93)
                parts.append(
                    f"@trail:lavfi=["
                    f"split[a][b];"
                    f"[b]tpad=start_mode=clone:start={delay_f},"
                    f"lagfun=decay={decay_t:.3f}[t];"
                    f"[a][t]blend=c0_mode={self.cfg.trail_mode}:"
                    f"c1_mode=normal:c2_mode=normal:shortest=1"
                    f"]"
                )
        self._cmd_async("vf", "set", ",".join(parts))

    def refresh_overlay(self):
        self._rebuild_vf()

    def apply_trail(self, mode=None):
        if mode is not None:
            self.cfg.trail_mode = mode
        self._rebuild_vf()
        log.info("trail applied (mode=%s, decay=%.3f)",
                 self.cfg.trail_mode, getattr(self.cfg, 'trail_decay', 0.93))

    def refresh_trail(self):
        self._rebuild_vf()

    def play_blank(self):
        self._stop_cam_proc()   # stop camera if we were in LIVE mode
        # Remove named filters first (belt-and-suspenders: vf set "" alone can
        # leave named lavfi filters partially live on some mpv builds).
        self._cmd_async("vf", "remove", "@overlay")
        self._cmd_async("vf", "remove", "@trail")
        self._cmd_async("vf", "set", "")
        url = (f"av://lavfi:color=black:size={self.cfg.width}x{self.cfg.height}"
               f":rate={self.cfg.fps}")
        self._cmd_async("loadfile", url, "replace")
        self._cmd_async("set_property", "loop-file", "inf")
        self._active_source = 'blank'
        log.info("loaded blank source for shader")

    # Path of the named FIFO used for CSI camera streaming
    _CAMERA_FIFO = "/tmp/recur-camera.fifo"

    def play_camera(self, device=None):
        """Load live camera input into mpv.

        CSI cameras (Pi Camera via libcamera): rpicam-vid streams MPEG-TS into
        a named FIFO; mpv reads the FIFO.  Direct av://v4l2 doesn't work for
        rp1-cfe because that driver needs the full media-controller pipeline.

        USB / V4L2 cameras: loaded directly as av://v4l2:/dev/videoN, same as
        before, but rp1-cfe and ISP nodes are now excluded from the scan.
        """
        self._stop_cam_proc()   # terminate any previous camera sub-process

        # CSI camera via libcamera?  Prefer that over V4L2 scan.
        if device is None and self._has_csi_camera():
            self._play_csi_camera()
            return

        # --- USB / V4L2 fallback ---
        # rp1-cfe devices look like "Video Capture" but require media-ctrl
        # pipeline setup and cannot be opened directly by mpv; skip them.
        candidates = [device] if device else \
                     sorted(glob.glob("/dev/video*"))
        chosen = None
        for d in candidates:
            if not d or not os.path.exists(d):
                continue
            try:
                r = subprocess.run(["v4l2-ctl", "-d", d, "--all"],
                                   capture_output=True, text=True, timeout=2)
                out = r.stdout
                if "Video Capture" not in out: continue
                if "Memory-to-Memory" in out or "M2M" in out: continue
                if any(x in out for x in
                       ("rpi-hevc-dec", "pispbe", "bcm2835-codec",
                        "rpivid", "rpi_codec", "rp1-cfe")):
                    continue
                chosen = d
                break
            except Exception:
                continue
        if not chosen:
            log.warning("no working camera found; staying on current source")
            return
        self._cmd_async("vf", "set", "")
        url = f"av://v4l2:{chosen}"
        # Per-file options override global --loop-file=inf and disable cache
        # so mpv treats this as a live stream (not a loopable file).
        self._cmd_async("loadfile", url, "replace", 0,
                        "loop-file=no,cache=no,demuxer-readahead-secs=0")
        self._active_source = 'camera'
        log.info("loaded USB camera %s", chosen)

    def _has_csi_camera(self):
        """Return True if picamera2 can see at least one CSI camera."""
        try:
            from picamera2 import Picamera2
            return bool(Picamera2.global_camera_info())
        except Exception:
            return False

    def _play_csi_camera(self):
        """Stream CSI camera via rpicam-vid → named FIFO → mpv.

        rpicam-vid writes an infinite MPEG-TS stream into the FIFO.
        mpv opens the FIFO for reading (its open() blocks until the writer
        arrives), then plays the stream like a normal file.

        The FIFO handshake sequence:
          1. Ensure the FIFO exists.
          2. Tell mpv to loadfile the FIFO  → mpv blocks on open(O_RDONLY).
          3. Start rpicam-vid             → its open(O_WRONLY) unblocks mpv.
        """
        import stat as _stat

        fifo = self._CAMERA_FIFO
        if os.path.exists(fifo):
            if not _stat.S_ISFIFO(os.stat(fifo).st_mode):
                os.remove(fifo)
                os.mkfifo(fifo)
        else:
            os.mkfifo(fifo)

        # Point mpv at the FIFO first; it will block-open waiting for writer.
        # Per-file options disable buffering/caching and enable latency hacks
        # so mpv always displays the freshest frame rather than a buffered one.
        self._cmd_async("vf", "set", "")
        self._cmd_async("loadfile", fifo, "replace", 0,
                        "loop-file=no,cache=no,"
                        "demuxer-readahead-secs=0,"
                        "demuxer-max-back-bytes=0,"
                        "video-latency-hacks=yes,"
                        f"container-fps-override={self.cfg.fps}")
        self._active_source = 'camera'

        # Small delay so mpv's loadfile reaches the kernel open() before
        # rpicam-vid opens the write end (avoids a brief ENXIO window).
        cam_w = getattr(self.cfg, "camera_width",  640)
        cam_h = getattr(self.cfg, "camera_height", 360)

        def _start():
            time.sleep(0.3)
            self._cam_proc = subprocess.Popen([
                "rpicam-vid",
                "-t", "0",                    # run until killed
                "--nopreview",
                "--width",  str(cam_w),
                "--height", str(cam_h),
                "--framerate", str(self.cfg.fps),
                "--rotation", "180",          # IMX708 is physically inverted
                # ── low-latency flags ──────────────────────────────────────
                "--intra", "1",               # every frame is a keyframe →
                                              # eliminates GOP decode delay
                                              # (default GOP≈30 adds ~1 s lag)
                "--buffer-count", "2",        # fewer in-flight ISP buffers
                "--denoise", "cdn_off",       # skip HQ denoising pass
                # ── transport ─────────────────────────────────────────────
                "--codec", "libav",
                "--libav-format", "mpegts",
                "--flush",                    # write to pipe immediately
                "-o", fifo,
            ], stdout=subprocess.DEVNULL,
               stderr=open("/tmp/rpicam.err", "w"))
            log.info("rpicam-vid started → %s  %dx%d  (pid %d)",
                     fifo, cam_w, cam_h, self._cam_proc.pid)

        threading.Thread(target=_start, daemon=True, name="rpicam-start").start()

    def _stop_cam_proc(self):
        """Terminate any rpicam-vid subprocess started by _play_csi_camera."""
        proc = getattr(self, "_cam_proc", None)
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
            log.info("rpicam-vid stopped")
        self._cam_proc = None

    # ------------------------------------------------------------- mpv setup
    def _launch_mpv(self):
        if os.path.exists(self.sock_path):
            os.remove(self.sock_path)
        # Pi 5 tuned: --profile=fast + bilinear scalers + bounded cache.
        # --hwdec=auto-safe avoids falling back to slow s/w paths.
        cmd = [
            "mpv",
            "--no-config",
            "--idle=yes",
            "--fullscreen",
            "--no-osc",
            "--no-input-default-bindings",
            "--no-audio",                   # this is a video instrument
            "--loop-file=inf",
            f"--input-ipc-server={self.sock_path}",
            "--vo=gpu",
            "--gpu-context=drm",
            "--drm-device=/dev/dri/card1",   # Pi 5: card1=VC4 display, card0=V3D GPU only
            "--profile=fast",
            "--scale=bilinear",
            "--dscale=bilinear",
            "--cscale=bilinear",
            "--video-sync=display-resample",
            "--hwdec=auto-safe",
            # bounded cache: 32MB demuxer max, 8s read-ahead -- enough for
            # smooth playback, not enough to balloon RAM on long files.
            "--cache=yes",
            "--demuxer-max-bytes=32MiB",
            "--demuxer-max-back-bytes=16MiB",
            "--demuxer-readahead-secs=8",
            # Hold the last frame at EOF instead of going idle (black) —
            # the EOF-driven play modes (playlist/random/oneshot/randstart)
            # rely on eof-reached firing while the picture stays up.
            "--keep-open=always",
        ]
        log.debug("launching mpv")
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                     stderr=open('/tmp/mpv.err', 'w'))
        # Apply scheduler priority + CPU affinity to mpv's playback process.
        # Imported here to avoid a hard dep from the engine module.
        try:
            from control.process_priority import boost_media
            boost_media(self.proc.pid, "mpv")
        except Exception as e:
            log.debug("priority boost skipped: %s", e)
        for _ in range(50):
            if os.path.exists(self.sock_path):
                return
            time.sleep(0.1)
        log.error("mpv IPC socket never appeared - is mpv installed?")

    def _connect(self):
        try:
            self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._sock.connect(self.sock_path)
            # blocking; the reader thread parks here. No timeout.
        except Exception as e:
            log.error("cannot connect to mpv socket: %s", e)
            self._sock = None

    # ------------------------------------------------------------- IPC
    def _cmd_async(self, *args):
        """Fire-and-forget command; no reply waited for."""
        if not self._sock:
            return
        with self._send_lock:
            self._reqid += 1
            line = json.dumps({"command": list(args),
                               "request_id": self._reqid}) + "\n"
            try:
                self._sock.sendall(line.encode())
            except Exception as e:
                log.debug("cmd send failed: %s", e)

    def _get_sync(self, prop, timeout=0.25):
        """Synchronous property read. Used sparingly."""
        if not self._sock:
            return None
        evt = threading.Event()
        with self._send_lock:
            self._reqid += 1
            reqid = self._reqid
            self._pending[reqid] = evt
            line = json.dumps({"command": ["get_property", prop],
                               "request_id": reqid}) + "\n"
            try:
                self._sock.sendall(line.encode())
            except Exception:
                self._pending.pop(reqid, None)
                return None
        if not evt.wait(timeout):
            self._pending.pop(reqid, None)
            return None
        return self._results.pop(reqid, None)

    def _reader_loop(self):
        """The ONE thread that reads from mpv. Parses property-change events
        and resolves pending sync gets."""
        buf = b""
        sock = self._sock
        if not sock:
            return
        while not self._stop.is_set():
            try:
                chunk = sock.recv(4096)
            except Exception:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line:
                    continue
                try:
                    msg = json.loads(line.decode("utf-8", "replace"))
                except Exception:
                    continue
                self._handle_message(msg)

    def _handle_message(self, msg):
        # Reply to a sync get?
        rid = msg.get("request_id")
        if rid and rid in self._pending:
            self._results[rid] = msg.get("data")
            self._pending.pop(rid).set()
            return
        # Property change event?
        if msg.get("event") == "property-change":
            pid = msg.get("id")
            data = msg.get("data")
            if pid == OBS_TIME_POS and data is not None:
                self.time_pos = data
                # out-point enforcement, no polling
                if self.out_pt is not None and data >= self.out_pt:
                    self._enforce_out_point()
            elif pid == OBS_DURATION and data is not None:
                self.duration = data
            elif pid == OBS_EOF and data:
                self._on_eof()

    def _enforce_out_point(self):
        if self.mode in ("loop", "random", "randstart", "fixed"):
            self._cmd_async("seek", self.in_pt, "absolute")
        elif self.mode == "playlist":
            self._advance_playlist()
        else:
            self.pause()

    def _on_eof(self):
        # keep-open pauses at EOF, so every continue-path must resume.
        if self.mode == "random" and self.clips:
            self.load(random.randrange(len(self.clips)))
            self.resume()
        elif self.mode == "randstart":
            self._seek_random_start()
            self.resume()
        elif self.mode == "playlist":
            self._advance_playlist()
            self.resume()
        # oneshot: stay paused on the last frame

    def _advance_playlist(self):
        """Advance to the next occupied slot (key order 4 → 5 → … → 9 → 4)."""
        slots    = getattr(self.cfg, 'clip_slots', {})
        occupied = sorted(k for k, v in slots.items()
                          if v and os.path.exists(v))
        if not occupied:
            return
        cur      = self.cfg.current_clip
        cur_slot = next((k for k, v in slots.items() if v == cur), None)
        if cur_slot is not None and cur_slot in occupied:
            nxt = occupied[(occupied.index(cur_slot) + 1) % len(occupied)]
        else:
            nxt = occupied[0]
        path = slots[nxt]
        self.cfg.current_clip = path
        self._cmd_async("loadfile", path, "replace")
        self.in_pt, self.out_pt = 0.0, None
        self._apply_loop_mode()
        self._active_source = 'clip'
        self.refresh_trail()
        self.refresh_overlay()
        log.info("playlist → slot %d: %s", nxt, os.path.basename(path))

    # ------------------------------------------------------------- clip mgmt
    _MEDIA_ROOTS = ("/media", "/mnt")
    _SKIP_DIRS   = frozenset(("System Volume Information", "$RECYCLE.BIN",
                               "RECYCLER", ".Spotlight-V100", ".Trashes"))
    _CLIP_EXTS   = (".mp4", ".mov", ".mkv", ".avi", ".webm")

    def _scan_clips(self):
        exts = self._CLIP_EXTS
        d = self.cfg.clips_dir
        local = sorted(
            os.path.join(d, f) for f in os.listdir(d)
            if f.lower().endswith(exts)
        ) if os.path.isdir(d) else []

        removable = []
        for base in self._MEDIA_ROOTS:
            removable.extend(self._scan_removable(base, exts))

        self.removable_paths = set(removable)
        self.clips = local + removable
        log.info("clips: %d local + %d removable", len(local), len(removable))

    def _scan_removable(self, base, exts, max_depth=3):
        """Find video files under base up to max_depth directory levels deep."""
        if not os.path.isdir(base):
            return []
        found = []
        try:
            for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
                rel   = os.path.relpath(dirpath, base)
                depth = 0 if rel == "." else rel.count(os.sep) + 1
                if depth >= max_depth:
                    dirnames[:] = []
                else:
                    dirnames[:] = sorted(
                        dn for dn in dirnames
                        if not dn.startswith(".") and dn not in self._SKIP_DIRS
                    )
                for f in sorted(filenames):
                    if not f.startswith(".") and f.lower().endswith(exts):
                        found.append(os.path.join(dirpath, f))
        except PermissionError:
            pass
        return found

    def rescan_clips(self):
        """Re-scan local clips dir and removable media. Call when browser opens."""
        self._scan_clips()

    def load(self, idx):
        if not self.clips:
            return
        self._stop_cam_proc()   # stop camera if switching away from LIVE
        self.idx = idx % len(self.clips)
        clip = self.clips[self.idx]
        self.cfg.current_clip = clip
        self._cmd_async("loadfile", clip, "replace")
        self.in_pt, self.out_pt = 0.0, None
        self._apply_loop_mode()
        self._active_source = 'clip'
        self.refresh_trail()
        self.refresh_overlay()
        log.info("clip %d -> %s", self.idx, os.path.basename(clip))

    def next_clip(self):  self.load(self.idx + 1)
    def prev_clip(self):  self.load(self.idx - 1)

    def slot(self, n):
        """Load the clip assigned to numpad key n (4–9) from cfg.clip_slots.
        Returns True if the slot was occupied and the clip exists, False if empty."""
        path = getattr(self.cfg, 'clip_slots', {}).get(n)
        if not path or not os.path.exists(path):
            return False
        self._stop_cam_proc()
        try:
            self.idx = self.clips.index(path)
        except ValueError:
            pass
        self.cfg.current_clip = path
        self._cmd_async("loadfile", path, "replace")
        self.in_pt, self.out_pt = 0.0, None
        self._apply_loop_mode()
        self._active_source = 'clip'
        self.refresh_trail()
        self.refresh_overlay()
        log.info("slot %d → %s", n, os.path.basename(path))
        return True

    # ------------------------------------------------------------- modes
    def set_mode(self, mode):
        if mode in MODES:
            self.mode = mode
            self._apply_loop_mode()
            log.info("sampler mode -> %s", mode)

    def cycle_mode(self):
        self.set_mode(MODES[(MODES.index(self.mode) + 1) % len(MODES)])

    def _apply_loop_mode(self):
        if self.mode in ("playlist", "random", "oneshot", "randstart"):
            # EOF-driven modes: let the clip end so eof-reached fires
            # (--keep-open=always holds the last frame meanwhile).
            self._cmd_async("set_property", "loop-file", "no")
        else:
            self._cmd_async("set_property", "loop-file", "inf")
        if self.mode == "randstart":
            self._seek_random_start()

    def trigger(self):
        self._cmd_async("seek", self.in_pt, "absolute")
        self.resume()

    def release(self):
        pass   # gated mode removed; method kept for API compatibility

    # ------------------------------------------------------------- in/out
    def set_in(self):
        """Set in-point to current playhead. Refuses if the current
        source isn't a real seekable clip (e.g. lavfi blank, live camera,
        or anything without a known finite duration)."""
        if not self._is_real_clip():
            log.info("ignoring set-in: current source isn't a real clip")
            return
        self.in_pt = self.time_pos
        log.info("in-point %.2fs", self.in_pt)

    def set_out(self):
        """Set out-point to current playhead.  Returns True on success,
        False if the point is at or before the current in-point (which
        would produce a zero- or negative-length loop region)."""
        if not self._is_real_clip():
            log.info("ignoring set-out: current source isn't a real clip")
            return False
        if self.time_pos <= self.in_pt:
            log.info("ignoring set-out: %.2fs is not after in-point %.2fs",
                     self.time_pos, self.in_pt)
            return False
        self.out_pt = self.time_pos
        log.info("out-point %.2fs", self.out_pt)
        return True

    def _is_real_clip(self):
        """A 'real clip' has a finite known duration > 0 and a sensible
        time_pos. Blank lavfi sources have effectively infinite duration
        and ever-growing time_pos; cameras have no duration."""
        d = self.duration
        if not d or d <= 0 or d > 86400:    # > 24 hours = not a clip
            return False
        if self.time_pos < 0 or self.time_pos > d:
            return False
        return True

    def clear_points(self):
        self.in_pt, self.out_pt = 0.0, None
        log.info("cleared in/out points")

    def _seek_random_start(self):
        # use observed duration, no sync call
        dur = self.duration
        if dur and dur > 1.0:
            self._cmd_async("seek", random.uniform(0, dur * 0.8), "absolute")

    # ------------------------------------------------------------- speed/dir
    def set_speed(self, speed):
        self.speed = max(0.1, min(4.0, speed))
        self._cmd_async("set_property", "speed", self.speed)

    def nudge_speed(self, delta):
        self.set_speed(self.speed + delta)

    def reverse(self):
        self._cmd_async("set_property", "speed", -self.speed)
        log.info("reverse toggled")

    # ------------------------------------------------------------- OSD pass-through
    def show_osd(self, text, ms=1500):
        """Public helper so OSD module doesn't need its own socket."""
        self._cmd_async("show-text", text, ms)
