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
        self._vf_had_trail  = False         # was a tpad trail in the last vf chain
        self._clip_rotate   = 0            # rotation metadata of current clip (degrees)
        self._cam_proc = None
        # Bumped on every _play_csi_camera()/_stop_cam_proc() call so a delayed
        # rpicam-start thread can tell it's been superseded (see _play_csi_camera).
        self._cam_gen  = 0

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
        # NB: we do NOT observe video-params/rotate. Launched with
        # --video-rotate=no, mpv reports rotate=0 for every clip regardless of
        # its metadata, so that property is useless here. Rotation is read from
        # the file with ffprobe at load time (see _probe_rotation).
        self.apply_video_zoom()
        if self.clips:
            self.load(0)
        # CPU LFO driver for zoom/speed (idles until something is bound).
        self._lfo_thread = threading.Thread(target=self._lfo_loop,
                                            daemon=True, name="cpu-lfo")
        self._lfo_thread.start()
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

    def apply_video_scale(self):
        """Push video_scale_mode to mpv (keepaspect + panscan)."""
        mode = getattr(self.cfg, 'video_scale_mode', 'fit')
        if mode == 'fill':
            self._cmd_async("set_property", "keepaspect", True)
            self._cmd_async("set_property", "panscan", 1.0)
        elif mode == 'stretch':
            self._cmd_async("set_property", "keepaspect", False)
            self._cmd_async("set_property", "panscan", 0.0)
        else:  # fit
            self._cmd_async("set_property", "keepaspect", True)
            self._cmd_async("set_property", "panscan", 0.0)

    def apply_video_zoom(self):
        """Push video_zoom (a linear factor) to mpv as video-zoom (log2 scale).

        video-zoom uniformly scales the picture at the VO stage, after any vf
        rotation, so it composes cleanly with rotation and panscan. 1.0x = no
        zoom; >1 pushes the picture out past the window edges (cropping the
        overflow) so a mismatched-aspect clip can be filled to the screen.
        """
        import math
        z = max(1.0, min(getattr(self.cfg, 'VIDEO_ZOOM_MAX', 4.0),
                         float(self.cfg.clip_get(self.cfg.current_clip, "zoom"))))
        self._cmd_async("set_property", "video-zoom", math.log2(z))

    def _probe_rotation(self, path):
        """Read a clip's intended display rotation (clockwise degrees) from its
        container metadata with ffprobe.

        Needed because we launch mpv with --video-rotate=no (so we control
        rotation entirely, without mpv double-rotating the lavfi trail frames),
        and under that flag mpv reports video-params/rotate=0 for everything.

        ffprobe exposes the display-matrix rotation, which is the COUNTER-
        clockwise angle (a portrait phone clip reads -90); mpv's convention —
        and our transpose table — want the clockwise angle, so negate it.
        """
        if not path or not os.path.exists(path):
            return 0
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream_side_data=rotation",
                 "-of", "default=nw=1:nk=1", path],
                capture_output=True, text=True, timeout=5).stdout
            line = out.strip().splitlines()
            if not line:
                return 0
            disp = int(float(line[0]))
            return (-disp) % 360
        except Exception as e:
            log.debug("rotation probe failed for %s: %s", path, e)
            return 0

    def set_speed_dir(self, speed, reverse):
        """Set playback speed and direction in one shot. mpv encodes reverse as
        a negative speed."""
        self.speed = max(0.1, min(4.0, float(speed)))
        self._cmd_async("set_property", "speed",
                        -self.speed if reverse else self.speed)

    def apply_video_eq(self):
        """Push this clip's brightness/contrast to mpv's VO equalizer
        (both -100..100, 0 = neutral)."""
        p = self.cfg.current_clip
        self._cmd_async("set_property", "brightness",
                        int(self.cfg.clip_get(p, "bright")))
        self._cmd_async("set_property", "contrast",
                        int(self.cfg.clip_get(p, "contrast")))

    def _apply_trail_from_clip(self, path):
        """Stage this clip's per-clip trail (steps / time / blend mode) onto the
        global trail fields the vf builder reads. steps (the 'trail' key) == 0
        turns it off. Mirrors the reference build's mode/steps/time controls.

        Works in every mode — this is mpv's lavfi tpad trail, NOT a GLSL feedback
        trail, so the Pi-5 'no cross-frame GPU feedback' limit does not apply."""
        cfg = self.cfg
        if not bool(cfg.clip_get(path, "trail_on")):
            cfg.trail_on = False
            return
        cfg.trail_on          = True
        cfg.trail_blend_type  = "mode"
        cfg.trail_echo_count  = max(1, min(5, int(cfg.clip_get(path, "trail"))))
        cfg.trail_delay_s     = float(max(0.25, min(8.0, cfg.clip_get(path, "trail_time"))))
        cfg.trail_mode        = cfg.clip_get(path, "trail_mode")
        cfg.trail_mode_opacity = max(0.0, min(1.0, float(cfg.clip_get(path, "trail_opc"))))

    def apply_clip_trail(self, path=None):
        """Live-apply a trail change (steps/time/mode) from the clip screen."""
        p = path or self.cfg.current_clip
        self._apply_trail_from_clip(p)
        self.refresh_trail()
        log.info("clip trail: on=%s steps=%d time=%.2fs mode=%s",
                 self.cfg.trail_on, getattr(self.cfg, 'trail_echo_count', 0),
                 getattr(self.cfg, 'trail_delay_s', 0.0),
                 getattr(self.cfg, 'trail_mode', '?'))

    def _apply_clip_settings(self, path):
        """Load the per-clip settings for `path` and push them to mpv: metadata
        rotation (probed), speed + direction, zoom, brightness/contrast, and
        trail. The vf chain (rotation + trail + overlay) is rebuilt by the
        caller via refresh_trail()/refresh_overlay() right after, so this only
        needs to stage the cfg trail fields before that runs."""
        cfg = self.cfg
        self._clip_rotate = self._probe_rotation(path)
        self.set_speed_dir(cfg.clip_get(path, "speed"),
                           cfg.clip_get(path, "reverse"))
        self._apply_trail_from_clip(path)
        self.apply_video_zoom()
        self.apply_video_eq()

    # ------------------------------------------------------- CPU-side LFOs
    # zoom + speed aren't GPU params (they're mpv properties), so an LFO bound
    # to them can't ride mpv's `frame` uniform like the shader LFOs do. Instead
    # a low-rate thread evaluates the same LFO math on the CPU and pushes the
    # property. Bindings live per-clip under lfo_zoom / lfo_speed.
    def _eval_lfo(self, idx, t):
        """Evaluate LFO `idx` at time `t` seconds → unipolar 0..1, matching the
        GLSL recur_lfo() in engine/shader.py exactly."""
        import math
        cfg  = self.cfg
        lfos = getattr(cfg, "lfos", [])
        if idx is None or not (0 <= idx < len(lfos)):
            return None
        try:
            from engine.shader import lfo_period
            L      = lfos[idx]
            period = max(0.05, lfo_period(cfg, L))
            shape  = int(float(L.get("shape", 0)) + 0.5)
            amp    = float(L.get("amp", 0.5))
            offset = float(L.get("offset", 0.0))
            phase  = (t / period) % 1.0
            if   shape == 0: bip = math.sin(phase * 6.28318530718)
            elif shape == 1: bip = phase * 4.0 - 1.0 if phase < 0.5 else 3.0 - phase * 4.0
            elif shape == 2: bip = phase * 2.0 - 1.0
            elif shape == 3: bip = 1.0 if phase < 0.5 else -1.0
            else:
                epoch = math.floor(t / period)
                frac  = math.sin(epoch * 127.1 + idx * 311.7) * 43758.5453
                bip   = (frac - math.floor(frac)) * 2.0 - 1.0
            return max(0.0, min(1.0, offset + (bip + 1.0) * 0.5 * amp))
        except Exception:
            return None

    def _lfo_loop(self):
        """Drive zoom/speed from their bound LFOs while a clip is playing.
        Idles cheaply (5 Hz) when nothing is bound; runs at ~20 Hz when it is."""
        import math
        t0 = time.monotonic()
        while not self._stop.is_set():
            cfg  = self.cfg
            path = cfg.current_clip
            lz   = cfg.clip_lfo(path, "zoom")
            ls   = cfg.clip_lfo(path, "speed")
            if (lz is not None or ls is not None) and self._active_source == 'clip':
                t = time.monotonic() - t0
                if lz is not None:
                    v = self._eval_lfo(lz, t)
                    if v is not None:
                        zmax = getattr(cfg, 'VIDEO_ZOOM_MAX', 4.0)
                        z = max(1.0, 1.0 + v * (zmax - 1.0))
                        self._cmd_async("set_property", "video-zoom", math.log2(z))
                if ls is not None:
                    v = self._eval_lfo(ls, t)
                    if v is not None:
                        spd = 0.1 + v * 3.9
                        rev = cfg.clip_get(path, "reverse")
                        self._cmd_async("set_property", "speed",
                                        -spd if rev else spd)
                self._stop.wait(0.05)
            else:
                self._stop.wait(0.2)

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
        self._apply_clip_settings(self.cfg.current_clip)
        self._apply_loop_mode()
        self._active_source = 'clip'
        self.refresh_overlay()   # apply this clip's rotation to the vf chain
        # Clear any stray pause inherited from a previous source ending under
        # --keep-open (e.g. leaving LIVE, or a clip that hit EOF): the pause
        # flag is global and survives loadfile, so a fresh clip could load
        # frozen. Always resume on a real (re)load.
        self.resume()
    def _rebuild_vf(self):
        """Atomically replace the VF chain with the current overlay+trail state.

        Using a single 'vf set' avoids the one-frame flash that occurs when
        'vf remove' + 'vf add' are sent as two separate commands — mpv renders
        one unfiltered frame in the gap between them.
        """
        # We apply ALL rotation ourselves via a lavfi transpose — mpv is
        # launched with --video-rotate=no and we keep it there. This is
        # deliberate: setting mpv's video-rotate to a NUMBER re-enables its own
        # autorotation, which then double-rotates the transpose we bake into the
        # trail's lavfi graph (mpv autorotates the graph's output frames on top
        # of the transpose). Keeping autorotate off makes the result
        # deterministic in every case (plain, overlay, trail).
        #
        # Effective angle = the clip's metadata rotation (from _probe_rotation,
        # since mpv reports 0 under --video-rotate=no) plus this clip's own
        # ROTATE override. 0 = original orientation.
        _rot = (self._clip_rotate +
                self.cfg.clip_get(self.cfg.current_clip, "rotate")) % 360
        # Rotation is applied ONCE as its own leading lavfi filter (below), not
        # baked into each of the overlay/trail graphs — baking it into every
        # part rotated the frame once PER part, so overlay+trail together turned
        # a 90° step into 180°. As a standalone physical transpose it outputs
        # already-rotated pixels that the downstream parts just pass through.
        _rot_filter = {
            90:  "transpose=1",   # 90° clockwise
            180: "hflip,vflip",
            270: "transpose=2",   # 90° counter-clockwise
        }.get(_rot, "")
        # Belt-and-suspenders: re-assert autorotate off (a stray numeric set
        # elsewhere would silently re-enable it and double-rotate).
        self._cmd_async("set_property", "video-rotate", "no")

        parts = []
        if _rot_filter:
            parts.append(f"@rotate:lavfi=[{_rot_filter}]")
        if getattr(self.cfg, 'overlay_on', False):
            # Self-blend of the current frame using overlay_mode, mixed at
            # overlay_blend_amount (OVL OPC). The old time-delay/lagfun echo was
            # removed — temporal echoes are the trail's job now.
            opacity = max(0.0, min(1.0, getattr(self.cfg, 'overlay_blend_amount', 1.0)))
            parts.append(
                f"@overlay:lavfi=["
                f"split[a][b];"
                f"[a][b]blend=c0_mode={self.cfg.overlay_mode}:"
                f"c1_mode=normal:c2_mode=normal:"
                f"all_opacity={opacity:.3f}:shortest=1"
                f"]"
            )
        if getattr(self.cfg, 'trail_on', False):
            delay_f = max(1, int(round(
                getattr(self.cfg, 'trail_delay_s', 2.0) * self.cfg.fps)))
            n = max(1, min(5, getattr(self.cfg, 'trail_echo_count', 1)))
            step_f = max(1, delay_f // n)
            blend_type = getattr(self.cfg, 'trail_blend_type', 'mode')
            if blend_type == 'opacity':
                # Onion-skin trail: weighted average (mix) of the live frame
                # plus n progressively-delayed PAST copies. tpad clone-pads the
                # START of each copy so every echo falls BEHIND live motion.
                # Weights decay linearly; mix normalises so brightness is
                # preserved and static areas stay clean — only motion ghosts.
                w = [max(0.05, 1.0 - i * 0.1) for i in range(n + 1)]
                taps = "".join(
                    f"[_s{i}]tpad=start_mode=clone:start={step_f*i}[_d{i}];"
                    for i in range(1, n + 1))
                inputs = f"[_cur]" + "".join(f"[_d{i}]" for i in range(1, n + 1))
                split_outs = f"[_cur]" + "".join(f"[_s{i}]" for i in range(1, n + 1))
                weights = " ".join(f"{x:.3f}" for x in w)
                g = (
                    f"split={n+1}{split_outs};"
                    f"{taps}"
                    f"{inputs}"
                    f"mix=inputs={n+1}:weights={weights}"
                )
                parts.append(f"@trail:lavfi=[{g}]")
            else:
                # Mode blend: chain n delayed copies blended onto the live frame
                # with the configured blend mode. Echoes are spaced step_f apart
                # so the furthest echo lands at trail_delay_s.
                # Every echo blends at trail_mode_opacity (the per-echo BLEND
                # slider) so brightening modes don't accumulate to white.
                tm = self.cfg.trail_mode
                op = getattr(self.cfg, 'trail_mode_opacity', 0.5)
                c0 = f"c0_mode={tm}:c0_opacity={op:.3f}"
                split_outs = "[_ma]" + "".join(f"[_ms{i}]" for i in range(1, n + 1))
                taps = "".join(
                    f"[_ms{i}]tpad=start_mode=clone:start={step_f*i}[_md{i}];"
                    for i in range(1, n + 1))
                blends = ""
                for i in range(1, n + 1):
                    inp = "[_ma]" if i == 1 else f"[_mr{i-1}]"
                    out = f"[_mr{i}]" if i < n else ""
                    blends += (
                        f"{inp}[_md{i}]blend={c0}:"
                        f"c1_mode=normal:c2_mode=normal:shortest=1{out};"
                    )
                blends = blends.rstrip(";")
                g = f"split={n+1}{split_outs};{taps}{blends}"
                parts.append(f"@trail:lavfi=[{g}]")
        self._cmd_async("vf", "set", ",".join(parts))

        # The trail uses tpad, which extends the filter timeline; on a short
        # looping clip the playhead drifts past the clip's real EOF. Removing
        # the trail then strands playback past the end and it freezes (time-pos
        # stops, though pause stays false). Re-sync the playhead whenever a
        # trail is in the chain now or was in the one we just replaced — a
        # zero-length relative seek clamps the position back into range and
        # resumes looping. Clips only (seeking a live camera/FIFO is invalid).
        has_trail = getattr(self.cfg, 'trail_on', False)
        if self._active_source == 'clip' and (has_trail or self._vf_had_trail):
            self._cmd_async("seek", 0, "relative")
        self._vf_had_trail = has_trail

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
        self._clip_rotate = 0   # synthetic source has no orientation
        self._active_source = 'blank'
        self.resume()   # never inherit a stray pause from a prior source
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
        self._cmd_async("vf", "remove", "@overlay")
        self._cmd_async("vf", "remove", "@trail")
        self._cmd_async("vf", "set", "")
        url = f"av://v4l2:{chosen}"
        # Per-file options override global --loop-file=inf and disable cache
        # so mpv treats this as a live stream (not a loopable file).
        self._cmd_async("loadfile", url, "replace", 0,
                        "loop-file=no,cache=no,demuxer-readahead-secs=0")
        self._clip_rotate = 0   # live camera has no metadata rotation
        self._active_source = 'camera'
        self.resume()   # never inherit a stray pause from a prior source
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
        self._cmd_async("vf", "remove", "@overlay")
        self._cmd_async("vf", "remove", "@trail")
        self._cmd_async("vf", "set", "")
        self._cmd_async("loadfile", fifo, "replace", 0,
                        "loop-file=no,cache=no,"
                        "demuxer-readahead-secs=0,"
                        "demuxer-max-back-bytes=0,"
                        "video-latency-hacks=yes,"
                        f"container-fps-override={self.cfg.fps}")
        self._clip_rotate = 0   # live camera has no metadata rotation
        self._active_source = 'camera'
        self.resume()   # never inherit a stray pause from a prior source

        # Small delay so mpv's loadfile reaches the kernel open() before
        # rpicam-vid opens the write end (avoids a brief ENXIO window).
        cam_w = getattr(self.cfg, "camera_width",  640)
        cam_h = getattr(self.cfg, "camera_height", 360)

        self._cam_gen += 1
        my_gen = self._cam_gen

        def _start():
            time.sleep(0.3)
            # A _stop_cam_proc()/restart that landed during the sleep above
            # bumped _cam_gen — don't launch a now-unwanted rpicam-vid.
            if self._cam_gen != my_gen:
                return
            proc = subprocess.Popen([
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
            if self._cam_gen != my_gen:
                # Stopped right as we launched — don't leak an untracked proc.
                proc.terminate()
                return
            self._cam_proc = proc
            log.info("rpicam-vid started → %s  %dx%d  (pid %d)",
                     fifo, cam_w, cam_h, proc.pid)

        threading.Thread(target=_start, daemon=True, name="rpicam-start").start()

    def _stop_cam_proc(self):
        """Terminate any rpicam-vid subprocess started by _play_csi_camera."""
        self._cam_gen += 1   # invalidate any in-flight delayed _start() above
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
            # Disable metadata auto-rotation so we control it entirely ourselves.
            # --video-rotate=no means mpv never applies the file's rotate tag;
            # we apply the correct angle via video-rotate at runtime (or via
            # a transpose prefix inside the lavfi chain for tpad trails).
            "--video-rotate=no",
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
                try:
                    self._handle_message(msg)
                except Exception as e:
                    log.warning("error handling mpv event %r: %s", msg, e)

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
        self._apply_clip_settings(path)
        self.in_pt, self.out_pt = 0.0, None
        self._apply_loop_mode()
        self._active_source = 'clip'
        self.refresh_trail()
        self.refresh_overlay()
        self.resume()
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
        self._apply_clip_settings(clip)
        self.in_pt, self.out_pt = 0.0, None
        self._apply_loop_mode()
        self._active_source = 'clip'
        self.refresh_trail()
        self.refresh_overlay()
        self.resume()
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
        self._apply_clip_settings(path)
        self.in_pt, self.out_pt = 0.0, None
        self._apply_loop_mode()
        self._active_source = 'clip'
        self.refresh_trail()
        self.refresh_overlay()
        self.resume()
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
