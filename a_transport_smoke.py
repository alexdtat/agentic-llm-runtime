"""Actual compose gateway + headless client + hardened-ish CPU sandbox checks."""
import json
import os
import secrets
import time
import traceback

from common import Commands, ROOT, new_run, save_json
from track_a.client import Client


def main():
    os.environ.setdefault('DOCKER_BUILDKIT','0')
    os.environ.setdefault('COMPOSE_BAKE','false')
    out=new_run('a-transport');commands=Commands(out)
    project='course-a-'+secrets.token_hex(4)
    os.environ['PROBE_TOKEN']=secrets.token_urlsafe(24)
    compose=['compose','-p',project,'-f',str(ROOT/'track_a/compose.yaml')]
    value={'kind':'v0_transport_smoke','model_calls':0,'checks':{},'runs':[]}
    sid=None;client=None
    try:
        commands.docker(*compose,'up','-d','--build',timeout=180)
        address=commands.docker(*compose,'port','gateway','8080')['stdout'].strip()
        client=Client('http://'+address,os.environ['PROBE_TOKEN'])
        value['base_url']=client.base
        for _ in range(100):
            try:
                status,health=client.request('GET','/v0/health')
                if status==200:break
            except OSError:pass
            time.sleep(.1)
        else:raise RuntimeError('Gateway never became ready')
        wrong=Client(client.base,'incorrect')
        value['checks']['auth_rejects_wrong_token']=wrong.request('POST','/v0/sessions',{'request_id':'x'})[0]==401
        value['checks']['session_schema_rejects_unknown_fields']=client.request('POST','/v0/sessions',{'request_id':'x','unknown':True})[0]==400
        status,session=client.request('POST','/v0/sessions',{'request_id':'session-smoke'})
        if status!=200:raise RuntimeError(session)
        sid=session['id'];value['session']=session
        again=client.request('POST','/v0/sessions',{'request_id':'session-smoke'})[1]
        value['checks']['idempotent_session']=again['id']==sid

        def run(key,argv,timeout=3):
            payload={'request_id':key,'argv':argv,'timeout_seconds':timeout}
            status,r=client.request('POST',f'/v0/sessions/{sid}/runs',payload)
            if status!=200:raise RuntimeError(r)
            result=client.wait(r['id']);value['runs'].append(result)
            return result,payload

        r,payload=run('write',['python3','-c',"from pathlib import Path;Path('/work/note.txt').write_text('alpha');print('written')"])
        value['checks']['command_in_sandbox']=r['result']['status']=='complete' and r['result']['stdout'].strip()=='written'
        same=client.request('POST',f'/v0/sessions/{sid}/runs',payload)[1]
        value['checks']['idempotent_run']=same['id']==r['id']
        different={**payload,'argv':['echo','different']}
        value['checks']['conflicting_request_id_rejected']=client.request('POST',f'/v0/sessions/{sid}/runs',different)[0]==409
        r,_=run('read',['python3','-c',"print(open('/work/note.txt').read())"])
        value['checks']['session_workdir_persists']=r['result']['stdout'].strip()=='alpha'
        r,_=run('command-error',['python3','-c',"import sys;print('expected-error',file=sys.stderr);sys.exit(3)"])
        value['checks']['command_error_distinguished']=r['result']['error_kind']=='command' and r['result']['exit_code']==3
        r,_=run('timeout',['python3','-c','import time;time.sleep(5)'],.2)
        value['checks']['timeout_distinguished']=r['result']['status']=='timed_out' and r['result']['error_kind']=='timeout'
        payload={'request_id':'cancel','argv':['python3','-c',"import subprocess,time;from pathlib import Path;p=subprocess.Popen(['sleep','30']);Path('/work/child.pid').write_text(str(p.pid));time.sleep(30)"],'timeout_seconds':10}
        status,r=client.request('POST',f'/v0/sessions/{sid}/runs',payload)
        if status!=200:raise RuntimeError(r)
        time.sleep(.3)
        child,_=run('child-pid',['python3','-c',"from pathlib import Path;p=Path('/work/child.pid');print(p.read_text() if p.exists() else '')"])
        child_pid=child['result']['stdout'].strip()
        if not child_pid:raise RuntimeError('Child PID not available')
        client.request('POST','/v0/runs/'+r['id']+'/cancel',{})
        cancelled=client.wait(r['id']);value['runs'].append(cancelled)
        value['checks']['cancelled_status']=cancelled['result']['status']=='cancelled'
        r2,_=run('verify-child',['python3','-c',f"from pathlib import Path;p=Path('/proc/{int(child_pid)}/stat');print('gone' if not p.exists() or p.read_text().split()[2]=='Z' else 'running')"])
        value['checks']['cancel_kills_descendant']=r2['result']['stdout'].strip()=='gone'
        repeated=client.request('POST','/v0/runs/'+cancelled['id']+'/cancel',{})[1]
        value['checks']['cancel_does_not_duplicate_terminal_event']=len([e for e in repeated['events'] if e['type']=='result'])==1
        value['checks']['one_terminal_event_per_run']=all(len([e for e in r['events'] if e['type']=='result'])==1 for r in value['runs'])
        status,released=client.request('DELETE','/v0/sessions/'+sid)
        value['checks']['release_session']=status==200 and released['status']=='closed'
        value['checks']['release_removes_sandbox']=commands.docker('inspect',session['container'],check=False)['returncode']!=0
    except Exception:value['error']=traceback.format_exc()
    finally:
        if sid and client:
            try:client.request('DELETE','/v0/sessions/'+sid)
            except Exception:pass
        logs=commands.docker(*compose,'logs','--no-color',check=False)
        (out/'server.log').write_text(logs['stdout']+logs['stderr'])
        commands.docker(*compose,'down','--timeout','5','--remove-orphans',check=False,timeout=20)
        value['passed']=bool(value['checks']) and all(value['checks'].values()) and 'error' not in value
        save_json(out/'summary.json',value)
    print(json.dumps({'result':str(out),**value},ensure_ascii=False,indent=2))
    return 0 if value['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
