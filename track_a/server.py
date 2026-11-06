"""A v0 transport prototype, not a finished runtime or production boundary."""
import json
import os
import re
import secrets
import signal
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path

IMAGE=os.environ.get('SANDBOX_IMAGE','course-feasibility:2026-11-06')
TOKEN=os.environ.get('PROBE_TOKEN','local-diagnostic-token')
WORKER=Path(__file__).with_name('worker.py').read_text()


def docker(*args,timeout=15,input=None):
    return subprocess.run(['docker',*map(str,args)],input=input,text=True,capture_output=True,timeout=timeout)


class State:
    def __init__(self):
        self.lock=threading.RLock();self.sessions={};self.runs={};self.requests={}

    def create(self,key):
        with self.lock:
            if ('session',key) in self.requests:
                return self.sessions[self.requests['session',key]]
            sid=uuid.uuid4().hex
            name='course-probe-a-api-'+sid[:12]
            r=docker('run','-d','--name',name,'--runtime','runc','--init','--network','none',
                     '--cpus','1','--memory','256m','--pids-limit','64','--read-only',
                     '--cap-drop','ALL','--security-opt','no-new-privileges',
                     '--tmpfs','/work:rw,size=16m,mode=1777','--tmpfs','/tmp:rw,size=16m,mode=1777',
                     '--user','1000:1000',IMAGE)
            if r.returncode: raise RuntimeError(r.stderr)
            session={'id':sid,'container':name,'status':'open'}
            self.sessions[sid]=session;self.requests['session',key]=sid
            return session

    def start(self,sid,payload):
        with self.lock:
            session=self.sessions.get(sid)
            if not session or session['status']!='open':raise KeyError('Unknown open session')
            key=('run',sid,payload['request_id'])
            if key in self.requests:
                run=self.runs[self.requests[key]]
                if run['request']!=payload:raise ValueError('Idempotency key reused with different payload')
                return run
            rid=uuid.uuid4().hex
            run={'id':rid,'session_id':sid,'status':'running','request':payload,
                 'events':[{'type':'started','sequence':0}],'result':None}
            self.runs[rid]=run;self.requests[key]=rid
            threading.Thread(target=self.execute,args=(run,session),daemon=True).start()
            return run

    def execute(self,run,session):
        try:
            r=docker('exec','-i',session['container'],'python3','-c',WORKER,run['id'],
                     input=json.dumps(run['request']),timeout=run['request']['timeout_seconds']+10)
            if r.returncode: result={'status':'failed','error_kind':'transport','exit_code':None,'stdout':'','stderr':r.stderr[-4000:]}
            else: result=json.loads(r.stdout)
        except Exception as exc:
            result={'status':'failed','error_kind':'sandbox','exit_code':None,'stdout':'','stderr':str(exc)}
        with self.lock:
            if run['result'] is None:
                run['result']=result;run['status']=result['status']
                run['events'].append({'type':'result','sequence':1,**result})

    def cancel(self,rid):
        with self.lock:
            run=self.runs[rid]
            if run['result'] is not None:return run
            session=self.sessions[run['session_id']]
        code=f"from pathlib import Path;p=Path('/tmp/probe-runs/{rid}/cancel');p.parent.mkdir(parents=True,exist_ok=True);p.touch()"
        r=docker('exec',session['container'],'python3','-c',code)
        if r.returncode:raise RuntimeError(r.stderr)
        return run

    def release(self,sid):
        with self.lock:
            session=self.sessions[sid]
            if session['status']=='closed':return session
            session['status']='closing'
            active=[r['id'] for r in self.runs.values() if r['session_id']==sid and r['result'] is None]
        for rid in active:self.cancel(rid)
        deadline=time.monotonic()+2
        while any(self.runs[r]['result'] is None for r in active) and time.monotonic()<deadline:time.sleep(.02)
        docker('rm','-f',session['container'])
        session['status']='closed'
        return session


STATE=State()


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass

    def reply(self,status,value):
        with STATE.lock:data=json.dumps(value).encode()
        self.send_response(status)
        self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)))
        self.end_headers();self.wfile.write(data)

    def handle_request(self):
        if self.path=='/v0/health' and self.command=='GET':return self.reply(200,{'version':'v0','status':'ok'})
        if not secrets.compare_digest(self.headers.get('Authorization',''),'Bearer '+TOKEN):
            return self.reply(401,{'error':'unauthorized'})
        try:
            size=int(self.headers.get('Content-Length',0))
            if not 0<=size<=16384:return self.reply(413,{'error':'request_too_large'})
            body=json.loads(self.rfile.read(size)) if size else {}
            parts=self.path.strip('/').split('/')
            if self.command=='POST' and parts==['v0','sessions']:
                if set(body)!={'request_id'}:raise TypeError('Invalid fields')
                self.validate_key(body.get('request_id'));value=STATE.create(body['request_id'])
            elif self.command=='POST' and len(parts)==4 and parts[:2]==['v0','sessions'] and parts[3]=='runs':
                self.validate_key(body.get('request_id'))
                if set(body)!={'request_id','argv','timeout_seconds'}:raise TypeError('Invalid fields')
                if not isinstance(body['argv'],list) or not body['argv'] or len(body['argv'])>100:raise TypeError('Invalid argv')
                if any(not isinstance(a,str) or '\0' in a for a in body['argv']):raise TypeError('Invalid argv')
                if type(body['timeout_seconds']) not in (int,float) or not .05<=body['timeout_seconds']<=10:raise TypeError('Invalid timeout')
                value=STATE.start(parts[2],body)
            elif self.command=='GET' and len(parts)==3 and parts[:2]==['v0','runs']:
                value=STATE.runs[parts[2]]
            elif self.command=='POST' and len(parts)==4 and parts[:2]==['v0','runs'] and parts[3]=='cancel':
                value=STATE.cancel(parts[2])
            elif self.command=='DELETE' and len(parts)==3 and parts[:2]==['v0','sessions']:
                value=STATE.release(parts[2])
            else:return self.reply(404,{'error':'unknown_route'})
            self.reply(200,value)
        except KeyError as exc:self.reply(404,{'error':str(exc)})
        except ValueError as exc:self.reply(409,{'error':str(exc)})
        except TypeError as exc:self.reply(400,{'error':str(exc)})
        except Exception as exc:self.reply(500,{'error':str(exc)})

    @staticmethod
    def validate_key(value):
        if not isinstance(value,str) or not re.fullmatch('[a-zA-Z0-9_-]{1,80}',value):raise TypeError('Invalid request_id')

    do_GET=handle_request;do_POST=handle_request;do_DELETE=handle_request


def main():
    server=ThreadingHTTPServer((os.environ.get('BIND_HOST','127.0.0.1'),int(os.environ.get('PORT','0'))),Handler)
    ready=os.environ.get('READY_FILE')
    if ready:Path(ready).write_text(json.dumps({'port':server.server_port}))
    def stop(*args):threading.Thread(target=server.shutdown,daemon=True).start()
    signal.signal(signal.SIGTERM,stop)
    try:server.serve_forever(poll_interval=.05)
    finally:
        for sid in list(STATE.sessions):STATE.release(sid)
        server.server_close()


if __name__=='__main__':main()
