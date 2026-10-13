import importlib.util
import json
from pathlib import Path
import plistlib
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('install_support',ROOT/'scripts/install_support.py')
installer=importlib.util.module_from_spec(spec);spec.loader.exec_module(installer)


class InstallationTests(unittest.TestCase):
    def test_health_qualifies_identity_then_same_process_provider_snapshot(self):
        identity={'name':'headroom','api_version':1,'source_fingerprint':'a'*64,'pid':111}
        replies=[{'service':identity,'synchronizing':False},{'service':identity,'providers':[{}]*6}]
        calls=[]
        class Response:
            def __init__(self,data):self.data=data
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,*args):return json.dumps(self.data).encode()
        def open_reply(url,**kwargs):calls.append(url);return Response(replies.pop(0))
        with patch.object(installer.urllib.request,'build_opener') as build:
            build.return_value.open.side_effect=open_reply
            self.assertEqual(len(installer.health('a'*64,111)['providers']),6)
        self.assertEqual(calls,['http://127.0.0.1:8793/health','http://127.0.0.1:8793/api/plans'])
        replies.extend([{'service':identity},{'service':{**identity,'pid':222},'providers':[{}]*6}])
        with patch.object(installer.urllib.request,'build_opener') as build:
            build.return_value.open.side_effect=open_reply
            with self.assertRaisesRegex(installer.InstallError,'identity changed'):installer.health('a'*64,111)

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
        self.no_ls=patch.object(installer,'launch_services_supported',return_value=False);self.no_ls.start();self.addCleanup(self.no_ls.stop)

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

    def fake_ls(self,calls,unregister_returncode=0,register_returncode=0,unregister_output=b''):
        def run(argv,check=True,timeout=30):
            calls.append(list(argv))
            if '-u' in argv:
                output=unregister_output(argv[-1]) if callable(unregister_output) else unregister_output
                return SimpleNamespace(returncode=unregister_returncode,stdout=output,stderr=b'')
            return SimpleNamespace(returncode=register_returncode,stdout=b'',stderr=b'')
        return run

    def test_successful_activation_unregisters_preserved_copies_and_registers_active_last(self):
        failed=self.root/'Applications/.Headroom.failed-0123456789abcdef0123456789abcdef.app';failed.mkdir()
        (failed/'binary').write_text('old-failed-build')
        old_backup=self.root/'Applications/.headroom-backups/old-archive/Headroom.app';old_backup.mkdir(parents=True)
        (old_backup/'binary').write_text('old-backup-build')
        calls=[];bootstrap_at=[]
        with patch.object(installer,'launch_services_supported',return_value=True),patch.object(installer,'command',side_effect=self.fake_ls(calls)),patch.object(installer,'bootstrap',side_effect=lambda label,plist:bootstrap_at.append(len(calls))):
            result=self.activate()
        # Active registration completed before bootstrap could launch the app.
        self.assertEqual(bootstrap_at,[len(calls)])
        self.assertTrue(all(call[0]==str(installer.LSREGISTER) for call in calls))
        self.assertTrue(all(call[1] in ('-u','-f') for call in calls))
        self.assertIn([str(installer.LSREGISTER),'-u',str(failed)],calls)
        self.assertIn([str(installer.LSREGISTER),'-u',str(old_backup)],calls)
        self.assertIn([str(installer.LSREGISTER),'-u',result['bundle_backup']],calls)
        self.assertEqual(calls[-1],[str(installer.LSREGISTER),'-f',str(self.bundle)])
        self.assertEqual((failed/'binary').read_text(),'old-failed-build')
        self.assertEqual((old_backup/'binary').read_text(),'old-backup-build')
        self.assertEqual((Path(result['bundle_backup'])/'binary').read_text(),'previous-build')
        self.assertEqual((self.bundle/'binary').read_text(),'candidate-build')

    def test_absent_inactive_registration_is_idempotent_but_other_ls_failures_are_not(self):
        failed=self.root/'Applications/.Headroom.failed-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.app';failed.mkdir()
        (failed/'binary').write_text('already-absent-build')
        calls=[]
        absent=lambda path:f'failed to scan {path}: -10814\n from spotlight'.encode()
        with patch.object(installer,'launch_services_supported',return_value=True),patch.object(installer,'command',side_effect=self.fake_ls(calls,unregister_returncode=1,unregister_output=absent)):
            installer.sync_launch_services(self.bundle)
        # The inactive copy really was passed to -u, and the active bundle was
        # still registered afterwards.
        self.assertEqual(calls,[[str(installer.LSREGISTER),'-u',str(failed)],[str(installer.LSREGISTER),'-f',str(self.bundle)]])
        self.assertEqual((failed/'binary').read_text(),'already-absent-build')
        for output in (f'failed to scan {failed}: -10810\n from spotlight'.encode(),b'failed to scan: permission denied',b''):
            other_calls=[]
            with patch.object(installer,'launch_services_supported',return_value=True),patch.object(installer,'command',side_effect=self.fake_ls(other_calls,unregister_returncode=1,unregister_output=output)):
                with self.assertRaisesRegex(installer.InstallError,'LaunchServices registration failed'):
                    installer.sync_launch_services(self.bundle)
            self.assertEqual(other_calls,[[str(installer.LSREGISTER),'-u',str(failed)]])
        with patch.object(installer,'launch_services_supported',return_value=True),patch.object(installer,'command',side_effect=self.fake_ls([],unregister_returncode=2,unregister_output=absent)):
            with self.assertRaisesRegex(installer.InstallError,'LaunchServices registration failed'):
                installer.sync_launch_services(self.bundle)
        with patch.object(installer,'launch_services_supported',return_value=True),patch.object(installer,'command',side_effect=self.fake_ls([],register_returncode=1)):
            with self.assertRaisesRegex(installer.InstallError,'LaunchServices registration failed'):
                installer.sync_launch_services(self.bundle)

    def test_rollback_unregisters_failed_candidate_and_registers_restored_bundle_last(self):
        calls=[]
        with patch.object(installer,'launch_services_supported',return_value=True),patch.object(installer,'command',side_effect=self.fake_ls(calls)),patch.object(installer,'bootstrap',side_effect=[installer.InstallError('failed'),None]):
            with self.assertRaisesRegex(installer.InstallError,'previous installation was restored'):self.activate()
        failed=list(self.bundle.parent.glob('.Headroom.failed-*.app'))
        self.assertEqual(len(failed),1)
        self.assertEqual((failed[0]/'binary').read_text(),'candidate-build')
        self.assertEqual((self.bundle/'binary').read_text(),'previous-build')
        self.assertIn([str(installer.LSREGISTER),'-u',str(failed[0])],calls)
        self.assertEqual(calls[-1],[str(installer.LSREGISTER),'-f',str(self.bundle)])
        self.assertLess(calls.index([str(installer.LSREGISTER),'-u',str(failed[0])]),len(calls)-1)

    def test_interrupted_registration_failure_keeps_journal_for_next_recovery(self):
        with patch.object(installer,'sync_launch_services',side_effect=[installer.InstallError('ls failed'),installer.InstallError('ls failed again')]):
            with self.assertRaisesRegex(installer.InstallError,'rollback needs recovery'):self.activate()
        self.assertTrue((self.data/'pending-install.json').exists())
        self.assertEqual((self.bundle/'binary').read_text(),'previous-build')
        with patch.object(installer,'bootstrap'),patch.object(installer,'sync_launch_services'):
            installer.recover(self.data)
        self.assertFalse((self.data/'pending-install.json').exists())
        self.assertEqual(self.plist.read_bytes(),self.old)
        self.assertEqual((self.bundle/'binary').read_text(),'previous-build')
        self.assertEqual(self.receipt.read_bytes(),self.before_receipt)
        self.assertTrue(list(self.bundle.parent.glob('.Headroom.failed-*.app')))

    def test_service_only_transaction_has_no_launch_services_effect(self):
        with patch.object(installer,'bootstrap'),patch.object(installer,'sync_launch_services') as sync:
            installer.activate('test',self.plist,{'Label':'test','ProgramArguments':['candidate']},self.data,lambda:None)
        sync.assert_not_called()

    def test_sync_rejects_symlinked_active_bundle_without_calling_lsregister(self):
        link=self.root/'Applications/Linked.app';link.symlink_to(self.bundle,target_is_directory=True)
        with patch.object(installer,'launch_services_supported',return_value=True),patch.object(installer,'command') as run:
            with self.assertRaisesRegex(installer.InstallError,'symbolic link'):installer.sync_launch_services(link)
        run.assert_not_called()

    def test_sync_rejects_symlinked_inactive_bundle_without_calling_lsregister(self):
        failed_link=self.root/'Applications/.Headroom.failed-cccccccccccccccccccccccccccccccc.app'
        failed_link.symlink_to(self.bundle,target_is_directory=True)
        with patch.object(installer,'launch_services_supported',return_value=True),patch.object(installer,'command') as run:
            with self.assertRaisesRegex(installer.InstallError,'symbolic link'):installer.sync_launch_services(self.bundle)
        run.assert_not_called()
        failed_link.unlink()
        backup_link=self.root/'Applications/.headroom-backups/link-archive/Headroom.app'
        backup_link.parent.mkdir(parents=True);backup_link.symlink_to(self.bundle,target_is_directory=True)
        with patch.object(installer,'launch_services_supported',return_value=True),patch.object(installer,'command') as run:
            with self.assertRaisesRegex(installer.InstallError,'symbolic link'):installer.sync_launch_services(self.bundle)
        run.assert_not_called()

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
