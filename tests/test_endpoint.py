import unittest
from pathlib import Path

from app import endpoint
from app.s3_audio import _audio_segments, has_wake_audio, is_noise_transcript

FLOOR, SPEECH = -41.0, -18.0


def levels(*parts):
    out = []
    for level, seconds in parts:
        out += [level] * round(seconds * 10)
    return out


class EndpointDecisionTest(unittest.TestCase):
    def test_waits_while_speaking_and_through_short_pauses(self):
        self.assertIsNone(endpoint.decide(levels((FLOOR, 1.5), (SPEECH, 2)))[0])
        # 1.6 s mid-phrase pause, as in the live Russian query.
        self.assertIsNone(endpoint.decide(levels((FLOOR, 1.5), (SPEECH, 2), (FLOOR, 1.6)))[0])

    def test_ends_after_trailing_silence(self):
        decision, at = endpoint.decide(levels((FLOOR, 1.5), (SPEECH, 2), (FLOOR, 1.8)))
        self.assertEqual(decision, endpoint.SPEECH_END)
        self.assertAlmostEqual(at, 3.5)

    def test_gives_up_without_speech(self):
        self.assertIsNone(endpoint.decide(levels((FLOOR, 4.9)))[0])
        self.assertEqual(endpoint.decide(levels((FLOOR, 5)))[0], endpoint.NO_SPEECH_DETECTED)

    def test_caps_continuous_sound(self):
        decision, _ = endpoint.decide(levels((FLOOR, 1.5), (SPEECH, 11)))
        self.assertEqual(decision, endpoint.MAX_LENGTH)

    def test_floor_adapts_to_quiet_follow_on_gain(self):
        decision, at = endpoint.decide(levels((-59, 1.5), (-33, 0.5), (-59, 1.8)))
        self.assertEqual((decision, at), (endpoint.SPEECH_END, 2.0))


class TranscriptFilterTest(unittest.TestCase):
    def test_noise_transcripts(self):
        for text in ("", "おい", "...", "Thank you.", "you", "Продолжение следует..."):
            self.assertTrue(is_noise_transcript(text), text)
        for text in ("Hello?", "What's the weather tomorrow?", "Супер большая бутылка хубабубы."):
            self.assertFalse(is_noise_transcript(text), text)


STREAMS = Path("data/streams")
# Owned-device captures: (request id, expected decision, max decision time s).
LIVE = [
    ("775d9d88", endpoint.SPEECH_END, 4.0),   # "Hello" after the hotword
    ("f8fd5be7", endpoint.SPEECH_END, 8.5),   # long query with a 1.6 s pause
    ("a8e3be76", endpoint.SPEECH_END, 4.0),   # follow-on with only the chime
]


@unittest.skipUnless(STREAMS.is_dir(), "owned-device captures not present")
class CapturedStreamTest(unittest.TestCase):
    def test_live_captures_end_promptly(self):
        for prefix, expected, deadline in LIVE:
            matches = list(STREAMS.glob(prefix + "*-req.bin"))
            if not matches:
                continue
            body = matches[0].read_bytes()
            query = [audio for kind, audio in _audio_segments(body) if kind == 293101][-1]
            all_levels = endpoint.frame_levels(endpoint.decode_pcm(query))
            for count in range(1, len(all_levels) + 1):
                decision, _ = endpoint.decide(all_levels[:count])
                if decision:
                    break
            with self.subTest(prefix):
                self.assertEqual(decision, expected)
                self.assertLessEqual(count / 10, deadline)
                self.assertEqual(has_wake_audio(body), prefix != "a8e3be76")


if __name__ == "__main__":
    unittest.main()
