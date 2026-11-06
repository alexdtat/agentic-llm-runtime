"""Trusted fixture transport and grading; checkers never enter agent workdir."""
import base64
import json
import os
import subprocess
import sys
from pathlib import Path,PurePosixPath

from common import ROOT

CHECKER_PY=ROOT/'runtime/arena-venv/bin/python'
if not CHECKER_PY.exists():CHECKER_PY=Path(sys.executable)


class SessionTools:
    def __init__(self,client,sid):self.client=client;self.sid=sid;self.sequence=0;self.events=[]

    def python(self,code,purpose):
        self.sequence+=1
        status,run=self.client.request('POST',f'/v0/sessions/{self.sid}/runs',
            {'request_id':f'{purpose}-{self.sequence}','argv':['python3','-c',code],'timeout_seconds':3})
        if status!=200:raise RuntimeError(run)
        run=self.client.wait(run['id']);self.events.append(run)
        if run['result']['status']!='complete':raise RuntimeError(run['result'])
        return run['result']['stdout']

    def upload(self,fixture):
        for path in sorted(fixture.rglob('*')):
            if path.is_symlink():raise ValueError('Fixture symlinks are forbidden')
            if not path.is_file():continue
            relative=path.relative_to(fixture).as_posix()
            target='/work/'+relative
            data=path.read_bytes()
            self.python(f"from pathlib import Path;p=Path({target!r});p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'')",'setup')
            for offset in range(0,len(data),4096):
                encoded=base64.b64encode(data[offset:offset+4096]).decode()
                self.python(f"import base64;from pathlib import Path;p=Path({target!r});f=p.open('ab');f.write(base64.b64decode({encoded!r}));f.close()",'setup')

    def collect(self,destination,config):
        code="""import os,json
from pathlib import Path
root=Path('/work');files=[];errors=[]
for directory,dirs,names in os.walk(root,followlinks=False):
 for name in dirs[:]:
  p=Path(directory)/name
  if p.is_symlink():errors.append(str(p.relative_to(root)));dirs.remove(name)
 for name in names:
  p=Path(directory)/name
  if p.is_symlink():errors.append(str(p.relative_to(root)));continue
  if not p.is_file():errors.append(str(p.relative_to(root)));continue
  files.append({'path':str(p.relative_to(root)),'bytes':p.stat().st_size})
print(json.dumps({'files':files,'forbidden_nodes':errors}))
"""
        inventory=json.loads(self.python(code,'collect'))
        if inventory['forbidden_nodes']:raise ValueError('Symlink/special output nodes: '+repr(inventory['forbidden_nodes']))
        files=inventory['files']
        if len(files)>config['collection_max_files'] or sum(f['bytes'] for f in files)>config['collection_max_total_bytes']:
            raise ValueError('Output snapshot exceeds configured bounds')
        destination.mkdir()
        for item in files:
            relative=PurePosixPath(item['path'])
            if relative.is_absolute() or '..' in relative.parts:raise ValueError('Unsafe collected path')
            if item['bytes']>config['collection_max_file_bytes']:raise ValueError('Collected file too large')
            data=bytearray()
            for offset in range(0,item['bytes'],8192):
                code=(f"import base64;f=open({'/work/'+item['path']!r},'rb');f.seek({offset});"
                      "print(base64.b64encode(f.read(8192)).decode())")
                data.extend(base64.b64decode(self.python(code,'collect').strip(),validate=True))
            if len(data)!=item['bytes']:raise ValueError('Output changed during snapshot collection')
            path=destination/Path(relative);path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
        return inventory


def check_task(task_dir,workdir,agent):
    task=json.loads((task_dir/'task.json').read_text())
    env={**os.environ,'PYTHONNOUSERSITE':'1','PYTHONDONTWRITEBYTECODE':'1'}
    env.pop('PYTHONPATH',None)
    r=subprocess.run([str(CHECKER_PY),str(task_dir/'check.py'),str(workdir)],
                     capture_output=True,text=True,timeout=10,cwd=ROOT,env=env)
    try:check=json.loads(r.stdout)
    except ValueError:check={'pass':False,'reason':'Чекер не вернул JSON','stderr':r.stderr}
    if r.returncode:check={'pass':False,'reason':'Ошибка выполнения чекера','stderr':r.stderr}
    changed=[]
    mutable=set(task.get('mutable_fixture_files',[]))
    for initial in (task_dir/'fixture').rglob('*'):
        if not initial.is_file():continue
        rel=initial.relative_to(task_dir/'fixture')
        if rel.as_posix() in mutable:continue
        actual=workdir/rel
        if not actual.is_file() or actual.read_bytes()!=initial.read_bytes():changed.append(rel.as_posix())
    checks={'file_checker':bool(check.get('pass',False)),'immutable_fixtures_preserved':not changed}
    if task['baf']=='B5':
        def used_stats(run):
            argv=run['request']['argv']
            return (len(argv)>=3 and Path(argv[0]).name in {'python','python3'}
                    and argv[1] in {'stats.py','/work/stats.py'} and argv[2] in {'data.csv','/work/data.csv'}
                    and run['result']['status']=='complete')
        checks['required_tool_actually_executed']=any(used_stats(r) for r in agent.get('tool_runs',[]))
    return {'pass':all(checks.values()),'checks':checks,'file_checker':check,'changed_immutable_files':changed}
