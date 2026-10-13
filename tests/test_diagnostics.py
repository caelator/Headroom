import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('diagnostics',Path(__file__).resolve().parents[1]/'scripts/diagnostics.py')
diagnostics=importlib.util.module_from_spec(spec);spec.loader.exec_module(diagnostics)

class DiagnosticsTests(unittest.TestCase):
    def test_runtime_environment_never_leaves_filtered_diagnostics(self):
        with patch.object(diagnostics.subprocess,'run') as run:
            run.return_value.returncode=0
            run.return_value.stdout=b'state = running\npid = 123\ninherited environment = {\n MODEL_API_KEY => PRIVATE_TEST_KEY\n}\n'
            result=diagnostics.agent_status('com.xxx.aiusage')
        self.assertTrue(result['running']);self.assertEqual(result['pid'],123)
        self.assertNotIn('PRIVATE_TEST_KEY',json.dumps(result));self.assertNotIn('MODEL_API_KEY',json.dumps(result))
    def test_unregistered_status(self):
        with patch.object(diagnostics.subprocess,'run') as run:
            run.return_value.returncode=1;result=diagnostics.agent_status('service')
        self.assertFalse(result['registered'])

if __name__=='__main__':unittest.main()
