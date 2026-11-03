import copy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

import numpy as np
from scipy import signal

sys.path.insert(0, str(Path(__file__).resolve().parent.parent/'src'))
from fl_studio_mcp.audio import mixing, song
from fl_studio_mcp.errors import FLError
from fl_studio_mcp.midi_export import arrangement_notes
from fl_studio_mcp.mix_match import fit_live, validate_controls
from fl_studio_mcp.recreation import read_json
from fl_studio_mcp.tools import FLSession
from test_recreation import analysis


def fixture(sr=16000, seconds=4):
    rng = np.random.default_rng(843)
    t = np.arange(sr*seconds)/sr
    env = .08+.4*np.maximum(0, np.sin(2*np.pi*2*t))**4
    noise = signal.lfilter([1], [1, -.75], rng.normal(0, .1, len(t)))
    left = (noise+np.sin(2*np.pi*220*t)*.2+np.sin(2*np.pi*880*t)*.1)*env
    return np.stack([left, .75*left+.25*np.roll(left, 180)], axis=1)


class OfflineMixTests(unittest.TestCase):
    def test_known_tone_width_balance_and_level_errors_improve(self):
        ref = fixture()
        dry = signal.sosfilt(signal.butter(1, 1200, fs=16000, output='sos'), ref, axis=0)*.45
        dry[:, 1] *= .7
        mixed, report = mixing.fit(ref, dry, 16000)
        self.assertLess(report['distance_after'], report['distance_before']*.8)
        self.assertLessEqual(report['true_peak_dbtp'], -.999)
        self.assertEqual(mixed.shape, dry.shape)
        self.assertTrue(np.isfinite(mixed).all())
        np.testing.assert_allclose(mixed, mixing.process(dry, 16000, report['recipe']), atol=1e-9)

    def test_identical_reference_is_not_worsened(self):
        ref = fixture()
        mixed, report = mixing.fit(ref, ref, 16000)
        self.assertFalse(report['accepted_processing'])
        self.assertAlmostEqual(report['distance_after'], 0, places=8)
        np.testing.assert_allclose(mixed, ref, atol=1e-12)

    def test_antiphase_audio_is_not_silence_and_peak_guard_is_oversampled(self):
        x = fixture()[:, 0]
        anti = np.stack([x, -x], axis=1)
        self.assertGreater(mixing.profile(anti, 16000)['width_db'], 20)
        hot = mixing.process(anti*8, 16000, {'gain_db':6})
        self.assertLessEqual(np.abs(signal.resample_poly(hot, 4, 1, axis=0)).max(), 10**(-1/20)+1e-9)

    def test_invalid_audio_rejected(self):
        for x in (np.zeros((16000, 2)), np.full((16000, 2), np.nan), np.ones((16000, 3))):
            with self.assertRaises(FLError):
                mixing.profile(x, 16000)


class DetailTests(unittest.TestCase):
    def test_audio_transcription_against_independent_pop_note_truth(self):
        from synth import make_song
        from benchmark_detail import expected, scores
        audio, sections = make_song(bpm=120, sr=44100, seed=1)
        # Match the PCM fixture format; no generated file is required by this test.
        audio = (audio*32767).astype(np.int16).astype(np.float32)/32768
        result = song.analyze_song(audio, 44100)
        notes, _ = arrangement_notes(result)
        truth = expected({'tempo':120, 'sections':sections})
        for role, minimum in {'kick':.95, 'snare':.90, 'hat':.82, 'bass':.92}.items():
            with self.subTest(role=role):
                self.assertGreater(scores(notes[role], truth[role], pitch=role=='bass')['f1'], minimum)

    def test_same_label_occurrences_keep_distinct_fills_velocity_and_bass(self):
        a = analysis()
        for i, section in enumerate(a['sections']):
            section['drum_events'] = {'snare':[{'start':3.13+i*.25,'length':.1,'velocity':.4+i*.4}]}
            section['bass'] = [{'pitch':33+i*7,'start':.25,'length':.23,'velocity':.62}]
        notes, _ = arrangement_notes(a)
        self.assertEqual(len(notes['kick']), 0)
        np.testing.assert_allclose([n['start'] for n in notes['snare']], [3.13, 11.38])
        self.assertEqual([n['pitch'] for n in notes['bass']], [33,40])
        self.assertEqual([n['velocity'] for n in notes['snare']], [.4,.8])

    def test_transient_extractor_keeps_one_bar_fill_and_microtiming(self):
        env = np.zeros(400)
        # two bars; the second has a one-off fill, shifted from the 16th grid.
        for position, amplitude in [(50,1.), (150,.8), (250,1.), (352,.9), (378,.6)]:
            env[position] = amplitude
        events = song.drum_events({'snare':env}, np.array([0,200,400]), [0,1])['snare']
        self.assertEqual(len(events), 5)
        self.assertAlmostEqual(events[-1]['start'], 7.56)
        self.assertGreater(events[0]['velocity'], events[-1]['velocity'])


class LiveMixTests(unittest.TestCase):
    def test_stereo_quantization_is_accepted_but_failed_writes_are_rejected(self):
        for fail_write in (False, True):
            session = Mock()
            session.info.return_value = {'bridge_version':8}
            value = [0.]
            session.mixer.side_effect = lambda tracks:{'tracks':[{'stereo_separation':value[0]}]}
            def write(track, stereo_separation, undo=False):
                if not fail_write:
                    value[0] = round(stereo_separation*64)/64
            session.set_mixer.side_effect = write
            session.ears.listener.record_playback.side_effect = lambda *args:(fixture(),16000,{'capture':'process'})
            with tempfile.TemporaryDirectory() as directory:
                args = (session, fixture(), 16000, Path(directory), [{'track':0,'kind':'stereo','step':.1}])
                if fail_write:
                    with self.assertRaisesRegex(FLError, 'readback'):
                        fit_live(*args, budget=2, seconds=4)
                else:
                    report = fit_live(*args, budget=2, seconds=4)
                    self.assertEqual(report['history'][1]['values'], [-.09375])
                self.assertEqual(value[0], 0.)

    def test_success_keeps_best_readback_and_cancel_restores_original(self):
        for cancel_after in (None, 3):
            session = Mock()
            session.info.return_value = {'bridge_version':8}
            value = [.7]
            calls = [0]
            session.mixer.side_effect = lambda tracks:{'tracks':[{'volume':value[0]}]}
            session.set_mixer.side_effect = lambda track, volume, undo=False:value.__setitem__(0, volume)
            session.ears.listener.record_playback.side_effect = lambda *args:(fixture()*value[0],16000,{})
            def cancelled():
                calls[0] += 1
                if calls[0] == cancel_after:
                    raise InterruptedError('cancelled')
            with tempfile.TemporaryDirectory() as directory:
                kwargs = dict(controls=[{'track':0,'kind':'volume','step':.1}], budget=5,seconds=4,cancelled=cancelled)
                if cancel_after:
                    with self.assertRaises(InterruptedError):
                        fit_live(session,fixture()*.5,16000,Path(directory),**kwargs)
                    self.assertEqual(value[0], .7)
                else:
                    result = fit_live(session,fixture()*.5,16000,Path(directory),**kwargs)
                    self.assertLess(result['distance_after'], result['distance_before'])
                    self.assertEqual(value[0], result['best_settings'][0]['value'])

    def test_validation_happens_before_any_fl_writes(self):
        session = FLSession()
        session._call = Mock()
        for kwargs in ({'volume':float('nan')}, {'pan':2}, {'eq':[{'band':3,'gain':.5}]}, {'eq':[{'band':0,'freq':-1}]}):
            with self.assertRaises(FLError):
                session.set_mixer(0, **kwargs)
        session._call.assert_not_called()
        with self.assertRaises(FLError):
            validate_controls([{'track':0,'kind':'effect','slot':0,'param':1,'min':.8,'max':.2}])

    def test_capture_failure_restores_exact_original_and_saves_revert(self):
        session = Mock()
        session.info.return_value = {'bridge_version':8}
        value = [.7]
        session.mixer.side_effect = lambda tracks:{'tracks':[{'volume':value[0]}]}
        session.set_mixer.side_effect = lambda track, volume, undo=False:value.__setitem__(0, volume)
        session.ears.listener.record_playback.side_effect = [(fixture(),16000,{}), RuntimeError('capture lost')]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            with self.assertRaisesRegex(RuntimeError, 'capture lost'):
                fit_live(session, fixture(), 16000, output, [{'track':0,'kind':'volume'}], budget=3, seconds=4)
            self.assertEqual(value[0], .7)
            self.assertEqual(read_json(output/'mixer-revert.json')['controls'][0]['value'], .7)
            self.assertEqual(read_json(output/'mixer-journal.json')['phase'], 'original_restored')


if __name__ == '__main__':
    unittest.main()
