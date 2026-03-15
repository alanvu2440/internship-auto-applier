"""ESC key monitor for toggling between auto/manual mode."""

import asyncio
import sys
import os
import threading
import select

from loguru import logger


class EscMonitor:
    """Monitor ESC key to toggle between auto/manual mode.

    Press ESC during automation -> bot pauses, you control the browser.
    Press ESC during manual mode -> bot resumes with next job.
    """

    def __init__(self):
        self.is_manual = False
        self._stop = False
        self._thread = None
        self._loop = None
        self._toggle_event = None
        self._old_settings = None
        self._fd = None

    def start(self, loop):
        if not sys.stdin.isatty():
            return
        import tty, termios
        self._loop = loop
        self._fd = sys.stdin.fileno()
        self._old_settings = termios.tcgetattr(self._fd)
        self._toggle_event = asyncio.Event()
        tty.setcbreak(self._fd)
        self._thread = threading.Thread(target=self._listen, daemon=True)
        self._thread.start()
        import atexit
        atexit.register(self.stop)
        logger.info("ESC monitor active — press ESC to toggle manual/auto mode")
        sys.stdout.write(
            "\r\n══════════════════════════════════════════════\r\n"
            "  ESC MONITOR ACTIVE — Press ESC to pause bot\r\n"
            "══════════════════════════════════════════════\r\n"
        )
        sys.stdout.flush()

    def stop(self):
        self._stop = True
        if self._old_settings and self._fd is not None:
            try:
                import termios
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old_settings)
            except Exception:
                pass
            self._old_settings = None

    def _listen(self):
        while not self._stop:
            try:
                r, _, _ = select.select([self._fd], [], [], 0.15)
                if not r:
                    continue
                ch = os.read(self._fd, 1)
                if ch == b'\x1b':
                    # Distinguish standalone ESC from escape sequences (arrow keys)
                    r2, _, _ = select.select([self._fd], [], [], 0.05)
                    if r2:
                        os.read(self._fd, 10)  # consume sequence
                        continue
                    # Standalone ESC — toggle
                    self.is_manual = not self.is_manual
                    if self._loop and self._toggle_event:
                        self._loop.call_soon_threadsafe(self._toggle_event.set)
                    if self.is_manual:
                        sys.stdout.write(
                            "\a\r\n  >>> MANUAL MODE — Browser is yours. Press ESC to resume bot. <<<\r\n"
                        )
                        logger.warning("ESC toggle: MANUAL MODE — bot paused, browser is yours")
                    else:
                        sys.stdout.write(
                            "\a\r\n  >>> AUTO MODE — Bot resuming. Press ESC to take over. <<<\r\n"
                        )
                        logger.warning("ESC toggle: AUTO MODE — bot resuming")
                    sys.stdout.flush()
            except (OSError, ValueError):
                break
            except Exception:
                continue

    async def wait_for_toggle(self):
        """Async: wait for next ESC press. Blocks forever if no terminal."""
        if self._loop:
            self._toggle_event = asyncio.Event()  # Fresh event — no stale state from prior ESC
            await self._toggle_event.wait()
        else:
            # No terminal — block forever (never resolve)
            await asyncio.Event().wait()
