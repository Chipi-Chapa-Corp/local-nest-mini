"""Incremental sentence boundaries for streamed assistant text."""


class LineBuffer:
    """Emit complete lines or sentences as soon as their boundary is known."""

    def __init__(self, emit):
        self.emit = emit
        self.buffer = ""
        self.emitted = 0

    def feed(self, text):
        pieces = text.split("\n")
        for line in pieces[:-1]:
            self.buffer += line
            self.finish()
        self.buffer += pieces[-1]
        self._drain(False)

    def finish(self):
        self._drain(True)
        self._emit(self.buffer)
        self.buffer = ""

    def _emit(self, text):
        if text.strip():
            self.emit(text.strip())
            self.emitted += 1

    def _drain(self, final):
        start = 0
        index = 0
        while index < len(self.buffer):
            if self.buffer[index] not in ".!?":
                index += 1
                continue
            end = index + 1
            while end < len(self.buffer) and self.buffer[end] in '.!?"\'”’»)]}':
                end += 1
            # One character of lookahead keeps decimals and punctuation
            # runs split across network deltas intact.
            if end == len(self.buffer) and not final:
                break
            if (self.buffer[index] == "." and index > 0
                    and self.buffer[index - 1].isdigit()
                    and end < len(self.buffer) and self.buffer[end].isdigit()):
                index = end
                continue
            if end < len(self.buffer) and not self.buffer[end].isspace():
                index = end
                continue
            self._emit(self.buffer[start:end])
            start = end
            index = end
        self.buffer = self.buffer[start:]
