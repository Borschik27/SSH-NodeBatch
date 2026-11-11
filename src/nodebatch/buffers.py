from .i18n import tr
from collections import deque


class TextBuffer:
    """Bound both text bytes and entry count, with visible loss accounting."""
    def __init__(self, entries=(), *, maxlen=2000, max_bytes=4 * 1048576):
        self.entries = deque()
        self.maxlen, self.max_bytes = maxlen, max_bytes
        self.bytes = self.dropped = 0
        for entry in entries:
            self.append(entry)

    def append(self, text):
        encoded = text.encode("utf-8")
        if len(encoded) > self.max_bytes:
            text = tr('[GUI: beginning of entry omitted] ') + encoded[-(self.max_bytes - 128):].decode("utf-8", "replace")
            encoded = text.encode("utf-8")
            self.dropped += 1
        while self.entries and (len(self.entries) >= self.maxlen or self.bytes + len(encoded) > self.max_bytes):
            _, size = self.entries.popleft()
            self.bytes -= size
            self.dropped += 1
        self.entries.append((text, len(encoded)))
        self.bytes += len(encoded)

    def clear(self):
        self.entries.clear()
        self.bytes = self.dropped = 0

    def __iter__(self):
        return (text for text, _ in self.entries)

    def __len__(self):
        return len(self.entries)
