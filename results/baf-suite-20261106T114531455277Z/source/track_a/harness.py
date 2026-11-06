"""Minimal source-visible agent loop; all tools execute through the v0 API."""
import json
import time
import urllib.request

from vision_client import parse_object


def run_agent(client,sid,model_key,out,config):
    deadline=time.monotonic()+config.get('per_task_timeout_seconds',360)
    messages=[{'role':'system','content':
        'You are a file-work assistant. You may act only through the exec tool in the sandbox. '
        'Return ONLY one JSON object: {"tool":"exec","argv":["program","arg",...]}, '
        'or {"tool":"finish","answer":"final answer"}. No prose outside JSON. '
        'Use python3 if needed. Read files before editing, preserve unrelated contents. '
        'Workdir is /work. Each tool command is limited to 3 seconds.'},
        {'role':'user','content':config['task']}]
    calls=[];tools=[];answer=None;stop_reason='step_limit'
    def progress():
        (out/'agent-progress.json').write_text(json.dumps({'answer':answer,'model_calls':calls,'tool_runs':tools,'messages':messages,'stop_reason':stop_reason},ensure_ascii=False,indent=2))
    for step in range(config['max_steps']):
        remaining=deadline-time.monotonic()
        if remaining<=0:raise TimeoutError('Agent task deadline exceeded')
        payload={'model':config['model'],'messages':messages,'max_tokens':config['max_output_tokens'],
                 'temperature':config['temperature'],'chat_template_kwargs':{'enable_thinking':False}}
        headers={'Content-Type':'application/json'}
        if model_key:headers['Authorization']='Bearer '+model_key
        request=urllib.request.Request(config['base_url']+'/chat/completions',
                                       data=json.dumps(payload).encode(),headers=headers)
        started=time.monotonic()
        with urllib.request.urlopen(request,timeout=min(60,remaining)) as response:body=json.load(response)
        event={'seconds':time.monotonic()-started,'response':body}
        calls.append(event)
        (out/f'model-call-{len(calls):02d}.json').write_text(json.dumps(event,ensure_ascii=False,indent=2))
        progress()
        content=body['choices'][0]['message']['content'];action=parse_object(content)
        messages.append({'role':'assistant','content':content})
        if action.get('tool')=='finish':
            answer=action.get('answer');stop_reason='finish';progress();break
        if action.get('tool')!='exec' or not isinstance(action.get('argv'),list):raise ValueError('Invalid agent action')
        status,run=client.request('POST',f'/v0/sessions/{sid}/runs',
            {'request_id':f'agent-step-{step}','argv':action['argv'],'timeout_seconds':3})
        if status!=200:raise RuntimeError(run)
        completed=client.wait(run['id']);tools.append(completed)
        messages.append({'role':'user','content':'Tool result: '+json.dumps(completed['result'])})
        progress()
    return {'answer':answer,'model_calls':calls,'tool_runs':tools,'messages':messages,'stop_reason':stop_reason}
