"""Detached worker: analyze -> editable FLP/MIDI -> native render -> audio checks."""
import hashlib
import ctypes
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import traceback

import numpy as np
from scipy.io import wavfile

from .audio import dsp, perception, song, mixing
from .errors import FLError
from .recreation import Recreation, atomic_json, hidden_process_options, read_json, running_fl


class Cancelled(Exception):
    pass


def launch_renderer(command, output, log):
    """Do not leak the frozen Python DLL search directory into native FL."""
    env = os.environ.copy()
    frozen_windows = sys.platform == 'win32' and getattr(sys, 'frozen', False)
    if frozen_windows:
        bundle = str(Path(sys._MEIPASS).resolve()).lower()
        env['PATH'] = os.pathsep.join(p for p in env.get('PATH','').split(os.pathsep)
                                    if not str(Path(p).resolve()).lower().startswith(bundle+os.sep)
                                    and str(Path(p).resolve()).lower() != bundle)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.SetDllDirectoryW.argtypes = [ctypes.c_wchar_p]
        if not kernel.SetDllDirectoryW(None):
            raise ctypes.WinError(ctypes.get_last_error())
    try:
        return subprocess.Popen(command,cwd=str(output),stdin=subprocess.DEVNULL,stdout=log,stderr=log,
                                env=env,**hidden_process_options())
    finally:
        if frozen_windows:
            kernel.SetDllDirectoryW(sys._MEIPASS)


def closed_complete_wav(path, quiet_seconds=10):
    """Only accept a finalized RIFF file no process still has open for writing."""
    if os.name != 'nt' or not path.exists():
        return False
    stat = path.stat()
    if stat.st_size < 44 or time.time()-stat.st_mtime < quiet_seconds:
        return False
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [ctypes.c_wchar_p,ctypes.c_ulong,ctypes.c_ulong,ctypes.c_void_p,
                                  ctypes.c_ulong,ctypes.c_ulong,ctypes.c_void_p]
    kernel.CreateFileW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    # Share mode zero requires exclusive access; a live exporter prevents this open.
    handle = kernel.CreateFileW(str(path),0x80000000,0,None,3,0,None)
    if handle == ctypes.c_void_p(-1).value:
        return False
    kernel.CloseHandle(handle)
    with path.open('rb') as stream:
        header = stream.read(12)
    return (header[:4] == b'RIFF' and header[8:] == b'WAVE'
            and int.from_bytes(header[4:8],'little')+8 == stat.st_size)


def stop_renderer(process):
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def verify_audio(reference_path, rendered_path, arrangement, destination):
    """Audio integrity and objective differences, explicitly not a fidelity score."""
    ref, sr = dsp.load_audio(reference_path, sr=44100)
    audio, _ = dsp.load_audio(rendered_path, sr=sr)
    expected = arrangement['duration_s']
    duration = len(audio)/sr
    peak = float(np.max(np.abs(audio)))
    finite = bool(np.isfinite(audio).all())
    rms = float(np.sqrt(np.mean(audio.astype(np.float64)**2)))
    if not finite or not expected-.15 <= duration <= expected+30 or rms < .0001:
        raise FLError(f'Render validation failed: duration={duration:.3f}, expected={expected:.3f}, RMS={rms:.6f}, finite={finite}')
    clipped = int(np.count_nonzero(np.abs(audio) >= .9999))
    measurements = []
    for section in arrangement['sections']:
        start = section['start_bar']*4*60/arrangement['tempo_bpm']
        end = (section['start_bar']+section['bars'])*4*60/arrangement['tempo_bpm']
        part = audio[round(start*sr):round(end*sr)]
        measurements.append({'name':section['name'],'start_s':start,'end_s':end,
                             'rms':float(np.sqrt(np.mean(part.astype(np.float64)**2))) if part.size else 0})
    ref_offset = arrangement.get('first_downbeat_s',0)
    aligned = ref[round(ref_offset*sr):round((ref_offset+expected)*sr)]
    target_metrics = perception.metrics(audio[:round(expected*sr)],sr)
    reference_metrics = perception.metrics(aligned,sr)
    chosen = next((s for s in arrangement['sections'] if s['name'].lower()=='chorus'), arrangement['sections'][0])
    start = chosen['start_bar']*4*60/arrangement['tempo_bpm']
    a = ref[round((ref_offset+start)*sr):round((ref_offset+start+16)*sr)]
    b = audio[round(start*sr):round((start+16)*sr)]
    n = min(len(a),len(b))
    a, b = a[:n], b[:n]
    if a.shape[1] == 1:
        a = np.repeat(a,2,axis=1)
    if b.shape[1] == 1:
        b = np.repeat(b,2,axis=1)
    if a.shape[1] != 2 or b.shape[1] != 2 or n == 0:
        raise FLError('Comparison requires mono or stereo nonempty audio')
    levels = [float(np.sqrt(np.mean(x.astype(np.float64)**2))) for x in (a,b)]
    gain = [min(levels)/max(level,1e-10) for level in levels]
    ab = np.concatenate([a*gain[0],np.zeros((int(.75*sr),2)),b*gain[1]])
    ab /= max(1,float(np.max(np.abs(ab)))/.98)
    comparison = Path(destination)/'comparison-reference-then-recreated.wav'
    wavfile.write(comparison,sr,np.round(ab*32767).astype(np.int16))
    report = {'duration_s':duration,'expected_duration_s':expected,'sample_rate':sr,
              'peak_dbfs':round(float(20*np.log10(max(peak,1e-12))),3),
              'clipped_samples':clipped,'sections':measurements,
              'reference_metrics':reference_metrics,'reconstruction_metrics':target_metrics,
              'relative_band_difference_db':perception.compare_bands(target_metrics,reference_metrics),
              'checks':{'finite_audio':finite,'complete_duration':True,'non_silent':True,'no_digital_clipping':clipped==0},
              'warnings':(['Digital clipping detected; reduce template levels and recreate.'] if clipped else [])+
                         (['One or more sections are nearly silent; review the transcription.'] if any(s['rms']<.0001 for s in measurements) else []),
              'comparison':{'path':str(comparison),'order':['reference','silence 0.75 s','recreated'],
                            'excerpt_seconds_each':n/sr,'level_matching':'RMS attenuation only'},
              'limitations':['Heuristic 4/4 drum/chord/bass transcription, no vocals or independent lead.',
                             'Template instruments approximate the reference; no automatic stem-wise timbre matching.',
                             'Audio checks do not establish note accuracy or perceptual similarity.']}
    atomic_json(Path(destination)/'verification.json',report)
    return report


def run(folder):
    manager = Recreation(root=folder.parent.parent)
    task_id = folder.name
    state = read_json(folder/'state.json')
    request = read_json(folder/'request.json')
    output = Path(request['output_dir'])
    output.mkdir(parents=True,exist_ok=True)
    config = request['config']
    cancel_file = folder/'cancel'
    state['worker_pid'] = os.getpid()

    def update(phase, progress, message, **extra):
        state.update(status='running',phase=phase,progress=round(progress,3),message=message,updated=time.time(),**extra)
        atomic_json(folder/'state.json',state)

    def cancelled():
        if cancel_file.exists():
            raise Cancelled()

    try:
        cancelled()
        if not (output/'arrangement.json').exists():
            update('analyze',.03,'Reading reference audio')
            audio, sr = dsp.load_audio(request['path'],start=request['start'],end=request['end'])
            duration = len(audio)/sr
            if not .1 <= duration <= 600 or not np.isfinite(audio).all() or np.max(np.abs(audio)) < .0001:
                raise FLError('Reference must contain finite, audible audio, between 0.1 and 600 seconds')
            kind = request['kind']
            if kind == 'auto':
                kind = 'song' if duration >= 20 else 'sound'
            state['kind'] = kind
            reference = output/'reference.wav'
            wavfile.write(reference, sr, audio.astype(np.float32))
            state['artifacts']['reference'] = str(reference)
            cancelled()
            if kind == 'mix':
                update('mix', .1, 'Matching reference mix')
                if request.get('source_path'):
                    dry, _ = dsp.load_audio(request['source_path'], sr=sr)
                    if not .1 <= len(dry)/sr <= 600:
                        raise FLError('Mix source must be between 0.1 and 600 seconds')
                    mixed, matched = mixing.fit(audio, dry, sr,
                        progress=lambda count, score: update('mix', min(.85, .1+count*.06), f'Mix candidate {count}: distance {score:.3f}'),
                        cancelled=cancelled)
                    cancelled()
                    wavfile.write(output/'before.wav', sr, dry.astype(np.float32))
                    dsp.save_wav(output/'mixed.wav', mixed, sr)
                    matched['comparison'] = mixing.save_comparison(audio, dry, mixed, sr, output/'mix-comparison.wav')
                else:
                    from .mix_match import fit_live
                    from .tools import FLSession
                    from .midi_transport import DEFAULT_IN_PORT, DEFAULT_OUT_PORT
                    session = FLSession(request.get('out_port') or DEFAULT_OUT_PORT, request.get('in_port') or DEFAULT_IN_PORT)
                    try:
                        matched = fit_live(session, audio, sr, output, controls=request['controls'],
                            budget=request['budget'], seconds=request['seconds'], mode=request['mode'],
                            progress=lambda count, score: update('mix', .1+.8*count/request['budget'], f'Live mix evaluation {count}: distance {score:.3f}'),
                            cancelled=cancelled)
                    finally:
                        session.close()
                atomic_json(output/'mix-report.json', matched)
                state['artifacts'].update(audio=str(output/'mixed.wav'), before=str(output/'before.wav'),
                    mix_report=str(output/'mix-report.json'), comparison=matched['comparison']['path'])
                if (output/'mixer-revert.json').exists():
                    state['artifacts']['mixer_revert'] = str(output/'mixer-revert.json')
                state['result'] = matched
                state.update(status='done', phase='complete', progress=1, message='Reference mix matching complete', updated=time.time())
                atomic_json(folder/'state.json', state)
                return
            if kind == 'sound':
                if request['channel'] is None:
                    raise FLError('Short audio was classified as sound; provide a live FL plugin channel or set kind=song')
                from .tools import FLSession
                from .midi_transport import DEFAULT_IN_PORT, DEFAULT_OUT_PORT
                session = FLSession(request.get('out_port') or DEFAULT_OUT_PORT,request.get('in_port') or DEFAULT_IN_PORT)
                try:
                    job = session.ears.match_sound(str(reference),request['channel'],request['params'],request['budget'],
                                                   pitch=request['pitch'],note_len_s=request['note_len_s'])
                    while True:
                        result = session.ears.job(job['job_id'],cancel=cancel_file.exists())
                        update('sound_match',.05+.9*result['progress'],result['message'])
                        if result['status'] in ('done','error','cancelled'):
                            break
                        time.sleep(.5)
                    if result['status']=='error':
                        raise FLError(result['error'])
                    matched = result['result']
                    for label,path in matched.get('audio_files',{}).items():
                        dest = output/(label+'.wav')
                        if Path(path).resolve() != dest.resolve():
                            shutil.copy2(path,dest)
                        state['artifacts'][label] = str(dest)
                    atomic_json(output/'sound-match.json',matched)
                    state['artifacts']['match_result'] = str(output/'sound-match.json')
                    state['result'] = matched
                    state.update(status='cancelled' if result['status']=='cancelled' else 'done',
                                 phase='complete',progress=1,message='Sound match finished; best parameters applied, revert values saved',updated=time.time())
                    atomic_json(folder/'state.json',state)
                    return
                finally:
                    session.close()
            if 'template_path' not in config:
                raise FLError('Song reconstruction requires fl_recreation_config with an instrument template')
            template = Path(config['template_path'])
            if hashlib.sha256(template.read_bytes()).hexdigest() != config['template_sha256']:
                raise FLError('Instrument template changed since configuration; register it again')
            update('analyze',.1,'Estimating tempo, sections, harmony, drums and bass')
            analysis = song.analyze_song(audio,sr,tempo_hint=request['tempo_hint'])
            analysis['source'] = request['path']
            atomic_json(output/'analysis.json',analysis)
            state['artifacts']['analysis'] = str(output/'analysis.json')
            cancelled()
            update('build',.35,'Creating editable FL project, playlist and MIDI')
            from .flp import build_project
            from .midi_export import export_arrangement
            midi = export_arrangement(analysis,output/'arrangement.mid')
            arrangement = build_project(template,output/'recreated.flp',analysis,config.get('role_patterns'))
            arrangement['first_downbeat_s'] = analysis.get('first_downbeat_s',0)
            arrangement['midi'] = midi
            atomic_json(output/'arrangement.json',arrangement)
            state['artifacts'].update(project=arrangement['project'],midi=midi['midi'],arrangement=str(output/'arrangement.json'))
        else:
            arrangement = read_json(output/'arrangement.json')
        cancelled()
        if not request['render']:
            state.update(status='done',phase='complete',progress=1,
                         message='Editable project and MIDI ready; audio rendering was explicitly disabled',updated=time.time())
            atomic_json(folder/'state.json',state)
            return
        if running_fl():
            state.update(status='waiting_fl_close',phase='render',progress=.5,
                         message='FL is already open. Save and close it, then resume; existing FL instances are never closed by this tool.',updated=time.time())
            atomic_json(folder/'state.json',state)
            return
        executable = config.get('fl_executable')
        if not executable or not Path(executable).is_file():
            raise FLError('Configure the installed FL executable before rendering')
        rendered = output/'recreated.wav'
        if rendered.exists():
            raise FLError('An unexpected render file already exists; refusing to treat it as fresh output')
        update('render',.55,'Rendering through FL Studio command line')
        # Official single-project CLI, each argument separate (no shell interpolation).
        command = [executable,'/Ewav','/R',str(output/'recreated.flp')]
        report = None
        verified_before_exit = False
        with (output/'render.log').open('ab') as log:
            process = launch_renderer(command,output,log)
            deadline = time.monotonic()+max(180,arrangement['duration_s']*10)
            try:
                while process.poll() is None:
                    cancelled()
                    if closed_complete_wav(rendered):
                        report = verify_audio(output/'reference.wav',rendered,arrangement,output)
                        cancelled()
                        # FL can finish export but hang during shutdown. Only clean up
                        # our own batch process after the closed WAV passes validation.
                        update('verify',.95,'WAV verified; closing completed batch renderer',
                               render_completion='verified_closed_wav')
                        stop_renderer(process)
                        verified_before_exit = True
                        break
                    if time.monotonic() > deadline:
                        raise FLError('FL rendering timed out; inspect missing-plugin or export dialogs')
                    update('render',.6,'FL is rendering the project',render_pid=process.pid)
                    time.sleep(1)
            except BaseException:
                # This process was started only for a generated project; never kill pre-existing FL.
                stop_renderer(process)
                raise
        if (process.returncode != 0 and not verified_before_exit) or not rendered.exists():
            raise FLError(f'FL render exited with {process.returncode} without a verified WAV; see render.log. An already-open FL instance can prevent CLI rendering.')
        cancelled()
        update('verify',.9,'Checking complete duration, signal levels and reference differences')
        if report is None:
            report = verify_audio(output/'reference.wav',rendered,arrangement,output)
        state['artifacts'].update(audio=str(rendered),verification=str(output/'verification.json'),comparison=report['comparison']['path'])
        if request.get('reference_mix', True):
            update('mix', .94, 'Matching rendered audio to reference tone, dynamics and stereo image')
            reference_audio, rate = dsp.load_audio(output/'reference.wav', sr=44100)
            dry, _ = dsp.load_audio(rendered, sr=rate)
            offset = round(arrangement.get('first_downbeat_s', 0)*rate)
            reference_audio = reference_audio[offset:offset+len(dry)]
            mixed, mix_report = mixing.fit(reference_audio, dry, rate, cancelled=cancelled)
            cancelled()
            dsp.save_wav(output/'mixed.wav', mixed, rate)
            mix_report['comparison'] = mixing.save_comparison(reference_audio, dry, mixed, rate, output/'mix-comparison.wav')
            atomic_json(output/'mix-report.json', mix_report)
            state['artifacts'].update(dry_audio=str(rendered), audio=str(output/'mixed.wav'),
                                      mix_report=str(output/'mix-report.json'), comparison=mix_report['comparison']['path'])
        state['result'] = {'bars':arrangement['bars'],'tempo_bpm':arrangement['tempo_bpm'],
                           'duration_s':report['duration_s'],'notes':arrangement['note_counts'],
                           'checks':report['checks'],'warnings':report['warnings'],'limitations':report['limitations']}
        if request.get('reference_mix', True):
            state['result']['mix'] = {key: mix_report[key] for key in ('distance_before', 'distance_after', 'true_peak_dbtp', 'limitations')}
        state.update(status='done',phase='complete',progress=1,message='FL project, MIDI, WAV and A/B comparison ready',updated=time.time())
        atomic_json(folder/'state.json',state)
    except Cancelled:
        state.update(status='cancelled',message='Cancelled; generated artifacts kept',updated=time.time())
        atomic_json(folder/'state.json',state)
    except Exception as exc:
        traceback.print_exc()
        state.update(status='error',message=f'{type(exc).__name__}: {exc}',updated=time.time())
        atomic_json(folder/'state.json',state)
    finally:
        if (output/'mixer-revert.json').exists():
            state['artifacts']['mixer_revert'] = str(output/'mixer-revert.json')
            if (output/'mixer-journal.json').exists():
                state['artifacts']['mixer_journal'] = str(output/'mixer-journal.json')
            atomic_json(folder/'state.json', state)
        (folder/'resume.lock').unlink(missing_ok=True)
        if state['status'] in ('done','error','cancelled'):
            manager._unlock(task_id)
