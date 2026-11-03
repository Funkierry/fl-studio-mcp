import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent/'src'))
from fl_studio_mcp.audio import dsp


class DecoderTests(unittest.TestCase):
    def test_explicit_decoder_path_with_spaces_does_not_need_path(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'reference song.mp3'
            source.touch()
            decoder=Path(directory)/'my ffmpeg.exe'
            decoder.touch()
            audio=np.array([[.1,-.1],[.2,-.2]],dtype=np.float32)
            result=Mock(returncode=0,stdout=audio.tobytes())
            with patch.dict(os.environ,{'FLSTUDIO_MCP_FFMPEG':str(decoder)}), \
                 patch.object(dsp.subprocess,'run',return_value=result) as run, \
                 patch.object(dsp.shutil,'which',return_value=None):
                actual,sr=dsp.load_audio(source)
            np.testing.assert_array_equal(actual,audio)
            self.assertEqual(sr,44100)
            self.assertEqual(run.call_args.args[0][0],str(decoder))
            self.assertIn(str(source),run.call_args.args[0])
            self.assertIn('-nostdin',run.call_args.args[0])

    def test_invalid_explicit_decoder_fails_without_launching(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'reference.mp3'
            source.touch()
            with patch.dict(os.environ,{'FLSTUDIO_MCP_FFMPEG':str(source.parent/'missing.exe')}), \
                 patch.object(dsp.subprocess,'run') as run:
                with self.assertRaisesRegex(dsp.AudioError,'FLSTUDIO_MCP_FFMPEG'):
                    dsp.load_audio(source)
                run.assert_not_called()
