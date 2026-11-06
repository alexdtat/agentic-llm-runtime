"""Headless test client; a desktop UI is still to be implemented."""
import json
import time
import urllib.error
import urllib.request


class Client:
    def __init__(self,base_url,token):self.base=base_url;self.token=token

    def request(self,method,path,payload=None):
        request=urllib.request.Request(self.base+path,method=method,
            data=None if payload is None else json.dumps(payload).encode(),
            headers={'Authorization':'Bearer '+self.token,'Content-Type':'application/json'})
        try:
            with urllib.request.urlopen(request,timeout=20) as response:return response.status,json.load(response)
        except urllib.error.HTTPError as exc:return exc.code,json.load(exc)

    def wait(self,rid,timeout=15):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            status,value=self.request('GET','/v0/runs/'+rid)
            if status!=200:raise RuntimeError(value)
            if value['result'] is not None:return value
            time.sleep(.03)
        raise TimeoutError(rid)
