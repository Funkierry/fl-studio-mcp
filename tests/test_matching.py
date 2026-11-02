import sys
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))
from fl_studio_mcp import match
from fl_studio_mcp.ears import Ears
from fl_studio_mcp.errors import AudioError, FLError
from fl_studio_mcp.store import Jobs


class Session:
    def __init__(self):
        self.value = .1
        self.writes = []
    def _call(self, command, **args):
        if command == 'get_param_values':
            return {'values': {str(p): self.value for p in args['params']}}
        self.value = args['changes'][0]['value']
        self.writes.append(self.value)
        return {}
    def project(self):
        return {'playing': False, 'channels': [{'index': 4, 'name': 'FLEX Bass', 'plugin': 'FLEX'}]}
    def list_params(self, *args, **kwargs):
        return {'params': [{'param': 10, 'name': 'Filter', 'value': .1}]}


class MatchingTests(unittest.TestCase):
    def setUp(self):
        self.session = Session()
        self.store = types.SimpleNamespace(save_audio=lambda a, sr, name: name+'.wav', put=lambda *a: 'match-test')
        self.listener = types.SimpleNamespace(store=self.store, render_note=lambda *a: (self.session.value, 44100))
        self.params = [{'param': 10, 'name': 'Filter', 'value': .1}]
        for patcher in (patch.object(match.time, 'sleep'),
                        patch.object(match.timbre, 'describe', side_effect=lambda audio, sr: audio),
                        patch.object(match.timbre, 'distance', side_effect=lambda a, b: (abs(a-b), {})),
                        patch.object(match.timbre, 'public', side_effect=lambda target: {'test_value': target}),
                        patch.object(match.timbre, 'compare', return_value=['test comparison'])):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_search(self, **kwargs):
        return match.run_match(self.session, self.listener, 4, .75, self.params, 45, budget=12, **kwargs)

    def test_real_integer_parameter_indexes_optimize_and_keep_best(self):
        result = self.run_search(reference_path='reference.wav')
        self.assertEqual(result['evaluations'], 12)
        self.assertAlmostEqual(self.session.value, .75)
        self.assertEqual(result['best_distance'], 0)
        self.assertEqual(result['revert'], [{'param': 10, 'value': .1}])
        self.assertEqual(result['audio_files']['reference'], 'reference.wav')
        self.assertIn('not a perceptual similarity', result['metric_note'])
        self.assertEqual(result['match_id'], 'match-test')

    def test_capture_failure_restores_original_value(self):
        count = [0]
        def render(*args):
            count[0] += 1
            if count[0] == 3:
                raise OSError('capture lost')
            return self.session.value, 44100
        self.listener.render_note = render
        with self.assertRaisesRegex(FLError, 'original settings restored'):
            self.run_search()
        self.assertEqual(self.session.value, .1)

    def test_all_silent_is_failure_with_original_restored(self):
        with patch.object(match.timbre, 'describe', side_effect=AudioError('silent')):
            with self.assertRaisesRegex(FLError, 'all matching renders were silent'):
                self.run_search()
        self.assertEqual(self.session.value, .1)

    def test_cancel_preserves_best_evaluated_setting(self):
        cancel = threading.Event()
        result = self.run_search(cancel=cancel, report=lambda *a: cancel.set())
        self.assertTrue(result['cancelled'])
        self.assertEqual(result['evaluations'], 2)
        self.assertLessEqual(result['best_distance'], result['start_distance'])
        self.assertEqual(self.session.value, result['best_settings'][0]['value'])

    def test_parameter_suggestion_keeps_volume_envelopes_but_not_output_gain(self):
        choices = [{'param': i, 'name': name} for i, name in enumerate([
            '音量包络起音', '输出音量', 'EQ Quality', 'Filter', '音高', 'Amp Envelope Decay'])]
        self.assertEqual({p['param'] for p in match.suggest_params(choices)}, {0, 3, 5})


class MatchingPreflightTests(unittest.TestCase):
    def setUp(self):
        self.ears = Ears.__new__(Ears)
        self.ears.session = Session()
        self.started = []
        self.ears.jobs = types.SimpleNamespace(active=lambda: None,
            start=lambda *args, **kwargs: self.started.append((args, kwargs)) or 'match-job')
        self.ears.store = types.SimpleNamespace(get=lambda aid: {'private': {'descriptor': {
            'midi_note': 45, 'decay_to_-20db_s': 1}, 'audio_path': 'reference.wav'}})
        self.ears.listener = object()

    def test_explicit_midi_zero_is_not_replaced_by_detected_pitch(self):
        result = self.ears.match_sound('sound-test', 4, pitch=0, budget=2)
        self.assertEqual(result['pitch'], 0)
        self.assertEqual(self.started[0][0][7], 0)

    def test_invalid_values_fail_before_starting_job(self):
        for args in ({'budget': 0}, {'budget': 10.5}, {'budget': float('nan')},
                     {'note_len_s': -1}, {'note_len_s': float('inf')}, {'params': [99]},
                     {'params': []}, {'start': 3, 'end': 1}, {'pitch': 128}):
            with self.subTest(args=args), self.assertRaises(FLError):
                self.ears.match_sound('sound-test', 4, **args)
        self.assertEqual(self.started, [])

    def test_playing_transport_rejected(self):
        self.ears.session.project = lambda: {'playing': True}
        with self.assertRaisesRegex(FLError, 'Stop FL playback'):
            self.ears.match_sound('sound-test', 4)
        self.assertEqual(self.started, [])


class JobExclusionTests(unittest.TestCase):
    def test_second_job_does_not_start_while_first_is_recording(self):
        jobs, release, complete = Jobs(), threading.Event(), threading.Event()
        def work(**kwargs):
            release.wait(2)
            complete.set()
        try:
            jobs.start('match', work)
            with self.assertRaisesRegex(ValueError, 'still running'):
                jobs.start('match', work)
        finally:
            release.set()
            self.assertTrue(complete.wait(2))


if __name__ == '__main__':
    unittest.main()
