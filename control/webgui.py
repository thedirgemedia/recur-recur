#!/usr/bin/env python3
"""
WebGUI — browser-based control surface for recur-recur.

Serves a single-page HTML app on HTTP_PORT (default 8080).
Real-time state sync and command reception via WebSocket on WS_PORT (8081).

The page matches the visual aesthetic of dirgemedia.com/recur-recur:
dark background, green terminal palette, Courier New monospace.

Usage (optional — add to main.py):
    from control.webgui import WebGUI
    ...
    self.webgui = WebGUI(self)
    self.webgui.start()

Requires: websockets (pip install websockets)
"""

import asyncio
import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import websockets

log = logging.getLogger("webgui")

_ASSETS = os.path.join(os.path.dirname(__file__), "webgui_assets")

HTTP_PORT = 8080
WS_PORT   = 8081

# How often (seconds) to push state even without a WS trigger — keeps the
# browser in sync after keyboard/MIDI/GPIO changes.
POLL_INTERVAL = 0.5


class _HTTPHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        fname = "index.html" if self.path in ("/", "/index.html") else None
        if fname is None:
            self.send_response(404)
            self.end_headers()
            return
        path = os.path.join(_ASSETS, fname)
        try:
            with open(path, "rb") as f:
                data = f.read()
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(data))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_):
        pass


class WebGUI:
    def __init__(self, inst):
        self.inst     = inst
        self._clients = set()
        self._lock    = threading.Lock()
        self._loop    = None
        self._paused  = False   # local pause state mirror

    # ---------------------------------------------------------------- lifecycle

    def start(self):
        threading.Thread(target=self._http_serve, daemon=True, name="wgui-http").start()
        threading.Thread(target=self._ws_serve,   daemon=True, name="wgui-ws").start()
        log.info("web GUI: http://0.0.0.0:%d/  ws://0.0.0.0:%d/", HTTP_PORT, WS_PORT)

    def _http_serve(self):
        server = HTTPServer(("0.0.0.0", HTTP_PORT), _HTTPHandler)
        server.serve_forever()

    def _ws_serve(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        async def _run():
            async with websockets.serve(self._ws_handler, "0.0.0.0", WS_PORT):
                await self._poll_loop()

        self._loop.run_until_complete(_run())

    async def _ws_handler(self, websocket):
        with self._lock:
            self._clients.add(websocket)
        try:
            await websocket.send(json.dumps(self._state()))
            async for raw in websocket:
                try:
                    msg = json.loads(raw)
                    self._handle(msg)
                    await websocket.send(json.dumps(self._state()))
                except Exception as e:
                    log.warning("webgui action error: %s", e)
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            with self._lock:
                self._clients.discard(websocket)

    async def _poll_loop(self):
        """Periodically broadcast state so keyboard/MIDI changes reach browsers."""
        while True:
            await asyncio.sleep(POLL_INTERVAL)
            await self._broadcast(json.dumps(self._state()))

    # ---------------------------------------------------------------- broadcast

    def notify(self):
        """Call from instrument after any state change to push update immediately."""
        if not self._loop:
            return
        asyncio.run_coroutine_threadsafe(
            self._broadcast(json.dumps(self._state())),
            self._loop,
        )

    async def _broadcast(self, payload):
        with self._lock:
            clients = set(self._clients)
        dead = set()
        for ws in clients:
            try:
                await ws.send(payload)
            except Exception:
                dead.add(ws)
        if dead:
            with self._lock:
                self._clients -= dead

    # ---------------------------------------------------------------- state

    def _state(self):
        inst = self.inst
        cfg  = inst.cfg
        s    = inst.sampler
        sh   = inst.shader

        clip_name = os.path.basename(cfg.current_clip) if cfg.current_clip else None
        clip_idx  = s.idx if s.clips else None
        clips     = [os.path.basename(c) for c in s.clips]

        gen_shaders = sh.list_shaders(kind="generative")
        fx_shaders  = sh.list_shaders(kind="fx")

        param_labels = sh.param_labels()
        fx_labels    = sh.fx_param_labels()

        presets = []
        if os.path.isdir(cfg.presets_dir):
            presets = sorted(
                f for f in os.listdir(cfg.presets_dir) if f.endswith(".json")
            )

        return {
            "mode":               inst.mode,
            "clip":               clip_name,
            "clip_idx":           clip_idx,
            "clips":              clips,
            "shader":             cfg.current_shader,
            "gen_shaders":        gen_shaders,
            "fx":                 cfg.current_fx,
            "fx_shaders":         fx_shaders,
            "shader_fx_stack":    getattr(cfg, "shader_fx_stack", False),
            "params":             dict(cfg.params),
            "fx_params":          dict(cfg.fx_params),
            "param_labels":       param_labels,
            "fx_labels":          fx_labels,
            "shader_blend":       getattr(cfg, "shader_blend",        False),
            "shader_blend_mode":  getattr(cfg, "shader_blend_mode",   "normal"),
            "shader_blend_amount":getattr(cfg, "shader_blend_amount", 0.5),
            "shader_blend_source":getattr(cfg, "shader_blend_source", "clip"),
            "overlay_on":         getattr(cfg, "overlay_on",          False),
            "overlay_mode":       getattr(cfg, "overlay_mode",        "difference"),
            "trail_on":           getattr(cfg, "trail_on",            False),
            "trail_mode":         getattr(cfg, "trail_mode",          "screen"),
            "color_hue":          getattr(cfg, "color_hue",           0.0),
            "color_sat":          getattr(cfg, "color_sat",           1.0),
            "paused":             self._paused,
            "presets":            presets,
        }

    # ---------------------------------------------------------------- actions

    def _handle(self, msg):
        action = msg.get("action")
        inst   = self.inst
        cfg    = inst.cfg
        s      = inst.sampler
        sh     = inst.shader

        if action == "set-mode":
            inst.set_mode(msg["mode"])

        elif action == "load-clip":
            idx = msg.get("idx")
            if idx is not None and 0 <= idx < len(s.clips):
                s.load(idx)
                s.trigger()

        elif action == "set-shader":
            sh.load(msg["shader"])

        elif action == "cycle-shader":
            sh.cycle(msg.get("dir", 1), kind="generative")

        elif action == "set-fx":
            fx = msg.get("shader")
            if not fx:
                return
            if inst.mode == "SHADER":
                cfg.current_fx       = fx
                cfg.shader_fx_stack  = True
                sh._read_fx_defaults(fx)
                sh._apply_now()
            else:
                sh.push_fx(fx)

        elif action == "cycle-fx":
            if inst.mode == "SHADER":
                sh.apply_fx_overlay(msg.get("dir", 1))
            else:
                sh.cycle(msg.get("dir", 1), kind="fx")

        elif action == "set-param":
            sh.set_param(msg["key"], float(msg["value"]))

        elif action == "set-fx-param":
            sh.set_fx_param(msg["key"], float(msg["value"]))

        elif action == "play-pause":
            if self._paused:
                s.resume()
                self._paused = False
            else:
                s.pause()
                self._paused = True

        elif action == "reverse":
            s.reverse()

        elif action == "toggle-overlay":
            inst.overlay_toggle()

        elif action == "toggle-blend":
            if inst.mode == "SHADER":
                inst.shader_blend_toggle()
            else:
                inst.overlay_toggle()

        elif action == "cycle-blend-mode":
            if inst.mode == "SHADER":
                inst.shader_blend_cycle()
            else:
                inst.overlay_cycle_mode(+1)

        elif action == "set-blend-amt":
            val = float(msg["value"])
            delta = val - getattr(cfg, "shader_blend_amount", 0.5)
            inst.shader_blend_adjust_amount(delta)

        elif action == "toggle-trail":
            inst.trail_toggle()

        elif action == "save-preset":
            name = msg.get("name", "").strip()
            if name:
                if not name.endswith(".json"):
                    name += ".json"
                cfg.save_preset(name)

        elif action == "load-preset":
            name = msg.get("name", "").strip()
            if name:
                data = cfg.load_preset(name)
                if data:
                    inst.apply_preset(data)

        elif action == "delete-preset":
            name = msg.get("name", "").strip()
            if name:
                path = os.path.join(cfg.presets_dir, name)
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass

        elif action == "cycle-blend-source":
            inst.shader_blend_source_cycle()

        else:
            log.debug("unknown webgui action: %s", action)
