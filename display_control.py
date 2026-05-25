#!/usr/bin/env python3
"""
Control full-screen bitmap display on Fire tablet over ADB-forwarded TCP.

The tablet shows bitmaps at VSYNC-accurate times while simultaneously
streaming the front camera on a separate port.

Setup:
    adb forward tcp:8888 tcp:8888   # display control
    adb forward tcp:7777 tcp:7777   # camera stream (optional)

Demo:
    python3 display_control.py --demo

Library usage:
    from display_control import DisplayController
    dc = DisplayController()
    dc.sync_clocks()
    dc.load_color(0, 255, 255, 255)       # white
    dc.load_color(1, 0, 0, 0)             # black
    dc.show(0)                             # show white now
    t = dc.show_at(1, dc.now_ns() + 100_000_000)  # black in 100ms
    print(f"Appeared at PC time {t} ns")
"""

import argparse
import socket
import struct
import time

CMD_PING             = 0x01
CMD_LOAD_RGBA        = 0x02
CMD_SHOW             = 0x03
CMD_LOAD_COLOR       = 0x04
CMD_INFO             = 0x05
CMD_HIGHLIGHT        = 0x06
CMD_CLEAR_HIGHLIGHTS = 0x07
CMD_HIGHLIGHT_ANIM   = 0x08

RSP_PONG          = 0x81
RSP_LOADED        = 0x82
RSP_SHOWN         = 0x83
RSP_ERROR         = 0x84
RSP_INFO          = 0x85
RSP_HIGHLIGHT_ACK = 0x86


class DisplayController:
    def __init__(self, host="127.0.0.1", port=8888):
        self.sock = socket.create_connection((host, port))
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.clock_offset_ns = 0
        self.rtt_ns = 0
        self.screen_width = 0
        self.screen_height = 0
        self.frame_period_ns = 16_666_667

    def close(self):
        self.sock.close()

    # ── low-level protocol ──

    def _send(self, cmd, payload=b""):
        self.sock.sendall(struct.pack("<BI", cmd, len(payload)) + payload)

    def _recv(self):
        hdr = self._recv_exact(5)
        cmd, length = struct.unpack("<BI", hdr)
        payload = self._recv_exact(length) if length else b""
        if cmd == RSP_ERROR:
            raise RuntimeError(f"Tablet error: {payload.decode()}")
        return cmd, payload

    def _recv_exact(self, n):
        buf = bytearray()
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("Connection closed")
            buf.extend(chunk)
        return bytes(buf)

    # ── commands ──

    def get_info(self):
        """Query screen dimensions and frame period."""
        self._send(CMD_INFO)
        cmd, payload = self._recv()
        assert cmd == RSP_INFO, f"expected INFO, got 0x{cmd:02x}"
        self.screen_width, self.screen_height, self.frame_period_ns = \
            struct.unpack("<IIq", payload)
        return self.screen_width, self.screen_height, self.frame_period_ns

    def sync_clocks(self, rounds=21):
        """NTP-like clock sync. Returns (offset_ns, best_rtt_ns).

        After this call, pc_to_tablet_ns() / tablet_to_pc_ns() are valid.
        """
        samples = []
        for _ in range(rounds):
            t1 = time.perf_counter_ns()
            self._send(CMD_PING, struct.pack("<q", t1))
            cmd, payload = self._recv()
            t4 = time.perf_counter_ns()
            assert cmd == RSP_PONG
            _t1_echo, t2 = struct.unpack("<qq", payload)
            rtt = t4 - t1
            offset = t2 - (t1 + t4) // 2
            samples.append((rtt, offset))

        samples.sort()
        best = samples[: max(1, len(samples) // 3)]
        offsets = sorted(o for _, o in best)
        self.clock_offset_ns = offsets[len(offsets) // 2]
        self.rtt_ns = best[0][0]
        return self.clock_offset_ns, self.rtt_ns

    def pc_to_tablet_ns(self, pc_ns):
        return pc_ns + self.clock_offset_ns

    def tablet_to_pc_ns(self, tablet_ns):
        return tablet_ns - self.clock_offset_ns

    def now_ns(self):
        """Current PC monotonic time in nanoseconds."""
        return time.perf_counter_ns()

    def load_color(self, bitmap_id, r, g, b, a=255):
        """Load a solid-color bitmap into a texture slot (0..15)."""
        self._send(CMD_LOAD_COLOR, struct.pack("<IBBBB", bitmap_id, r, g, b, a))
        cmd, _ = self._recv()
        assert cmd == RSP_LOADED

    def load_rgba(self, bitmap_id, width, height, rgba_data):
        """Load raw RGBA pixel data into a texture slot."""
        hdr = struct.pack("<III", bitmap_id, width, height)
        self._send(CMD_LOAD_RGBA, hdr + rgba_data)
        cmd, _ = self._recv()
        assert cmd == RSP_LOADED

    def load_image(self, bitmap_id, filepath):
        """Load an image file (PNG/JPEG) into a texture slot. Requires Pillow."""
        from PIL import Image
        img = Image.open(filepath).convert("RGBA")
        w, h = img.size
        self.load_rgba(bitmap_id, w, h, img.tobytes())

    def show(self, bitmap_id):
        """Show bitmap immediately. Returns actual appearance time (PC ns)."""
        return self._do_show(bitmap_id, 0)

    def show_at(self, bitmap_id, pc_time_ns):
        """Show bitmap at a specific PC time. Returns actual appearance time (PC ns)."""
        return self._do_show(bitmap_id, pc_time_ns)

    def _do_show(self, bitmap_id, pc_time_ns):
        tablet_target = self.pc_to_tablet_ns(pc_time_ns) if pc_time_ns else 0
        self._send(CMD_SHOW, struct.pack("<Iq", bitmap_id, tablet_target))
        cmd, payload = self._recv()
        assert cmd == RSP_SHOWN
        _shown_id, _sched_ns, actual_ns = struct.unpack("<Iqq", payload)
        return self.tablet_to_pc_ns(actual_ns)

    # ── highlights ──

    def add_highlight(self, x1, y1, x2, y2, target_pc_ns=0,
                      color=(255, 220, 0, 77)):
        """Add a highlight rectangle overlay.

        Coordinates are normalized [0.0, 1.0] where (0,0) = top-left,
        (1,1) = bottom-right. Any two opposite corners work (order doesn't
        matter). Color is (R, G, B, A) with A controlling transparency.
        ACK is immediate; the rectangle appears at the target VSYNC.
        """
        tablet_t = self.pc_to_tablet_ns(target_pc_ns) if target_pc_ns else 0
        r, g, b, a = color
        payload = struct.pack("<ffffBBBBq", x1, y1, x2, y2, r, g, b, a, tablet_t)
        self._send(CMD_HIGHLIGHT, payload)
        cmd, _ = self._recv()
        assert cmd == RSP_HIGHLIGHT_ACK

    def add_highlight_px(self, x1, y1, x2, y2, bmp_w, bmp_h,
                         target_pc_ns=0, color=(255, 220, 0, 77)):
        """Like add_highlight but coordinates are in bitmap pixels."""
        self.add_highlight(x1 / bmp_w, y1 / bmp_h,
                           x2 / bmp_w, y2 / bmp_h,
                           target_pc_ns, color)

    def clear_highlights(self, target_pc_ns=0):
        """Remove all highlight overlays at the target time (0 = immediate)."""
        tablet_t = self.pc_to_tablet_ns(target_pc_ns) if target_pc_ns else 0
        self._send(CMD_CLEAR_HIGHLIGHTS, struct.pack("<q", tablet_t))
        cmd, _ = self._recv()
        assert cmd == RSP_HIGHLIGHT_ACK

    def add_highlight_anim(self, x_start, y_top, x_end, y_bot,
                           t_start_pc_ns, t_end_pc_ns,
                           color=(255, 220, 0, 60)):
        """Add a smoothly animated highlight that grows from x_start to x_end.

        The rectangle's leading edge is at x_start at t_start and reaches
        x_end at t_end. The tablet interpolates pixel-by-pixel at 60 fps
        with zero USB traffic during playback.

        Coordinates are normalized [0.0, 1.0].
        """
        r, g, b, a = color
        t1 = self.pc_to_tablet_ns(t_start_pc_ns)
        t2 = self.pc_to_tablet_ns(t_end_pc_ns)
        payload = struct.pack("<ffffBBBBqq",
                              x_start, y_top, x_end, y_bot,
                              r, g, b, a, t1, t2)
        self._send(CMD_HIGHLIGHT_ANIM, payload)
        cmd, _ = self._recv()
        assert cmd == RSP_HIGHLIGHT_ACK

    def add_highlight_anim_px(self, x_start, y_top, x_end, y_bot,
                              bmp_w, bmp_h, t_start_pc_ns, t_end_pc_ns,
                              color=(255, 220, 0, 60)):
        """Like add_highlight_anim but with bitmap-pixel coordinates."""
        self.add_highlight_anim(
            x_start / bmp_w, y_top / bmp_h,
            x_end / bmp_w, y_bot / bmp_h,
            t_start_pc_ns, t_end_pc_ns, color)


def demo():
    print("Connecting to tablet (port 8888)...")
    dc = DisplayController()

    print("Getting device info...")
    w, h, fp = dc.get_info()
    print(f"  Screen: {w}x{h}, frame period: {fp/1e6:.2f} ms ({1e9/fp:.1f} Hz)")

    print("Synchronizing clocks...")
    offset, rtt = dc.sync_clocks()
    print(f"  Clock offset: {offset/1e6:.3f} ms, best RTT: {rtt/1e6:.3f} ms")

    print("Loading colors...")
    dc.load_color(0, 255, 255, 255)  # white
    dc.load_color(1, 0, 0, 0)        # black
    dc.load_color(2, 255, 0, 0)      # red
    dc.load_color(3, 0, 255, 0)      # green
    dc.load_color(4, 0, 0, 255)      # blue
    print("  5 colors loaded")

    print("\nImmediate show test:")
    for i, name in enumerate(["white", "black", "red", "green", "blue"]):
        t_before = time.perf_counter_ns()
        t_actual = dc.show(i)
        latency_ms = (t_actual - t_before) / 1e6
        print(f"  {name}: latency {latency_ms:.2f} ms")
        time.sleep(0.4)

    print("\nScheduled timing accuracy test (10 flashes):")
    errors = []
    for i in range(10):
        t_target = time.perf_counter_ns() + 100_000_000  # 100 ms ahead
        t_actual = dc.show_at(i % 2, t_target)
        error_us = (t_actual - t_target) / 1000
        errors.append(abs(error_us))
        print(f"  Frame {i}: target error = {error_us:+.1f} us")
        time.sleep(0.5)

    mean_err = sum(errors) / len(errors)
    max_err = max(errors)
    print(f"\n  Mean |error|: {mean_err:.1f} us,  max |error|: {max_err:.1f} us")
    print(f"  (Bounded by VSYNC period: {fp/1000:.0f} us)")

    # ── highlight demo ──
    print("\nHighlight overlay demo:")
    dc.load_color(5, 240, 240, 240)  # light gray "score" background
    dc.show(5)
    time.sleep(0.3)

    print("  Sweeping highlight left-to-right (simulating score playback)...")
    steps = 20
    for i in range(steps):
        x_right = (i + 1) / steps
        dc.clear_highlights()
        dc.add_highlight(0.0, 0.0, x_right, 1.0)
        time.sleep(0.15)

    print("  Scheduled highlight with precise timing...")
    dc.clear_highlights()
    for i in range(10):
        t = time.perf_counter_ns() + (i + 1) * 200_000_000  # every 200ms
        x_right = (i + 1) / 10
        if i > 0:
            dc.clear_highlights(target_pc_ns=t)
        dc.add_highlight(0.0, 0.2, x_right, 0.8, target_pc_ns=t)
    print("  Queued 10 timed highlights — watching...")
    time.sleep(2.5)

    dc.clear_highlights()
    dc.show(1)  # end on black
    dc.close()
    print("Done.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Control bitmap display on Fire tablet")
    ap.add_argument("--demo", action="store_true", help="Run timing demo")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8888)
    args = ap.parse_args()

    if args.demo:
        demo()
    else:
        print("Use --demo to run the timing demo, or import as a library.")
        print("Setup: adb forward tcp:8888 tcp:8888")
