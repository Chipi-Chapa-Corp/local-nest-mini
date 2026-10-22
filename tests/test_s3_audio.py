import unittest

from app.native import _field, _frame
from app.s3_audio import extract_flac_segments, extract_query_audio


def audio(extension, data):
    return _frame(_field(extension, _field(1, data)))


class S3AudioTest(unittest.TestCase):
    def test_followup_preserves_all_audio_across_interleaved_control(self):
        first = b'fLaC' + b'first' * 300
        second = b'continuation' * 400
        # A control message includes misleading audio-like tags and magic bytes.
        control = _frame(_field(232532745, _field(1, b'fLaC' + b'x' * 600)))
        body = audio(293101, first) + control + audio(293101, second)
        self.assertEqual(extract_flac_segments(body), [first + second])
        self.assertEqual(extract_query_audio(body), first + second)

    def test_short_query_is_selected_instead_of_long_wake_recording(self):
        wake = b'fLaC' + b'wake' * 200
        query = b'fLaCquery'
        body = audio(62724734, wake) + audio(293101, query)
        self.assertEqual(extract_flac_segments(body), [wake, query])
        self.assertEqual(extract_query_audio(body), query)
        self.assertEqual(extract_query_audio(audio(62724734, wake)), b'')

    def test_partial_network_frame_does_not_corrupt_preceding_audio(self):
        first, second = b'fLaCfirst', b'second'
        tail = audio(293101, second)
        body = audio(293101, first)
        for size in range(len(tail)):
            self.assertEqual(extract_query_audio(body + tail[:size]), first)
        self.assertEqual(extract_query_audio(body + tail), first + second)

    def test_new_flac_header_starts_a_new_query_segment(self):
        first, second = b'fLaCfirst', b'fLaCsecond'
        body = audio(293101, first) + audio(293101, second)
        self.assertEqual(extract_flac_segments(body), [first, second])
        self.assertEqual(extract_query_audio(body), second)
