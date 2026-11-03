"""No registry writes: test spare-port selection against a simulated device inventory."""
import contextlib
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent/'src'))
from fl_studio_mcp import setup_wizard as setup


class RoutingSetupTests(unittest.TestCase):
    def parking(self, devices, reply=10):
        dev = r'Test\Devices'
        inventory = {'input':devices.get('input',{}), 'output':devices.get('output',{})}
        def read(path, name):
            direction = 'input' if '\\MIDI input\\' in path else 'output'
            return inventory[direction].get(path.rsplit('\\',1)[1])
        def open_key(root, path):
            return contextlib.nullcontext('input' if path.endswith('input') else 'output')
        with patch.object(setup,'_get_string',side_effect=read), \
             patch.object(setup.winreg,'OpenKey',side_effect=open_key), \
             patch.object(setup.winreg,'QueryInfoKey',side_effect=lambda k:(len(inventory[k]),0,0)), \
             patch.object(setup.winreg,'EnumKey',side_effect=lambda k,i:list(inventory[k])[i]):
            return setup._parking_output_port(dev,'FL Agent',reply)

    def test_unassigned_missing_and_conflicting_outputs_receive_explicit_port(self):
        for existing in (None,'-1','10'):
            self.assertEqual(self.parking({'output':{'FL Agent':existing,'FL Agent Reply':'10'}}),9)

    def test_other_devices_are_never_reassigned_or_collided_with(self):
        devices={'input':{'Keyboard':'9','Pads':'255'},'output':{'Synth':'254','FL Agent':'10'}}
        self.assertEqual(self.parking(devices),253)

    def test_existing_separate_output_is_preserved(self):
        self.assertEqual(self.parking({'output':{'FL Agent':'22'}}),22)

    def test_port_zero_and_exhausted_inventory(self):
        self.assertEqual(self.parking({},reply=0),255)
        self.assertIsNone(self.parking({'input':{str(i):str(i) for i in range(256)}}))


if __name__ == '__main__':
    unittest.main()
