"""Wait for each Cast sentence to finish and stop promptly on cancellation."""

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app import player
from app.tasks import TaskStore, TaskCancelled


class PlayerTests(unittest.TestCase):
    def setUp(self):
        self.store = TaskStore()
        self.task = self.store.create()
        self.media = Mock()
        self.media.status = SimpleNamespace(player_state="PLAYING", content_id="http://local/line.wav",
                                            idle_reason=None)
        self.cast = SimpleNamespace(media_controller=self.media)
        self.player = player._Player()
        self.player._cast = self.cast
        for p in (patch.object(player, 'TASKS', self.store), patch.object(player, 'ENABLED', True),
                  patch.object(player.time, 'sleep')):
            p.start()
            self.addCleanup(p.stop)

    def test_waiting_play_does_not_return_until_finished(self):
        states = iter(['PLAYING', 'BUFFERING', 'IDLE'])
        def update():
            self.media.status.player_state = next(states)
            self.media.status.idle_reason = 'FINISHED'
        self.media.update_status.side_effect = update
        result = self.player.play('http://local/line.wav', self.task['id'], wait=True, duration=2)
        self.assertTrue(result['played'])
        self.assertEqual(self.media.update_status.call_count, 3)
        self.media.play_media.assert_called_once()

    def test_cancel_stops_current_sentence_without_restarting_it(self):
        self.media.update_status.side_effect = lambda: self.store.cancel(self.task['id'])
        with self.assertRaises(TaskCancelled):
            self.player.play('http://local/line.wav', self.task['id'], wait=True, duration=2)
        self.media.stop.assert_called_once()
        self.media.play_media.assert_called_once()

    def test_another_content_item_is_not_mistaken_for_completion(self):
        self.media.update_status.side_effect = lambda: setattr(self.media.status, 'content_id', 'another-url')
        with self.assertRaisesRegex(RuntimeError, 'was replaced'):
            self.player.play('http://local/line.wav', self.task['id'], wait=True, duration=2)
        self.media.play_media.assert_called_once()


if __name__ == '__main__':
    unittest.main()
