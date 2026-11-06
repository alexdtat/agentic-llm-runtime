"""Bounded real-model diagnostic; does not start/stop the user's HyperQwen."""
import json
import os
import secrets
import time
import traceback

from common import Commands,ROOT,new_run,save_json
from track_a.client import Client
from track_a.harness import run_agent
from vision_client import local_api_key


def main():
    os.environ.setdefault('DOCKER_BUILDKIT','0')
    os.environ.setdefault('COMPOSE_BAKE','false')
    out=new_run('a-agent');commands=Commands(out)
    config={'kind':'technical_agent_smoke','base_url':'http://127.0.0.1:18020/v1',
            'model':'qwen3.8-27b','max_steps':6,'max_output_tokens':256,'temperature':0,
            'task':"Read /work/config.json and find the service port. Edit /work/report.txt: change only the line 'status: draft' to 'status: ready', preserving all other lines exactly. Finish with PORT=<the actual port>."}
    save_json(out/'config.json',config)
    project='course-a-agent-'+secrets.token_hex(4)
    os.environ['PROBE_TOKEN']=secrets.token_urlsafe(24)
    compose=['compose','-p',project,'-f',str(ROOT/'track_a/compose.yaml')]
    value={'config':config,'checks':{}};client=None;sid=None
    try:
        status=commands.run(['qwen-profile','status'])
        (out/'model-status.txt').write_text(status['stdout'])
        if 'сервер:   работает' not in status['stdout']:raise RuntimeError('Existing HyperQwen is not ready; no start attempted')
        commands.docker(*compose,'up','-d','--build',timeout=180)
        address=commands.docker(*compose,'port','gateway','8080')['stdout'].strip()
        client=Client('http://'+address,os.environ['PROBE_TOKEN'])
        for _ in range(100):
            try:
                if client.request('GET','/v0/health')[0]==200:break
            except OSError:pass
            time.sleep(.1)
        status,session=client.request('POST','/v0/sessions',{'request_id':'agent-session'})
        if status!=200:raise RuntimeError(session)
        sid=session['id']
        def execute(key,code):
            status,r=client.request('POST',f'/v0/sessions/{sid}/runs',
                {'request_id':key,'argv':['python3','-c',code],'timeout_seconds':3})
            if status!=200:raise RuntimeError(r)
            return client.wait(r['id'])['result']
        execute('setup',"from pathlib import Path;Path('/work/config.json').write_text('{\"port\":8127}');Path('/work/report.txt').write_text('header\\nstatus: draft\\nkeep: yes\\n')")
        agent=run_agent(client,sid,local_api_key(),out,config)
        save_json(out/'agent.json',agent)
        verified=execute('verify',"import json;print(json.dumps({'report':open('/work/report.txt').read(),'port':json.load(open('/work/config.json'))['port']}))")
        actual=json.loads(verified['stdout'])
        value['checks']['correct_final_answer']=agent['answer']=='PORT=8127'
        value['checks']['only_requested_edit']=actual['report']=='header\nstatus: ready\nkeep: yes\n'
        value['checks']['tool_used_real_protocol']=bool(agent['tool_runs'])
        value['checks']['one_terminal_event_per_tool']=all(len([e for e in r['events'] if e['type']=='result'])==1 for r in agent['tool_runs'])
        value['model_calls']=len(agent['model_calls']);value['tool_calls']=len(agent['tool_runs'])
        value['answer']=agent['answer'];value['actual']=actual
        value['limitations']=['One open technical task, not full BAF benchmark','No desktop UI or SSHFS integration in API runtime','Model eligibility for course quality benchmark not established']
    except Exception:value['error']=traceback.format_exc()
    finally:
        if client and sid:
            try:client.request('DELETE','/v0/sessions/'+sid)
            except Exception:pass
        logs=commands.docker(*compose,'logs','--no-color',check=False)
        (out/'server.log').write_text(logs['stdout']+logs['stderr'])
        commands.docker(*compose,'down','--timeout','5','--remove-orphans',timeout=20,check=False)
        value['passed']=bool(value['checks']) and all(value['checks'].values()) and 'error' not in value
        save_json(out/'summary.json',value)
    print(json.dumps({'result':str(out),**value},ensure_ascii=False,indent=2))
    return 0 if value['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
