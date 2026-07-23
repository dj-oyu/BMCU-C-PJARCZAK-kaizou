"""Small Pico-local runtime log and exception containment helpers."""

try:
    import ujson as json
except ImportError:
    import json
try:
    import uio as io
except ImportError:
    import io
import sys


def _traceback_text(error):
    stream = io.StringIO()
    printer = getattr(sys, "print_exception", None)
    if printer is not None:
        printer(error, stream)
        return stream.getvalue()
    try:
        import traceback
        return "".join(traceback.format_exception(
            type(error), error, error.__traceback__))
    except Exception:
        return "%s: %s" % (type(error).__name__, error)


class PicoRuntimeLog:
    """Bounded RAM event log with one flash-backed last-crash record."""

    def __init__(self, clock_ms, limit=32, crash_path="pico_crash.json"):
        self.clock_ms = clock_ms
        self.limit = max(8, int(limit))
        self.crash_path = crash_path
        self.entries = []
        self.next_sequence = 1
        self.exception_count = 0
        self._last_exception_key = None
        self._last_exception_ms = None
        self._last_persist_key = None
        self._last_persist_ms = None
        self.previous_crash = self._load_previous_crash()
        if self.previous_crash is not None:
            self.add("warning", "runtime", "previous crash recovered", {
                "component": self.previous_crash.get("component"),
                "message": self.previous_crash.get("message"),
            })

    @staticmethod
    def _clean(value, limit=320):
        text = str(value).replace("\r", " ").replace("\n", " ")
        return text[:limit]

    def _load_previous_crash(self):
        if not self.crash_path:
            return None
        try:
            with open(self.crash_path, "r") as source:
                value = json.loads(source.read())
            return value if isinstance(value, dict) else None
        except (OSError, ValueError, TypeError):
            return None

    def add(self, level, component, message, details=None):
        entry = {
            "sequence": self.next_sequence,
            "uptime_ms": int(self.clock_ms()),
            "level": self._clean(level, 16),
            "component": self._clean(component, 40),
            "message": self._clean(message),
        }
        self.next_sequence += 1
        if details is not None:
            entry["details"] = details
        self.entries.append(entry)
        if len(self.entries) > self.limit:
            drop_index = 0
            for index, candidate in enumerate(self.entries):
                if candidate["level"] != "error":
                    drop_index = index
                    break
            self.entries.pop(drop_index)
        return entry

    def info(self, component, message, details=None):
        return self.add("info", component, message, details)

    def warning(self, component, message, details=None):
        return self.add("warning", component, message, details)

    def exception(self, component, error):
        self.exception_count += 1
        now_ms = int(self.clock_ms())
        key = (str(component), type(error).__name__, str(error))
        elapsed = (None if self._last_exception_ms is None
                   else now_ms - self._last_exception_ms)
        if (key == self._last_exception_key and elapsed is not None and
                0 <= elapsed < 5000 and self.entries):
            entry = self.entries[-1]
            details = entry.setdefault("details", {})
            details["suppressed"] = details.get("suppressed", 0) + 1
            details["exception_count"] = self.exception_count
            return entry

        traceback_text = _traceback_text(error)[-1200:]
        # Keep one full traceback in RAM; older entries retain their summary.
        for previous in self.entries:
            details = previous.get("details")
            if isinstance(details, dict):
                details.pop("traceback", None)
        entry = self.add("error", component, error, {
            "exception": type(error).__name__,
            "traceback": traceback_text,
            "exception_count": self.exception_count,
        })
        self._last_exception_key = key
        self._last_exception_ms = now_ms
        persist_elapsed = (None if self._last_persist_ms is None
                           else now_ms - self._last_persist_ms)
        should_persist = (
            key != self._last_persist_key or
            persist_elapsed is None or
            persist_elapsed < 0 or
            persist_elapsed >= 60000
        )
        if self.crash_path and should_persist:
            crash = dict(entry)
            try:
                with open(self.crash_path, "w") as target:
                    target.write(json.dumps(crash))
                self._last_persist_key = key
                self._last_persist_ms = now_ms
            except OSError:
                pass
        return entry

    def snapshot(self, limit=None, include_details=True):
        entries = self.entries if limit is None else self.entries[-limit:]
        if include_details:
            result_entries = list(entries)
        else:
            result_entries = [{
                "sequence": entry["sequence"],
                "uptime_ms": entry["uptime_ms"],
                "level": entry["level"],
                "component": entry["component"],
                "message": entry["message"],
            } for entry in entries]
        return {
            "entries": result_entries,
            "exception_count": self.exception_count,
            "previous_crash": self.previous_crash if include_details else None,
        }


def guarded_call(runtime_log, component, callback, recovery=None):
    """Run one cooperative task without allowing it to kill the main loop."""
    try:
        callback()
        return True
    except Exception as error:
        try:
            runtime_log.exception(component, error)
        except Exception:
            pass
        if recovery is not None:
            try:
                recovery()
            except Exception as recovery_error:
                try:
                    runtime_log.exception(
                        component + ".recovery", recovery_error)
                except Exception:
                    pass
        return False
