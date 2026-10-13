import importlib.util
import json
from pathlib import Path
import plistlib
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('install_support',ROOT/'scripts/install_support.py')
installer=importlib.util.module_from_spec(spec);spec.loader.exec_module(installer)


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.data=installer.private_directory(self.root/'data')
        self.plist=self.root/'agents/test.plist';self.plist.parent.mkdir()
        self.old=plistlib.dumps({'Label':'test','EnvironmentVariables':{'PRIVATE_TEST_KEY':'original-sentinel'}})
        self.plist.write_bytes(self.old)
        self.bundle=self.root/'Applications/Headroom.app';self.bundle.mkdir(parents=True)
        (self.bundle/'binary').write_text('previous-build')
        self.candidate=self.root/'Applications/candidate.app';self.candidate.mkdir()
        (self.candidate/'binary').write_text('candidate-build')
        self.receipt=self.data/'installation.json';self.receipt.write_text('{"schema":1,"old":true}')
        self.before_receipt=self.receipt.read_bytes()
        self.running=patch.object(installer,'agent_status',return_value={'registered':True,'running':True,'pid':1});self.running.start();self.addCleanup(self.running.stop)
        self.stop=patch.object(installer,'bootout');self.stop.start();self.addCleanup(self.stop.stop)

    def activate(self,validate=lambda:None,complete=None):
        return installer.activate('test',self.plist,{'Label':'test','ProgramArguments':['candidate']},self.data,validate,
            bundle=self.bundle,candidate_bundle=self.candidate,complete=complete)

    def test_failed_registration_restores_original_app_plist_and_receipt(self):
        with patch.object(installer,'bootstrap',side_effect=[installer.InstallError('failed'),None]):
            with self.assertRaisesRegex(installer.InstallError,'previous installation was restored'):self.activate()
        self.assertEqual(self.plist.read_bytes(),self.old)
        self.assertEqual((self.bundle/'binary').read_text(),'previous-build')
        self.assertEqual(self.receipt.read_bytes(),self.before_receipt)
        self.assertFalse((self.data/'pending-install.json').exists())
    def test_failed_readiness_rolls_back_and_preserves_failed_candidate(self):
        def fail():raise installer.InstallError('not ready')
        with patch.object(installer,'bootstrap'):
            with self.assertRaises(installer.InstallError):self.activate(fail)
        self.assertEqual((self.bundle/'binary').read_text(),'previous-build')
        self.assertTrue(list(self.bundle.parent.glob('.Headroom.failed-*.app')))
    def test_success_preserves_original_inode_without_copying_environment(self):
        original_inode=self.plist.stat().st_ino
        with patch.object(installer,'bootstrap'):result=self.activate()
        before=Path(result['plist_backup'])
        self.assertEqual(before.stat().st_ino,original_inode)
        self.assertEqual(before.read_bytes(),self.old)
        self.assertEqual((Path(result['bundle_backup'])/'binary').read_text(),'previous-build')
        self.assertEqual((self.bundle/'binary').read_text(),'candidate-build')
        self.assertNotIn('original-sentinel',json.dumps(result))
    def test_receipt_write_failure_is_part_of_transaction(self):
        def fail(result):installer.atomic_write(self.receipt,b'partial');raise OSError('disk failure')
        with patch.object(installer,'bootstrap'):
            with self.assertRaises(installer.InstallError):self.activate(complete=fail)
        self.assertEqual(self.receipt.read_bytes(),self.before_receipt)
        self.assertEqual(self.plist.read_bytes(),self.old)
    def test_kernel_lock_rejects_concurrent_installer(self):
        with installer.installation_lock(self.data):
            with self.assertRaisesRegex(installer.InstallError,'Another Headroom installation'):
                with installer.installation_lock(self.data):pass
    def test_interrupted_transaction_is_recovered_by_next_lock_owner(self):
        # Interrupt recovery itself once; the next owner must finish from the journal.
        with patch.object(installer,'bootstrap',side_effect=installer.InstallError('failed')),patch.object(installer,'recover',side_effect=OSError('interrupted')):
            with self.assertRaises(installer.InstallError):self.activate()
        self.assertTrue((self.data/'pending-install.json').exists())
        with installer.installation_lock(self.data),patch.object(installer,'bootstrap'):installer.recover(self.data)
        self.assertEqual(self.plist.read_bytes(),self.old);self.assertEqual((self.bundle/'binary').read_text(),'previous-build')
    def test_recovery_preserves_configuration_changed_outside_installer(self):
        with patch.object(installer,'bootstrap',side_effect=installer.InstallError('failed')),patch.object(installer,'recover',side_effect=OSError('interrupted')):
            with self.assertRaises(installer.InstallError):self.activate()
        self.plist.write_bytes(b'outside-change')
        with patch.object(installer,'bootstrap'):
            with self.assertRaisesRegex(installer.InstallError,'conflicts with a changed'):installer.recover(self.data)
        self.assertEqual(self.plist.read_bytes(),b'outside-change')
    def test_symlink_managed_file_is_rejected(self):
        self.plist.unlink();self.plist.symlink_to(self.root/'private')
        with self.assertRaises(installer.InstallError):self.activate()


if __name__=='__main__':unittest.main()
