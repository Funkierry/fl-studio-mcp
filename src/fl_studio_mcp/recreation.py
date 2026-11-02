"""Persistent music-recreation jobs, shared by stdio, HTTP and one-shot CLI clients."""
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid

from .errors import FLError
from .store import data_dir

TERMINAL = {'done', 'error', 'cancelled'}
ACTIVE = {'queued', 'running'}


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def process_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == 'nt':
        k = ctypes.WinDLL('kernel32', use_last_error=True)
        k.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        k.OpenProcess.restype = ctypes.c_void_p
        k.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        k.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = k.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(k.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            k.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def hidden_process_options():
    if os.name != 'nt':
        return {'start_new_session': True}
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0
    return {'startupinfo': startup, 'creationflags': subprocess.CREATE_NO_WINDOW}


def running_fl():
    if os.name != 'nt':
        return None
    from .audio.capture import find_process
    return find_process()


class Recreation:
    def __init__(self, session=None, root=None):
        self.session = session
        self.root = Path(root) if root else data_dir()
        self.root.mkdir(parents=True, exist_ok=True)
        self.jobs = self.root/'recreation-jobs'
        self.jobs.mkdir(exist_ok=True)
        self.config_path = self.root/'recreation-config.json'

    def configure(self, template_path=None, role_patterns=None, fl_executable=None, output_root=None):
        from .flp import inspect_template
        config = read_json(self.config_path) if self.config_path.exists() else {}
        if role_patterns is not None and template_path is None:
            raise FLError('Provide template_path together with role_patterns')
        if template_path is not None:
            source = Path(template_path).resolve(strict=True)
            detail = inspect_template(source, role_patterns)
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            target = self.root/'templates'/(digest+'.flp')
            target.parent.mkdir(exist_ok=True)
            if not target.exists():
                shutil.copy2(source, target)
            config.update(template_path=str(target), template_sha256=digest,
                          role_patterns={r:v['pattern'] for r,v in detail['roles'].items()},
                          template_info=detail)
        if fl_executable is not None:
            exe = Path(fl_executable).resolve(strict=True)
            if exe.name.lower() not in ('fl64.exe', 'fl.exe', 'fl64 (scaled).exe'):
                raise FLError('fl_executable must be an installed FL Studio executable')
            config['fl_executable'] = str(exe)
        if output_root is not None:
            folder = Path(output_root)
            if not folder.is_absolute():
                raise FLError('output_root must be an absolute local path')
            folder.mkdir(parents=True, exist_ok=True)
            config['output_root'] = str(folder.resolve())
        if any(value is not None for value in (template_path, fl_executable, output_root)):
            atomic_json(self.config_path, config)
        return {'configured':all(k in config for k in ('template_path', 'fl_executable', 'output_root')),
                'config':config, 'config_file':str(self.config_path),
                'song_requirements':'FL 24 instrument template; close FL before command-line rendering. The MCP never closes your existing FL instance.',
                'sound_requirements':'FL open with the bridge and a suitable plugin on the requested channel.'}

    def _folder(self, task_id):
        if not isinstance(task_id, str) or not re.fullmatch(r'recreate-[a-f0-9]{16}', task_id):
            raise FLError('Invalid recreation task_id')
        folder = self.jobs/task_id
        if not (folder/'state.json').exists():
            raise FLError('Unknown recreation task_id')
        return folder

    def _lock(self, task_id):
        lock = self.root/'recreation.lock'
        try:
            with lock.open('x', encoding='utf-8') as f:
                f.write(task_id)
        except FileExistsError:
            owner = lock.read_text(encoding='utf-8').strip()
            if owner == task_id:
                return
            try:
                state = self.status(owner)
            except FLError:
                raise FLError('Invalid recreation lock; inspect recreation.lock before retrying')
            if state['status'] in TERMINAL:
                lock.unlink(missing_ok=True)
                return self._lock(task_id)
            raise FLError(f'Recreation task {owner} is still pending; finish or cancel it first')

    def _unlock(self, task_id):
        lock = self.root/'recreation.lock'
        if lock.exists() and lock.read_text(encoding='utf-8').strip() == task_id:
            lock.unlink(missing_ok=True)

    def start(self, path, kind='auto', output_dir=None, tempo_hint=None, render=True,
              start=None, end=None, channel=None, params=None, budget=40, pitch=None, note_len_s=None):
        from .tools import _finite_number, parse_pitch
        if kind not in ('auto','song','sound') or not isinstance(render, bool):
            raise FLError('kind must be auto, song or sound; render must be boolean')
        source = Path(path).resolve(strict=True)
        if not source.is_file():
            raise FLError('path must point to an audio file')
        start = 0.0 if start is None else _finite_number(start, 'start')
        end = None if end is None else _finite_number(end, 'end')
        if start < 0 or end is not None and end <= start:
            raise FLError('Require 0 <= start < end')
        if tempo_hint is not None:
            tempo_hint = _finite_number(tempo_hint, 'tempo_hint')
            if not 30 <= tempo_hint <= 300:
                raise FLError('tempo_hint must be between 30 and 300 BPM')
        if channel is not None and (isinstance(channel,bool) or not isinstance(channel,int) or channel < 0):
            raise FLError('channel must be a nonnegative integer')
        if params is not None and (not isinstance(params,list) or not params or any(type(p) is not int or p < 0 for p in params)):
            raise FLError('params must contain nonnegative integer parameter indices')
        if type(budget) is not int or not 2 <= budget <= 200:
            raise FLError('budget must be an integer from 2 to 200')
        if pitch is not None:
            pitch = parse_pitch(pitch)
        if note_len_s is not None:
            note_len_s = _finite_number(note_len_s,'note_len_s')
            if not .05 <= note_len_s <= 5:
                raise FLError('note_len_s must be between .05 and 5')
        config = self.configure()['config']
        base = Path(output_dir or config.get('output_root', self.root/'exports'))
        if not base.is_absolute():
            raise FLError('output_dir must be an absolute directory')
        if kind == 'song' and ('template_path' not in config or render and 'fl_executable' not in config):
            raise FLError('Call fl_recreation_config with an instrument template and FL executable first')
        if kind == 'sound' and channel is None:
            raise FLError('A live plugin channel is required for sound matching')
        if self.session is not None and self.session._ears is not None and self.session.ears.jobs.active():
            raise FLError('Finish the active fl_match_sound job before starting a recreation')
        task_id = 'recreate-'+uuid.uuid4().hex[:16]
        folder = self.jobs/task_id
        folder.mkdir()
        output = (base/task_id).resolve()
        request = {'path':str(source), 'kind':kind, 'output_dir':str(output), 'tempo_hint':tempo_hint,
                   'render':render, 'start':start, 'end':end, 'channel':channel, 'params':params,
                   'budget':budget, 'pitch':pitch, 'note_len_s':note_len_s, 'config':config,
                   'out_port':getattr(self.session,'out_port',None), 'in_port':getattr(self.session,'in_port',None)}
        atomic_json(folder/'request.json', request)
        atomic_json(folder/'state.json', {'task_id':task_id,'status':'queued','phase':'queued','progress':0,
                    'message':'Starting audio reconstruction','created':time.time(),'updated':time.time(),
                    'output_dir':str(output),'artifacts':{},'worker_pid':None})
        try:
            self._lock(task_id)
            self._spawn(folder)
        except Exception as exc:
            state = read_json(folder/'state.json')
            state.update(status='error',message=str(exc),updated=time.time())
            atomic_json(folder/'state.json',state)
            self._unlock(task_id)
            raise
        return self.status(task_id)

    def _spawn(self, folder):
        if getattr(sys, 'frozen', False):
            cmd = [sys.executable, 'recreation-worker', str(folder)]
        else:
            cmd = [sys.executable, '-m', 'fl_studio_mcp.recreation', str(folder)]
        env = dict(os.environ)
        if getattr(sys, 'frozen', False):
            # PyInstaller >= 6.9 otherwise reuses the parent's temporary extraction,
            # which disappears when a one-shot MCP/CLI client exits.
            env['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
        env['PYTHONPATH'] = str(Path(__file__).resolve().parent.parent)+os.pathsep+env.get('PYTHONPATH','')
        env['FLSTUDIO_MCP_DATA'] = str(self.root)
        # Explicitly detached stdin/out: the worker survives one-shot CLI/MCP disconnects.
        with (folder/'worker.log').open('ab') as log:
            process = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,stdout=log,stderr=log,
                                       env=env,cwd=str(folder),**hidden_process_options())
        atomic_json(folder/'process.json', {'pid':process.pid,'started':time.time()})

    def status(self, task_id, cancel=False, resume=False):
        folder = self._folder(task_id)
        state = read_json(folder/'state.json')
        if cancel and resume:
            raise FLError('Choose cancel or resume, not both')
        if cancel and state['status'] not in TERMINAL:
            (folder/'cancel').touch()
            if state['status'] == 'waiting_fl_close':
                state.update(status='cancelled', message='Cancelled; generated files kept', updated=time.time())
                atomic_json(folder/'state.json',state)
                self._unlock(task_id)
        if resume:
            if state['status'] != 'waiting_fl_close':
                raise FLError('Only waiting_fl_close tasks can be resumed; do not replay active or completed jobs')
            if running_fl():
                return dict(state, next_action='Save and close FL Studio, then call fl_recreation_status(resume=true).')
            # A per-task exclusive flag prevents simultaneous clients from spawning two workers.
            try:
                with (folder/'resume.lock').open('x'):
                    pass
            except FileExistsError:
                return read_json(folder/'state.json')
            self._lock(task_id)
            state.update(status='queued', phase='render', message='Resuming render', updated=time.time())
            atomic_json(folder/'state.json',state)
            try:
                self._spawn(folder)
            except Exception:
                state.update(status='waiting_fl_close',message='Could not start render worker',updated=time.time())
                atomic_json(folder/'state.json',state)
                (folder/'resume.lock').unlink(missing_ok=True)
                raise
        state = read_json(folder/'state.json')
        process_file = folder/'process.json'
        if state['status'] in ACTIVE and process_file.exists() and time.time()-state['updated'] > 10:
            pid = read_json(process_file)['pid']
            if not process_alive(pid):
                state.update(status='error',message='Worker exited unexpectedly; see worker.log. Completed artifacts were kept.',updated=time.time())
                atomic_json(folder/'state.json',state)
                self._unlock(task_id)
        return dict(state, status_file=str(folder/'state.json'), log_file=str(folder/'worker.log'),
                    next_action=('Save and close FL Studio, then call fl_recreation_status(resume=true).'
                                 if state['status']=='waiting_fl_close' else 'Poll fl_recreation_status' if state['status'] in ACTIVE else None))


def worker_main(folder):
    from .recreation_worker import run
    run(Path(folder))


if __name__ == '__main__':
    worker_main(sys.argv[1])
