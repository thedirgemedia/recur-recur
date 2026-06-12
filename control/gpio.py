#!/usr/bin/env python3
"""
GpioController - physical knobs (via MCP3008 ADC over SPI) and tact buttons,
in the spirit of i_n_c_u_r / c_l_i_k_r hardware. 4 knobs -> shader params,
buttons -> mode/trigger.

Uses gpiozero. On Pi 5 gpiozero auto-selects the lgpio pin factory.

Optimisations (over the earlier version):
  * Knob loop runs at 20 Hz, not 30 Hz - the human ear/eye doesn't notice
    the difference and it cuts SPI traffic by a third.
  * Larger deadband (0.015) on knob reads means we only fire param updates
    when the user actually moves a knob, not from ADC noise.
  * No per-read float allocation when value hasn't changed.
"""

import logging
import threading
import time

log = logging.getLogger("gpio")

try:
    from gpiozero import MCP3008, Button
    HAVE_GPIO = True
except Exception:
    HAVE_GPIO = False

# tact buttons -> BCM pins (adjust to your wiring)
BTN_MODE    = 5
BTN_TRIGGER = 6
BTN_REC     = 13
BTN_SHADER  = 19

POLL_HZ     = 20
DEADBAND    = 0.015     # knob change threshold (0..1)
PARAM_KEYS  = ("p1", "p2", "p3", "p4")


class GpioController:
    def __init__(self, inst):
        self.inst   = inst
        self.knobs  = []
        self.btns   = []
        self._stop  = threading.Event()
        self._thread= None
        self._last  = [-1.0] * 4   # sentinel; first read always fires

    def start(self):
        if not self.inst.cfg.use_gpio:
            return
        if not HAVE_GPIO:
            log.warning("gpiozero not available - GPIO control disabled")
            return
        try:
            # MCP3008 must use SPI0.1 (device=1) — SPI0.0 is reserved for
            # the ILI9486 display. Sharing CE0 would corrupt display GRAM.
            self.knobs = [MCP3008(channel=c, device=1) for c in range(4)]
        except Exception as e:
            log.warning("MCP3008 init failed (%s) - knobs disabled", e)

        self._setup_buttons()
        if self.knobs:
            self._thread = threading.Thread(target=self._poll, daemon=True,
                                            name="gpio-knobs")
            self._thread.start()
        log.info("GPIO control active (%d knobs)", len(self.knobs))

    def _setup_buttons(self):
        configs = [
            (BTN_MODE,    0.05, lambda: self.inst.cycle_mode(),             None),
            (BTN_TRIGGER, 0.02, lambda: self.inst.sampler.trigger(),        lambda: self.inst.sampler.release()),
            (BTN_REC,     0.1,  lambda: self.inst.mixer.toggle_recording(), None),
            (BTN_SHADER,  0.05, lambda: self.inst.sampler.cycle_mode(),     None),
        ]
        for pin, bounce, press_cb, release_cb in configs:
            try:
                b = Button(pin, bounce_time=bounce)
                b.when_pressed = press_cb
                if release_cb:
                    b.when_released = release_cb
                self.btns.append(b)
            except Exception as e:
                log.warning("button GPIO%d failed: %s", pin, e)

    def _poll(self):
        """Read knobs at POLL_HZ; only push when one actually moves."""
        period = 1.0 / POLL_HZ
        last   = self._last
        knobs  = self.knobs
        set_p  = self.inst.shader.set_param
        keys   = PARAM_KEYS
        while not self._stop.is_set():
            t0 = time.monotonic()
            for i in range(len(knobs)):
                try:
                    v = knobs[i].value
                except Exception:
                    continue
                if abs(v - last[i]) > DEADBAND:
                    last[i] = v
                    set_p(keys[i], v)
            # sleep the remainder of the period (drift-correcting)
            dt = time.monotonic() - t0
            if dt < period:
                self._stop.wait(period - dt)

    def stop(self):
        self._stop.set()
        for b in self.btns:
            try: b.close()
            except Exception: pass
        for k in self.knobs:
            try: k.close()
            except Exception: pass
