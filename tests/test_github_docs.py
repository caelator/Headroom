import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

PROJECT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('github_docs',PROJECT/'scripts/github_docs.py')
docs=importlib.util.module_from_spec(spec);spec.loader.exec_module(docs)


class GitHubDocsTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup);self.root=Path(temporary.name)
        self.run_git('init','-q');self.run_git('config','user.name','Test');self.run_git('config','user.email','test@example.invalid')
        self.run_git('config','core.hooksPath','.git/no-hooks')
        self.write('pyproject.toml','[project]\nname="headroom"\nversion="0.1.0"\nrequires-python=">=3.11"\ndependencies=[]\n')
        self.write('LICENSE','MIT License\n')
        self.write('src/aiusage/registry.py',"PROVIDERS={'codex':{'name':'Codex','connection_url':'https://chatgpt.com/usage'}}\nMAX_DATA_AGE_SECONDS=90\nDEFAULT_SETTINGS={'refresh_interval_seconds':30}\n")
        self.write('src/aiusage/codex.py','def collect(store):\n    return None\n')
        self.write('src/aiusage/collectors.py',"def fetch(provider):\n    return {'codex':collect_codex}[provider]\n")
        self.write('native/MenuBar.swift','import Cocoa\n')
        self.write('scripts/install_menubar.py',"info={'LSMinimumSystemVersion':'13.0'}\n")
        self.write('web/index.html','<html>synthetic preview</html>\n')
        self.write('docs/assets/dashboard.jpg',b'synthetic-image')
        self.write('README.md','# Headroom\n\n'+''.join(f'<!-- headroom:{key}:start -->\n<!-- headroom:{key}:end -->\n\n' for key in docs.BLOCKS))
        self.receipt();self.sync();self.commit()

    def write(self,path,value):
        target=self.root/path;target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes(value if isinstance(value,bytes) else value.encode())

    def run_git(self,*args):
        return subprocess.check_output(['git',*args],cwd=self.root,stderr=subprocess.DEVNULL).decode().strip()

    def sync(self):self.write('README.md',docs.render(docs.Source(self.root)))

    def receipt(self):
        source=docs.Source(self.root)
        self.write(docs.PREVIEW,json.dumps({'image':'dashboard.jpg','image_sha256':docs.hashlib.sha256(source.read('docs/assets/dashboard.jpg')).hexdigest(),
            'frontend_sha256':source.digest([p for p in source.modes if p.startswith('web/')])})+'\n')

    def commit(self):
        self.run_git('add','.');self.run_git('commit','-qm','test source')
        return self.run_git('rev-parse','HEAD')

    def push_line(self,oid):return 'refs/heads/main '+oid+' refs/heads/main '+'0'*40+'\n'

    def test_working_and_committed_source_match(self):
        docs.check(docs.Source(self.root));docs.check(docs.Source(self.root,'HEAD'))

    def test_pre_commit_uses_staged_source_and_preserves_unstaged_readme(self):
        project=self.root/'pyproject.toml';original=project.read_text()
        project.write_text(original.replace('0.1.0','0.2.0'));self.run_git('add','pyproject.toml')
        project.write_text(original.replace('0.1.0','0.9.0'))
        readme=self.root/'README.md';readme.write_text(readme.read_text()+'Unstaged manual note.\n');working=readme.read_bytes()
        docs.prepare(self.root)
        staged=self.run_git('show',':README.md')
        self.assertIn('version `0.2.0`',staged);self.assertNotIn('version `0.9.0`',staged)
        self.assertEqual(readme.read_bytes(),working)
        docs.check(docs.Source(self.root,'INDEX'))

    def test_pre_commit_updates_worktree_only_when_fully_staged(self):
        project=self.root/'pyproject.toml';project.write_text(project.read_text().replace('0.1.0','0.2.0'))
        self.run_git('add','pyproject.toml');docs.prepare(self.root)
        self.assertIn('version `0.2.0`',(self.root/'README.md').read_text())
    def test_index_snapshot_reads_frozen_blobs(self):
        source=docs.Source(self.root,'INDEX')
        project=self.root/'pyproject.toml';project.write_text(project.read_text().replace('0.1.0','0.8.0'))
        self.run_git('add','pyproject.toml')
        self.assertIn('version `0.1.0`',docs.render(source))

    def test_push_checks_actual_sha_even_when_worktree_is_correct(self):
        project=self.root/'pyproject.toml';project.write_text(project.read_text().replace('0.1.0','0.2.0'))
        stale=self.commit();self.sync()
        docs.check(docs.Source(self.root))
        with self.assertRaisesRegex(docs.DocsError,'README facts are stale'):
            docs.pre_push(self.root,self.push_line(stale))

    def test_dirty_worktree_does_not_change_valid_pushed_snapshot(self):
        head=self.run_git('rev-parse','HEAD')
        project=self.root/'pyproject.toml';project.write_text(project.read_text().replace('0.1.0','0.3.0'));self.sync()
        docs.pre_push(self.root,self.push_line(head))

    def test_non_head_branch_and_annotated_tag_are_checked(self):
        valid=self.run_git('rev-parse','HEAD')
        self.run_git('tag','-a','v0.1','-m','version')
        tag=self.run_git('rev-parse','v0.1')
        docs.pre_push(self.root,'refs/tags/v0.1 '+tag+' refs/tags/v0.1 '+'0'*40+'\n')
        project=self.root/'pyproject.toml';project.write_text(project.read_text().replace('0.1.0','0.2.0'));stale=self.commit()
        self.run_git('checkout','--detach',valid)
        with self.assertRaises(docs.DocsError):docs.pre_push(self.root,self.push_line(stale))

    def test_ref_deletion_does_not_require_a_snapshot(self):
        docs.pre_push(self.root,'(delete) '+'0'*40+' refs/heads/old '+self.run_git('rev-parse','HEAD')+'\n')

    def test_frontend_changes_require_new_preview_receipt(self):
        self.write('web/index.html','<html>changed interface</html>');self.sync()
        with self.assertRaisesRegex(docs.DocsError,'preview is stale'):docs.check(docs.Source(self.root))
        self.write('docs/assets/dashboard.jpg',b'updated-synthetic-image');self.receipt()
        docs.check(docs.Source(self.root))

    def test_missing_links_and_hosted_workflows_block_publication(self):
        readme=self.root/'README.md';readme.write_text(readme.read_text()+'[Missing](missing.md)\n')
        with self.assertRaisesRegex(docs.DocsError,'missing public file'):docs.check(docs.Source(self.root))
        readme.write_text(readme.read_text().replace('[Missing](missing.md)\n',''))
        self.write('.github/workflows/ci.yml','on: [push, pull_request]\njobs: {}\n')
        with self.assertRaisesRegex(docs.DocsError,'Hosted workflow'):docs.check(docs.Source(self.root))

    def test_generated_markers_and_unwired_provider_fail(self):
        self.write('src/aiusage/collectors.py','def fetch(provider):\n    return None\n')
        with self.assertRaisesRegex(docs.DocsError,'not connected'):docs.render(docs.Source(self.root))

    def test_invalid_ref_input_never_becomes_a_git_option(self):
        for line in ['broken','refs/heads/main --help refs/heads/main '+'0'*40]:
            with self.assertRaises(docs.DocsError):docs.pre_push(self.root,line)

    def test_real_pre_commit_hook_refreshes_staged_version(self):
        for path in ['scripts/github_docs.py','.githooks/pre-commit','.githooks/pre-push']:
            target=self.root/path;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(PROJECT/path,target)
            if path.startswith('.githooks/'):target.chmod(0o755)
        self.sync();self.commit();self.run_git('config','core.hooksPath','.githooks')
        project=self.root/'pyproject.toml';project.write_text(project.read_text().replace('0.1.0','0.4.0'))
        self.run_git('add','pyproject.toml');self.run_git('commit','-qm','new version')
        self.assertIn('version `0.4.0`',self.run_git('show','HEAD:README.md'))
        docs.pre_push(self.root,self.push_line(self.run_git('rev-parse','HEAD')))


if __name__=='__main__':unittest.main()
