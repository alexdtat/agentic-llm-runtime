"""Headless test client; a desktop UI is still to be implemented."""
import json
import time
import urllib.error
import urllib.request


class Client:
    def __init__(self,base_url,token):self.base=base_url;self.token=token

    def request(self,method,path,payload=None,timeout=20):
        request=urllib.request.Request(self.base+path,method=method,
            data=None if payload is None else json.dumps(payload).encode(),
            headers={'Authorization':'Bearer '+self.token,'Content-Type':'application/json'})
        try:
            with urllib.request.urlopen(request,timeout=timeout) as response:return response.status,json.load(response)
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, json.load(exc)

    def wait(self,rid,timeout=15):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            status,value=self.request('GET','/v0/runs/'+rid,timeout=max(.01,deadline-time.monotonic()))
            if status!=200:raise RuntimeError(value)
            if value['result'] is not None:return value
            time.sleep(.03)
        raise TimeoutError(rid)

    def events(self, session_id, after, timeout=5):
        url = f'{self.base}/v2/sessions/{session_id}/events?after={after}'
        request = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + self.token,
                                                       'Accept': 'text/event-stream'})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, [json.loads(line[6:]) for line in response if line.startswith(b'data: ')]
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, json.load(exc)

    def v2(self, method, path, body=None, timeout=15):
        status, value = self.request(method, '/v2/sessions' + path, body, timeout)
        if status != 200:
            raise RuntimeError(f'HTTP {status}: {value}')
        return value

    def wait_run(self, sid, rid, timeout=75):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            run = self.v2('GET', f'/{sid}/runs/{rid}', timeout=max(.01, deadline - time.monotonic()))
            if run['result'] is not None:
                return run
            time.sleep(.05)
        raise TimeoutError(rid)
