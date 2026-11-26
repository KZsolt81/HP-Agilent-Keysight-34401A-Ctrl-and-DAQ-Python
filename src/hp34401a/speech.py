"""Voice announcements (port of the System.Speech features).

Uses ``pyttsx3`` (SAPI5 on Windows, NSSpeechSynthesizer on macOS, eSpeak on Linux).
If it is not installed, the feature reports itself unavailable and does nothing.
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Callable, Optional

from .engine import LogLevel, StatsTracker
from .formatting import speech_number
from .scpi import Function

#: words-per-minute used for the original's Slow/Medium/Fast/Very Fast/Fastest (SAPI rate 0..4).
RATE_WPM = {"Slow": 150, "Medium": 175, "Fast": 200, "Very Fast": 230, "Fastest": 260}


class Speaker:
    def __init__(self, log: Optional[Callable[[str, LogLevel], None]] = None):
        self._log = log
        self._q: "queue.Queue[Optional[str]]" = queue.Queue()
        self._engine = None
        self.available = False
        self.error: Optional[str] = None
        self.rate = "Medium"
        self.volume = 100
        self.gender = "male"
        self._settings_dirty = True
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="speech", daemon=True)
        self._thread.start()
        self._ready.wait(10)

    def _run(self) -> None:
        try:
            import pyttsx3

            self._engine = pyttsx3.init()
            self.available = True
        except Exception as e:  # ImportError, missing espeak, no audio device...
            self.error = str(e) or e.__class__.__name__
        self._ready.set()
        while True:
            text = self._q.get()
            if text is None:
                break
            if not self.available:
                continue
            try:
                if self._settings_dirty:
                    self._apply_settings()
                self._engine.say(text)
                self._engine.runAndWait()
            except Exception as e:
                if self._log:
                    self._log(f"Speech Synthesizer failed: {e}", LogLevel.ERROR)

    def _apply_settings(self) -> None:
        eng = self._engine
        eng.setProperty("rate", RATE_WPM.get(self.rate, 175))
        eng.setProperty("volume", max(0.0, min(1.0, self.volume / 100.0)))
        try:
            voices = eng.getProperty("voices") or []
            want = self.gender.lower()
            for v in voices:
                g = (getattr(v, "gender", "") or "").lower()
                name = (getattr(v, "name", "") or "").lower()
                if want in g or (want == "female" and any(n in name for n in ("zira", "female", "hazel", "susan", "samantha"))):
                    eng.setProperty("voice", v.id)
                    break
        except Exception:
            pass
        self._settings_dirty = False

    def configure(self, rate: Optional[str] = None, volume: Optional[int] = None, gender: Optional[str] = None) -> None:
        if rate is not None:
            self.rate = rate
        if volume is not None:
            self.volume = int(volume)
        if gender is not None:
            self.gender = gender
        self._settings_dirty = True

    def say(self, text: str) -> None:
        self._q.put(text)

    def busy(self) -> bool:
        return not self._q.empty()

    def shutdown(self) -> None:
        self._q.put(None)


class Announcer:
    """Continuous announcements and MIN/MAX threshold warnings."""

    def __init__(self, speaker: Speaker, stats: StatsTracker, function_getter: Callable[[], Function],
                 latest_getter: Callable[[], Optional[float]]):
        self.speaker = speaker
        self.stats = stats
        self.function_getter = function_getter
        self.latest_getter = latest_getter
        self.enabled = False
        self.precision = 1
        self.continuous_minutes: Optional[float] = None
        self.min_threshold: Optional[float] = None
        self.max_threshold: Optional[float] = None
        self._next_continuous = 0.0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="announcer", daemon=True)
        self._thread.start()

    def set_continuous(self, minutes: Optional[float]) -> None:
        self.continuous_minutes = minutes
        self._next_continuous = time.monotonic() + (minutes or 0) * 60

    def _word(self) -> str:
        return self.function_getter().speech

    def _num(self, v: float) -> str:
        return speech_number(v, self.precision)

    def _run(self) -> None:
        while not self._stop.wait(1.0):
            if not self.enabled or self.speaker.busy():
                continue
            now = time.monotonic()
            if self.continuous_minutes and now >= self._next_continuous:
                self._next_continuous = now + self.continuous_minutes * 60
                v = self.latest_getter()
                if v is not None:
                    if abs(v) > 999_999_999:
                        self.speaker.say(f"Overload {self._word()}")
                    else:
                        self.speaker.say(f"{self._num(v)} {self._word()}")
            mx, mn = self.stats.max, self.stats.min
            if self.max_threshold is not None and mx is not None and mx >= self.max_threshold:
                self.speaker.say(f"Warning, maximum value of {self._num(self.max_threshold)} {self._word()} reached.")
                self.speaker.say("maximum value is " + ("overload" if mx > 9_999_999_999 else self._num(mx)) + f" {self._word()}")
            if self.min_threshold is not None and mn is not None and mn <= self.min_threshold:
                self.speaker.say(f"Warning, minimum value of {self._num(self.min_threshold)} {self._word()} reached.")
                self.speaker.say("minimum value is " + ("overload" if mn < -9_999_999_999 else self._num(mn)) + f" {self._word()}")

    def stop(self) -> None:
        self._stop.set()
