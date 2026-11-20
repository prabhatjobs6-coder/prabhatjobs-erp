import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import threading
import unittest
import urllib.request
import urllib.error
from urllib.parse import urlsplit


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'files'
        self.state = Path(self.temp.name) / 'state'
        self.root.mkdir()
        self.old_env = {k: os.environ.get(k) for k in ['PWF_FILE_ROOT', 'PWF_STATE_ROOT']}
        os.environ['PWF_FILE_ROOT'] = str(self.root)
        os.environ['PWF_STATE_ROOT'] = str(self.state)
        spec = importlib.util.spec_from_file_location('under_test', Path(__file__).with_name('workbench.py'))
        self.w = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.w)

    def tearDown(self):
        for k, v in self.old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.temp.cleanup()

    def test_xlsx_preserves_phone_and_blocks_formula_injection(self):
        from openpyxl import load_workbook
        result = self.w.spreadsheet_create('a'*32, ['Name','Phone'], [[None,'09040841969'], ['=HYPERLINK("bad")','+918866677615']])
        ws = load_workbook(result['artifacts'][0]['path']).active
        self.assertEqual(ws.max_row, 3)
        self.assertEqual(ws['B2'].value, '09040841969')
        self.assertEqual(ws['A3'].data_type, 's')
        self.assertEqual(ws['B3'].data_type, 's')

    def test_pdf_split_merge_and_html_are_plain_text(self):
        from pypdf import PdfReader
        a = self.w.document_create('a'*32, 'Test <Candidate>', [{'heading':'Experience','lines':['A & B <not markup>']}])
        source = a['artifacts'][0]['path']
        self.assertIn('A & B <not markup>', PdfReader(source).pages[0].extract_text())
        merged = self.w.pdf_merge('b'*32, [source,source])
        self.assertEqual(merged['pages'],2)
        split = self.w.pdf_split('c'*32, merged['artifacts'][0]['path'], [{'name':'Passport','pages':[2]}])
        self.assertEqual(len(PdfReader(split['artifacts'][0]['path']).pages),1)
        with self.assertRaises(ValueError):
            self.w.pdf_split('d'*32, source, [{'pages':[0]}])

    def test_workspace_escape_and_symlink_are_rejected(self):
        outside = Path(self.temp.name) / 'secret.txt'
        outside.write_text('outside')
        (self.root/'link.txt').symlink_to(outside)
        for name in [str(outside),'../secret.txt','link.txt']:
            with self.assertRaises(ValueError):
                self.w.checked_file(name)

    def test_durable_queue_idempotency_and_worker_execution(self):
        args = {'columns':['S.No','Name'], 'rows':[[1,'Test Candidate']]}
        a = self.w.submit('spreadsheet_create',args,'test-stable-key')
        b = self.w.submit('spreadsheet_create',args,'test-stable-key')
        self.assertEqual(a['job_id'],b['job_id'])
        self.assertEqual(a['state'],'queued')
        with self.assertRaises(ValueError):
            self.w.submit('spreadsheet_create',{'columns':['Other'],'rows':[]},'test-stable-key')
        self.assertTrue(self.w.run_one())
        done = self.w.status(a['job_id'])
        self.assertEqual(done['state'],'done')
        self.assertEqual(done['attempts'],1)
        output = done['result']['artifacts'][0]
        self.assertEqual(self.w.artifact(output['path'])['sha256'],output['sha256'])

    def test_cancel_and_recover_crashed_worker(self):
        a = self.w.submit('spreadsheet_create',{'columns':['A'],'rows':[]})
        self.w.cancel(a['job_id'])
        self.assertIsNone(self.w.claim())
        b = self.w.submit('spreadsheet_create',{'columns':['B'],'rows':[]})
        self.assertEqual(self.w.claim(),b['job_id'])
        with self.w.db() as con:
            con.execute('UPDATE jobs SET lease_until=0 WHERE id=?',(b['job_id'],))
        self.assertEqual(self.w.claim(),b['job_id'])
        self.assertEqual(self.w.status(b['job_id'])['attempts'],2)

    def test_health_reports_queue_and_never_exposes_candidate_data(self):
        candidate_label = 'Confidential Test Candidate'
        self.w.submit('spreadsheet_create',
                      {'columns': ['Candidate'], 'rows': [[candidate_label]]})
        self.w.heartbeat()
        health = self.w.workbench_health()
        self.assertTrue(health['read_only'])
        self.assertTrue(health['worker_online'])
        self.assertEqual(health['status'], 'ok')
        self.assertEqual(health['ready_jobs'], 1)
        self.assertEqual(health['job_states']['queued'], 1)
        self.assertNotIn(candidate_label, json.dumps(health))

    def test_health_detects_stalled_lease_and_retry_backlog(self):
        job = self.w.submit('spreadsheet_create',
                            {'columns': ['Candidate'], 'rows': [['Synthetic']]})
        self.assertEqual(self.w.claim(), job['job_id'])
        with self.w.db() as con:
            con.execute("UPDATE jobs SET lease_until=? WHERE id=?",
                        (time.time() - 30, job['job_id']))
        health = self.w.workbench_health()
        self.assertEqual(health['stale_running_leases'], 1)
        self.assertIn('expired_running_job_leases', health['alerts'])
        self.assertEqual(health['status'], 'needs_attention')
        self.assertEqual(self.w.claim(), job['job_id'])
        health = self.w.workbench_health()
        self.assertEqual(health['retry_pending'], 0)
        self.w.finish(job['job_id'], error='Synthetic failure', retry=True)
        health = self.w.workbench_health()
        self.assertEqual(health['retry_pending'], 1)
        self.assertEqual(health['stale_running_leases'], 0)

    def test_protocol_and_unsupported_command(self):
        inp = '\n'.join(json.dumps(m) for m in [
            {'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-03-26'}},
            {'jsonrpc':'2.0','method':'notifications/initialized'},
            {'jsonrpc':'2.0','id':2,'method':'tools/list'},
            {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'workbench_submit','arguments':{'operation':'shell','arguments':{'command':'whoami'}}}},
        ])+'\n'
        r = subprocess.run([sys.executable,str(Path(__file__).with_name('workbench.py'))],input=inp,capture_output=True,text=True,check=True)
        messages = [json.loads(l) for l in r.stdout.splitlines()]
        self.assertEqual(len(messages),3)
        self.assertEqual(messages[0]['result']['protocolVersion'],'2025-03-26')
        self.assertEqual(len(messages[1]['result']['tools']),6)
        self.assertTrue(messages[2]['result']['isError'])

    def test_indic_pdf_refuses_unshaped_text_and_docx_preserves_unicode(self):
        from docx import Document
        sections = [{'heading':'Details','lines':['ગુજરાતી નામ']}]
        with self.assertRaises(ValueError):
            self.w.document_create('a'*32,'Candidate',sections)
        result = self.w.document_create('b'*32,'Candidate',sections,format='docx')
        self.assertIn('ગુજરાતી નામ',[p.text for p in Document(result['artifacts'][0]['path']).paragraphs])

    def test_signed_download_requires_valid_unexpired_artifact_signature(self):
        job = self.w.submit('spreadsheet_create',{'columns':['Name'],'rows':[['Synthetic']]})
        self.w.run_one()
        result = self.w.status(job['job_id'])
        item = result['result']['artifacts'][0]
        server = self.w.ThreadingHTTPServer(('127.0.0.1',0),self.w.DownloadHandler)
        thread = threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        def local(url):
            parts=urlsplit(url)
            return f'http://127.0.0.1:{server.server_port}'+parts.path+'?'+parts.query
        def rejected(url):
            with self.assertRaises(urllib.error.HTTPError) as exc:
                urllib.request.urlopen(url)
            self.assertEqual(exc.exception.code,404)
        try:
            url = local(item['download_url'])
            with urllib.request.urlopen(url) as response:
                self.assertEqual(response.status,200)
                self.assertTrue(response.read().startswith(b'PK'))
                self.assertIn('attachment;',response.headers['Content-Disposition'])
                self.assertEqual(response.headers['Cache-Control'],'private, no-store')
            with urllib.request.urlopen(urllib.request.Request(url,method='HEAD')) as response:
                self.assertEqual(response.read(),b'')
            rejected(url.rsplit('=',1)[0]+'='+'0'*64)
            expired=self.w.download_url(job['job_id'],0,item,expires=int(time.time())-1)
            rejected(local(expired))
            rejected(f'http://127.0.0.1:{server.server_port}/workbench/files/{job["job_id"]}/0')
            Path(item['path']).write_bytes(b'changed after completion')
            rejected(url)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main(verbosity=2)
