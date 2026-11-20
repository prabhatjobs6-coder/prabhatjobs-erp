import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest


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
        self.assertEqual(len(messages[1]['result']['tools']),5)
        self.assertTrue(messages[2]['result']['isError'])

    def test_indic_pdf_refuses_unshaped_text_and_docx_preserves_unicode(self):
        from docx import Document
        sections = [{'heading':'Details','lines':['ગુજરાતી નામ']}]
        with self.assertRaises(ValueError):
            self.w.document_create('a'*32,'Candidate',sections)
        result = self.w.document_create('b'*32,'Candidate',sections,format='docx')
        self.assertIn('ગુજરાતી નામ',[p.text for p in Document(result['artifacts'][0]['path']).paragraphs])


if __name__ == '__main__':
    unittest.main(verbosity=2)
