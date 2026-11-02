import hashlib
import json
import os
import struct
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from scipy.io import wavfile

sys.path.insert(0,str(Path(__file__).resolve().parent.parent/'src'))
from fl_studio_mcp.errors import FLError
from fl_studio_mcp.flp import Project, ROLES, build_project, inspect_template
from fl_studio_mcp.recreation import Recreation, atomic_json, read_json
from fl_studio_mcp.recreation_worker import closed_complete_wav, verify_audio
from fl_studio_mcp.server import McpServer
from fl_studio_mcp.tools import FLSession
from test_recreation import analysis


def fixture(path):
    # Independent minimal binary fixture: five populated patterns and untrimmed clips.
    events=[(199,b'24.2.1.4526\0'),(156,struct.pack('<I',120000)),(9,b'\x01'),(213,b'opaque plugin state\0\xff')]
    clips=[]
    for i,role in enumerate(ROLES,1):
        note=struct.pack('<IHHIHH8B',0,0,i-1,96,60,0,120,0,64,0,64,100,128,128)
        events.extend([(65,struct.pack('<H',i)),(224,note),(65,struct.pack('<H',i)),
                       (193,('Full '+role.title()+'\0').encode('utf-16le'))])
        clip=bytearray(60)
        struct.pack_into('<IHHIHH',clip,0,0,20480,20480+i,384,500-i,0)
        clip[16:24]=bytes([120,0,64,0,64,100,128,128])
        clip[24:32]=b'\xff'*8
        clips.append(bytes(clip))
    events.extend([(99,b'\0\0'),(241,'Old arrangement\0'.encode('utf-16le')),(233,b''.join(clips)),
                   (148,struct.pack('<I',20000)),(205,'Old marker\0'.encode('utf-16le'))])
    data=bytearray()
    for eid,payload in events:
        data.append(eid)
        if eid>=192:
            size=len(payload)
            while size>127:
                data.append((size&127)|128)
                size>>=7
            data.append(size)
        data.extend(payload)
    Path(path).write_bytes(struct.pack('<4sIHHH4sI',b'FLhd',6,0,5,96,b'FLdt',len(data))+data)


class TemplateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.source=self.root/'template.flp'
        fixture(self.source)

    def test_generated_project_has_full_timeline_correct_notes_and_opaque_plugins(self):
        original=self.source.read_bytes()
        destination=self.root/'new.flp'
        result=build_project(self.source,destination,analysis())
        self.assertEqual(original,self.source.read_bytes())
        self.assertEqual(result['bars'],4)
        project=Project(destination)
        self.assertIn((213,b'opaque plugin state\0\xff'),project.events)
        self.assertEqual([int.from_bytes(d,'little') for e,d in project.events if e==148],[0,8*96])
        self.assertEqual(sum(e==205 for e,d in project.events),2)
        clips=next(d for e,d in project.events if e==233)
        self.assertEqual(len(clips),300)
        for i in range(5):
            c=clips[i*60:(i+1)*60]
            self.assertEqual(struct.unpack_from('<IHHIHH',c),(0,20480,20481+i,16*96,499-i,0))
            self.assertEqual(c[24:32],b'\xff'*8)
        patterns=project.patterns()
        bass=patterns[5]['notes']
        self.assertEqual([struct.unpack_from('<I',bass,i)[0] for i in range(0,len(bass),24)],[0,8*96,12*96])
        kick=patterns[1]['notes']
        self.assertTrue(all(struct.unpack_from('<H',kick,i+12)[0]==60 for i in range(0,len(kick),24)))
        self.assertTrue(all(struct.unpack_from('<H',bass,i+6)[0]==4 for i in range(0,len(bass),24)))

    def test_existing_destination_and_source_cannot_be_overwritten(self):
        original=self.source.read_bytes()
        with self.assertRaises(FLError):build_project(self.source,self.source,analysis())
        self.assertEqual(original,self.source.read_bytes())

    def test_corrupt_file_fails_closed(self):
        raw=self.source.read_bytes()
        for data in (raw[:-1],raw[:30],raw.replace(b'24.2.1',b'25.2.1'),b'not an flp'):
            path=self.root/'bad.flp';path.write_bytes(data)
            with self.assertRaises(FLError):Project(path)

    def test_trimmed_clip_rejected(self):
        p=Project(self.source)
        events=[(e,d[:24]+b'\0'*8+d[32:] if e==233 else d) for e,d in p.events]
        p.write(self.root/'trimmed.flp',events)
        with self.assertRaisesRegex(FLError,'untrimmed'):inspect_template(self.root/'trimmed.flp')

    def test_role_mapping_must_use_five_distinct_patterns(self):
        with self.assertRaises(FLError):inspect_template(self.source,{r:1 for r in ROLES})


class PersistentTaskTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.manager=Recreation(root=self.root/'data')
        template=self.root/'template.flp';fixture(template)
        exe=self.root/'FL64.exe';exe.write_bytes(b'placeholder, never executed')
        self.manager.configure(str(template),fl_executable=str(exe),output_root=str(self.root/'outputs'))
        self.audio=self.root/'reference.wav'
        wavfile.write(self.audio,8000,np.sin(np.arange(8000)*.1).astype(np.float32))

    def start(self):
        with patch.object(self.manager,'_spawn'):
            return self.manager.start(str(self.audio),kind='song',render=False)

    def test_configuration_snapshots_template(self):
        cfg=self.manager.configure()['config']
        original=(self.root/'template.flp').read_bytes()
        (self.root/'template.flp').write_bytes(b'edited later')
        self.assertEqual(Path(cfg['template_path']).read_bytes(),original)
        self.assertEqual(cfg['template_sha256'],hashlib.sha256(original).hexdigest())

    def test_reconnect_reads_same_task_and_no_duplicate_start(self):
        first=self.start()
        other=Recreation(root=self.manager.root)
        self.assertEqual(other.status(first['task_id'])['output_dir'],first['output_dir'])
        with patch.object(other,'_spawn') as spawn,self.assertRaisesRegex(FLError,'still pending'):
            other.start(str(self.audio),kind='song')
        spawn.assert_not_called()

    def test_waiting_resume_does_not_spawn_while_fl_open(self):
        result=self.start();folder=self.manager._folder(result['task_id'])
        state=read_json(folder/'state.json');state['status']='waiting_fl_close';atomic_json(folder/'state.json',state)
        with patch('fl_studio_mcp.recreation.running_fl',return_value=123),patch.object(self.manager,'_spawn') as spawn:
            self.assertEqual(self.manager.status(result['task_id'],resume=True)['status'],'waiting_fl_close')
            spawn.assert_not_called()

    def test_cancel_waiting_task_keeps_files_and_releases_lock(self):
        r=self.start();folder=self.manager._folder(r['task_id'])
        state=read_json(folder/'state.json');state['status']='waiting_fl_close';atomic_json(folder/'state.json',state)
        self.assertEqual(self.manager.status(r['task_id'],cancel=True)['status'],'cancelled')
        self.assertTrue((folder/'request.json').exists())
        self.assertFalse((self.manager.root/'recreation.lock').exists())

    def test_invalid_task_id_cannot_escape_job_directory(self):
        for task in ('../../secret','recreate-../x',''):
            with self.assertRaises(FLError):self.manager.status(task)

    def test_worker_crash_is_not_reported_as_success(self):
        r=self.start();folder=self.manager._folder(r['task_id'])
        state=read_json(folder/'state.json');state.update(status='running',updated=time.time()-20)
        atomic_json(folder/'state.json',state);atomic_json(folder/'process.json',{'pid':123})
        with patch('fl_studio_mcp.recreation.process_alive',return_value=False):
            self.assertEqual(self.manager.status(r['task_id'])['status'],'error')

    def test_invalid_arguments_do_not_start_a_job(self):
        for kw in ({'kind':'wrong'},{'start':float('nan')},{'start':2,'end':1},
                   {'kind':'sound'},{'budget':1},{'tempo_hint':float('inf')},{'params':[True]},
                   {'output_dir':'relative'},{'render':'yes'}):
            with self.subTest(kw=kw),self.assertRaises(FLError):self.manager.start(str(self.audio),**kw)
        self.assertFalse((self.manager.root/'recreation.lock').exists())

    def test_frozen_worker_does_not_depend_on_parent_extraction_directory(self):
        from unittest.mock import Mock
        r=self.start();folder=self.manager._folder(r['task_id'])
        with patch.object(sys,'frozen',True,create=True),patch('subprocess.Popen',return_value=Mock(pid=42)) as popen:
            self.manager._spawn(folder)
        args=popen.call_args.kwargs
        self.assertEqual(args['env']['PYINSTALLER_RESET_ENVIRONMENT'],'1')
        self.assertEqual(popen.call_args.args[0][1],'recreation-worker')

    def test_protocol_exposes_unified_tools_without_live_fl(self):
        s=FLSession();s._recreation=self.manager
        server=McpServer(s)
        names={t['name'] for t in server.handle({'id':1,'method':'tools/list'})['result']['tools']}
        self.assertTrue({'fl_recreate_music','fl_recreation_status','fl_recreation_config'}<=names)
        response=server.handle({'id':2,'method':'tools/call','params':{'name':'fl_recreation_config'}})
        self.assertFalse(response['result'].get('isError'))
        self.assertTrue(json.loads(response['result']['content'][0]['text'])['configured'])


class RenderValidationTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt','Windows file sharing check')
    def test_only_closed_finalized_wav_can_finish_a_lingering_renderer(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'render.wav'
            wavfile.write(path,8000,np.ones((8000,2),np.int16))
            self.assertFalse(closed_complete_wav(path))
            os.utime(path,(time.time()-15,time.time()-15))
            with path.open('rb'):
                self.assertFalse(closed_complete_wav(path))
            self.assertTrue(closed_complete_wav(path))
            path.write_bytes(path.read_bytes()[:-12])
            os.utime(path,(time.time()-15,time.time()-15))
            self.assertFalse(closed_complete_wav(path))

    def test_duration_and_silence_fail_instead_of_false_completion(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);source=root/'ref.wav';render=root/'render.wav'
            wavfile.write(source,8000,np.ones((8000,2),np.float32)*.1)
            arrangement={'duration_s':8,'tempo_bpm':120,'sections':[{'name':'Verse','start_bar':0,'bars':4}]}
            for audio in (np.zeros((64000,2),np.float32),np.ones((8000,2),np.float32)*.1):
                wavfile.write(render,8000,audio)
                with self.assertRaisesRegex(FLError,'validation failed'):verify_audio(source,render,arrangement,root)


if __name__=='__main__':unittest.main()
