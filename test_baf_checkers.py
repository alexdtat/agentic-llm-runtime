"""Positive, negative and tampering checks before any real model evaluation."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from common import ROOT
from track_a.baf_support import check_task

BASE=ROOT/'track_a/baf'


class BafCheckers(unittest.TestCase):
    def test_five_positive_and_negative_outputs(self):
        entries=json.loads((BASE/'catalog.json').read_text())['tasks']
        for entry in entries:
            task_dir=BASE/entry['path'];tid=entry['id']
            with self.subTest(task=tid),tempfile.TemporaryDirectory() as temporary:
                work=Path(temporary)/'work';shutil.copytree(task_dir/'fixture',work)
                agent={'tool_runs':[]}
                if tid=='B1.1':(work/'answer.txt').write_text('8137\n')
                elif tid=='B2.1':(work/'answer.txt').write_text('util/date_tools.py\n')
                elif tid=='B3.1':
                    (work/'config.json').write_text(json.dumps({'service':'reports','host':'127.0.0.1','port':8137,'debug':False,'limits':{'timeout_seconds':30,'retries':2}}))
                elif tid=='B4.1':
                    p=work/'README.md';p.write_bytes(p.read_bytes().replace(b'pip install example-package==1.2',b'pip install example-package==1.3'))
                elif tid=='B5.1':
                    (work/'answer.txt').write_text('7.0\n')
                    agent={'tool_runs':[{'request':{'argv':['python3','stats.py','data.csv']},'result':{'status':'complete'}}]}
                self.assertTrue(check_task(task_dir,work,agent)['pass'])
                if tid in {'B1.1','B2.1','B5.1'}:(work/'answer.txt').write_text('wrong')
                elif tid=='B3.1':
                    p=work/'config.json';value=json.loads(p.read_text());value['debug']='false';p.write_text(json.dumps(value))
                elif tid=='B4.1':
                    p=work/'README.md';p.write_bytes((task_dir/'fixture/README.md').read_bytes().replace(b'1.2',b'1.3'))
                self.assertFalse(check_task(task_dir,work,agent)['pass'])

    def test_fixture_tampering_rejected(self):
        task=BASE/'B1-read/B1.1-service-port'
        with tempfile.TemporaryDirectory() as folder:
            work=Path(folder)/'work';shutil.copytree(task/'fixture',work)
            (work/'answer.txt').write_text('8137')
            (work/'config/app.yaml').write_text('port: 9999')
            self.assertFalse(check_task(task,work,{})['pass'])

    def test_b5_answer_without_tool_rejected(self):
        task=BASE/'B5-tool/B5.1-run-stats'
        with tempfile.TemporaryDirectory() as folder:
            work=Path(folder)/'work';shutil.copytree(task/'fixture',work)
            (work/'answer.txt').write_text('7')
            self.assertFalse(check_task(task,work,{'tool_runs':[]})['pass'])


if __name__=='__main__':unittest.main(verbosity=2)
