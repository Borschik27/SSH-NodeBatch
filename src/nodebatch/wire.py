"""Bounded incremental decoder for the remote helper's private output stream."""
from .i18n import tr
import base64
import json

MAX_FRAME = 32768
VERSION = 2
PREPARATION_REASONS = {"preparation_failed", "shell_unavailable", "command_too_large", "process_creation_failed"}


class WireError(ValueError):
    pass


class Decoder:
    def __init__(self, token, session=""):
        self.token = token
        self.session = session
        self.buffer = bytearray()
        self.ready = False
        self.result = None
        self.failed = False
        self.started = False
        self.not_started = None

    def feed(self, data, final=False):
        try:
            return self._feed(data, final)
        except WireError:
            self.result = None
            self.not_started = None
            self.failed = True
            raise

    def _feed(self, data, final):
        events = []
        # The caller reads bounded chunks; a peer cannot accumulate an unbounded line.
        self.buffer.extend(data)
        while b"\n" in self.buffer:
            raw, _, tail = self.buffer.partition(b"\n")
            self.buffer = bytearray(tail)
            if len(raw) > MAX_FRAME:
                raise WireError(tr('Protocol message is too long'))
            try:
                message = json.loads(raw)
                if (not isinstance(message, dict) or type(message.get("v")) is not int or
                        message.get("v") != VERSION or self.failed):
                    raise ValueError()
                kind = message["type"]
                if not self.ready:
                    expected = {"v": VERSION, "type": "ready"}
                    if self.session:
                        expected["session"] = self.session
                    if message != expected:
                        raise ValueError()
                    self.ready = True
                    events.append(("ready", None))
                    continue
                if message.get("token") != self.token or self.result is not None:
                    raise ValueError()
                if kind == "started":
                    if self.started:
                        raise ValueError()
                    self.started = True
                    events.append(("started", None))
                elif kind == "not_started":
                    if self.started or message["reason"] not in PREPARATION_REASONS:
                        raise ValueError()
                    self.not_started = message["reason"]
                    self.failed = True
                    events.append(("not_started", None))
                elif kind == "data":
                    if not self.started:
                        raise ValueError()
                    if message["channel"] not in ("stdout", "stderr"):
                        raise ValueError()
                    decoded = base64.b64decode(message["data"], validate=True)
                    if len(decoded) > 16384:
                        raise ValueError()
                    events.append((message["channel"], decoded))
                elif kind == "result":
                    if not self.started:
                        raise ValueError()
                    code = message["code"]
                    if type(code) is not int or not -255 <= code <= 255:
                        raise ValueError()
                    incomplete = message.get("output_incomplete", False)
                    if type(incomplete) is not bool:
                        raise ValueError()
                    self.result = code
                    if incomplete:
                        events.append(("output_incomplete", None))
                elif kind == "error":
                    if not self.started or message.get("reason") != "execution_failed":
                        raise ValueError()
                    self.failed = True
                    events.append(("error", None))
                else:
                    raise ValueError()
            except (ValueError, KeyError, TypeError, UnicodeError) as exc:
                raise WireError(tr('Invalid execution protocol message')) from exc
        if len(self.buffer) > MAX_FRAME or (final and self.buffer):
            raise WireError(tr('Incomplete execution protocol message'))
        return events
