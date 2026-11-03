"""Independent hit/onset metrics, including false positives (not rest-step accuracy).

Run after make_samples.py. --baseline compares the committed v0.6.0 algorithm
without checking it out or changing the active source tree.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import types

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT/'src'))
from fl_studio_mcp.audio import dsp, song
from fl_studio_mcp import midi_export
from synth import ROOT as BASS_ROOT


def scores(found, truth, tolerance=.10, pitch=False):
    available = set(range(len(truth)))
    errors = []
    for note in sorted(found, key=lambda n:n['start']):
        candidates = [i for i in available if abs(truth[i]['start']-note['start']) <= tolerance
                      and (not pitch or truth[i]['pitch'] == note['pitch'])]
        if candidates:
            i = min(candidates, key=lambda i:abs(truth[i]['start']-note['start']))
            errors.append(abs(truth[i]['start']-note['start']))
            available.remove(i)
    tp = len(errors)
    return {'found':len(found), 'truth':len(truth), 'precision':tp/max(1,len(found)),
            'recall':tp/max(1,len(truth)), 'f1':2*tp/max(1,len(found)+len(truth)),
            'onset_mae_beats':float(np.mean(errors)) if errors else None}


def expected(truth):
    result = {k:[] for k in ('kick','snare','hat','bass')}
    for s in truth['sections']:
        for bar in range(s['bars']):
            offset = (s['start_bar']+bar)*4
            for role in ('kick','snare','hat'):
                for step, hit in enumerate(s['drums'][role]):
                    if hit != '.':
                        swing = .25 if truth['tempo'] == 85 and step % 2 else 0.
                        result[role].append({'start':offset+(step+swing)/4})
            if s['bass']:
                for i in range(8):
                    if s['bass'] == 'offbeat' and i % 2 == 0:
                        continue
                    chord = s['chords'][bar % len(s['chords'])]
                    root = BASS_ROOT.get(chord, {'Dm':50,'C':48,'G':43,'Am':45}.get(chord))
                    result['bass'].append({'start':offset+i/2, 'pitch':root-12+(12 if s['bass']=='octaves' and i%2 else 0)})
    return result


def baseline_module(name, path):
    module = types.ModuleType(name)
    module.__package__ = name.rpartition('.')[0]
    exec(subprocess.check_output(['git','show','v0.6.0:'+path], cwd=ROOT).decode('utf-8'), module.__dict__)
    return module


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--baseline', action='store_true')
    parser.add_argument('--output')
    args = parser.parse_args()
    engines = {'current': (song, midi_export)}
    if args.baseline:
        engines['v0.6.0'] = (baseline_module('fl_studio_mcp.audio.old_song', 'src/fl_studio_mcp/audio/song.py'),
                              baseline_module('fl_studio_mcp.old_midi', 'src/fl_studio_mcp/midi_export.py'))
    report = {}
    for wav in sorted((ROOT/'samples'/'songs').glob('*.wav')):
        audio, sr = dsp.load_audio(wav)
        truth = json.loads(wav.with_suffix('.truth.json').read_text(encoding='utf-8'))
        target = expected(truth)
        report[wav.stem] = {}
        for name, (analyzer, exporter) in engines.items():
            result = analyzer.analyze_song(audio, sr)
            notes, _ = exporter.arrangement_notes(result)
            report[wav.stem][name] = {role:scores(notes[role], events, pitch=role=='bass') for role, events in target.items()}
    text = json.dumps(report, indent=2)
    print(text)
    if args.output:
        Path(args.output).write_text(text, encoding='utf-8')


if __name__ == '__main__':
    main()
