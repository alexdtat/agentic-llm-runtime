"""B1-B5 smoke runner: live local model or recorded actions, one fresh session each."""
import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

from common import Commands,ROOT,new_run,save_json
from track_a.baf_support import SessionTools,check_task
from track_a.client import Client
from track_a.harness import run_agent
from vision_client import local_api_key,parse_object


def replay_agent(client,sid,previous,out,config):
    steps=sorted(previous.glob('model-call-*.json'))
    tools=[];answer=None;stop='step_limit'
    for i,path in enumerate(steps[:config['max_steps']]):
        saved=json.loads(path.read_text())
        action=parse_object(saved['response']['choices'][0]['message']['content'])
        if action.get('tool')=='finish':answer=action.get('answer');stop='finish';break
        if action.get('tool')!='exec' or not isinstance(action.get('argv'),list):raise ValueError('Invalid recorded action')
        status,run=client.request('POST',f'/v0/sessions/{sid}/runs',
            {'request_id':f'agent-step-{i}','argv':action['argv'],'timeout_seconds':3})
        if status!=200:raise RuntimeError(run)
        tools.append(client.wait(run['id']))
    return {'answer':answer,'model_calls':[],'tool_runs':tools,'messages':[],
            'stop_reason':stop,'recorded_responses':len(steps),'mode':'record_replay'}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--checkers-only',action='store_true')
    parser.add_argument('--replay-from',type=Path)
    args=parser.parse_args()
    if args.checkers_only:
        return subprocess.run([sys.executable,'-m','unittest','test_baf_checkers','-v'],cwd=ROOT).returncode
    out=new_run('baf-suite');commands=Commands(out)
    source=(args.replay_from.resolve()/'suite') if args.replay_from else ROOT/'track_a/baf'
    shutil.copytree(source,out/'suite')
    for name in ['baf_runner.py','common.py','track_a/harness.py','track_a/baf_support.py','track_a/client.py']:
        target=out/'source'/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(ROOT/name,target)
    protocol=json.loads((out/'suite/protocol-v1.json').read_text())
    catalog=json.loads((out/'suite/catalog.json').read_text())
    save_json(out/'protocol.json',protocol)
    project='course-baf-'+secrets.token_hex(4)
    os.environ['PROBE_TOKEN']=secrets.token_urlsafe(24)
    compose=['compose','-p',project,'-f',str(ROOT/'track_a/compose.yaml')]
    summary={'protocol':protocol,'mode':'record_replay' if args.replay_from else 'live',
             'episodes':[],'source_run':str(args.replay_from) if args.replay_from else None,
             'limitations':['Five simple smoke tasks only, not a full BAF benchmark',
                            'No comparison between harness/model pairs',
                            'Local model eligibility for course quality evaluation not established']}
    result_file=ROOT/'results/baf/hyperqwen-v0'/(out.name+'.jsonl')
    result_file.parent.mkdir(parents=True,exist_ok=True)
    summary['jsonl']=str(result_file)
    client=None;sid=None
    try:
        if not args.replay_from:
            status=commands.run(['qwen-profile','status'])
            (out/'model-status.txt').write_text(status['stdout'])
            if 'сервер:   работает' not in status['stdout']:raise RuntimeError('HyperQwen is not ready; no start attempted')
        commands.docker(*compose,'up','-d','--build',timeout=60)
        address=commands.docker(*compose,'port','gateway','8080')['stdout'].strip()
        client=Client('http://'+address,os.environ['PROBE_TOKEN'])
        for _ in range(100):
            try:
                if client.request('GET','/v0/health')[0]==200:break
            except OSError:pass
            time.sleep(.1)
        else:raise RuntimeError('Gateway did not become ready')
        for entry in catalog['tasks']:
            task_dir=out/'suite'/entry['path']
            task=json.loads((task_dir/'task.json').read_text())
            episode=out/task['id'];episode.mkdir()
            item={'task_id':task['id'],'baf':task['baf'],'level':task['level'],'repeat':1,
                  'pass':False,'tokens_in':0,'tokens_out':0,'outcome':'error'}
            agent={'model_calls':[],'tool_runs':[],'stop_reason':'error'}
            tools=None;started=None
            try:
                status,session=client.request('POST','/v0/sessions',{'request_id':'baf-'+task['id'].replace('.','-')})
                if status!=200:raise RuntimeError(session)
                sid=session['id'];tools=SessionTools(client,sid)
                tools.upload(task_dir/'fixture')
                config={**protocol,'task':'\n'.join(task['messages']),
                        'per_task_timeout_seconds':task['timeout_s']}
                save_json(episode/'config.json',config)
                started=time.monotonic()
                if args.replay_from:
                    agent=replay_agent(client,sid,args.replay_from.resolve()/task['id'],episode,config)
                else:
                    agent=run_agent(client,sid,local_api_key(),episode,config)
                item['seconds']=time.monotonic()-started
                save_json(episode/'agent.json',agent)
                inventory=tools.collect(episode/'workdir',protocol)
                save_json(episode/'inventory.json',inventory)
                verdict=check_task(task_dir,episode/'workdir',agent)
                save_json(episode/'check.json',verdict)
                finished=agent['stop_reason']=='finish'
                item['pass']=verdict['pass'] and (finished or not protocol['require_agent_finish'])
                item['reason']='ok' if item['pass'] else json.dumps({'check':verdict,'agent_finish':finished},ensure_ascii=False)
                item['outcome']='complete' if finished else 'step_limit'
            except Exception:
                item['error']=traceback.format_exc();item['reason']=item['error'].splitlines()[-1]
                progress=episode/'agent-progress.json'
                if progress.exists():agent=json.loads(progress.read_text())
                if started is not None:item['seconds']=time.monotonic()-started
                # Retain any partial state for diagnosing a failed model/harness run.
                if tools and not (episode/'workdir').exists():
                    try:tools.collect(episode/'workdir',protocol)
                    except Exception:pass
            finally:
                if tools:save_json(episode/'operator-setup-and-collection.json',tools.events)
                if sid:
                    client.request('DELETE','/v0/sessions/'+sid);sid=None
            calls=[json.loads(p.read_text()) for p in sorted(episode.glob('model-call-*.json'))]
            item['model_calls']=len(calls)
            item['tool_calls']=len(agent.get('tool_runs',[]))
            item['tokens_in']=sum(c['response'].get('usage',{}).get('prompt_tokens',0) for c in calls)
            item['tokens_out']=sum(c['response'].get('usage',{}).get('completion_tokens',0) for c in calls)
            item['usage_complete']=all('usage' in c['response'] for c in calls)
            item['artifact_dir']=str(episode)
            save_json(episode/'result.json',item)
            with result_file.open('a') as stream:stream.write(json.dumps(item,ensure_ascii=False)+'\n')
            summary['episodes'].append(item)
            print(json.dumps({k:item.get(k) for k in ['task_id','pass','model_calls','tool_calls','seconds','reason']},ensure_ascii=False),flush=True)
    except Exception:summary['error']=traceback.format_exc()
    finally:
        if client and sid:
            try:client.request('DELETE','/v0/sessions/'+sid)
            except Exception:pass
        logs=commands.docker(*compose,'logs','--no-color',check=False)
        (out/'server.log').write_text(logs['stdout']+logs['stderr'])
        commands.docker(*compose,'down','--timeout','5','--remove-orphans',timeout=20,check=False)
        summary['tasks_passed']=sum(x['pass'] for x in summary['episodes'])
        summary['tasks_total']=len(catalog['tasks'])
        summary['passed']=len(summary['episodes'])==len(catalog['tasks']) and all(x['pass'] for x in summary['episodes']) and 'error' not in summary
        summary['model_requests_performed']=sum(x['model_calls'] for x in summary['episodes'])
        save_json(out/'summary.json',summary)
    print(json.dumps({'result':str(out),'passed':summary['passed'],'tasks_passed':summary['tasks_passed'],
                      'tasks_total':summary['tasks_total'],'error':summary.get('error')},ensure_ascii=False),flush=True)
    return 0 if summary['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
