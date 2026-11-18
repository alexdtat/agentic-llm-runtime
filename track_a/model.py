"""Server-only model boundary; mock is explicitly not a quality experiment."""
import json
import os
import urllib.parse
import urllib.request

from vision_client import parse_object

SYSTEM = ('You are a file-work assistant. Act only through the exec tool in /work. '
          'Return one JSON object: {"tool":"exec","argv":["program","arg",...]}, '
          'or {"tool":"finish","answer":"final answer"}. '
          'Read files before editing; preserve unrelated contents. Commands have a 3 second limit.')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Model:
    def __init__(self):
        self.backend = os.environ.get('MODEL_BACKEND', 'mock')
        if self.backend not in {'mock', 'openai-compatible'}:
            raise ValueError('Unknown model backend')
        self.base = os.environ.get('MODEL_BASE_URL', '').rstrip('/')
        self.name = os.environ.get('MODEL_NAME', '')
        self.key = os.environ.get('MODEL_API_KEY', '')
        if self.backend != 'mock':
            url = urllib.parse.urlsplit(self.base)
            if (url.scheme not in {'http', 'https'} or not url.netloc or url.username
                    or url.password or url.query or url.fragment or not self.name):
                raise ValueError('Set MODEL_BASE_URL and MODEL_NAME')

    def ask(self, messages, timeout):
        if self.backend == 'mock':
            # Fixed infrastructure fixture: cannot be counted as an LLM attempt.
            if any(m['role'] == 'assistant' for m in messages):
                action = {'tool': 'finish', 'answer': 'Mock completed; no LLM was called.'}
            else:
                code = ("import time;time.sleep(8)" if messages[1]['content'] == 'mock:slow' else
                        "from pathlib import Path;Path('/work/mock.txt').write_text('mock-ok\\n');print('mock-ok')")
                action = {'tool': 'exec', 'argv': ['python3', '-c', code]}
            return action, json.dumps(action), {}
        content, usage = self.generate(messages, timeout)
        return parse_object(content), content, usage

    def generate(self, messages, timeout):
        payload = {'model': self.name, 'messages': messages, 'max_tokens': 512, 'temperature': 0}
        headers = {'Content-Type': 'application/json'}
        if self.key:
            headers['Authorization'] = 'Bearer ' + self.key
        request = urllib.request.Request(self.base + '/chat/completions',
                                         data=json.dumps(payload).encode(), headers=headers)
        with urllib.request.build_opener(NoRedirect).open(request, timeout=min(20, timeout)) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError('Model response exceeds limit')
        body = json.loads(raw)
        content = body['choices'][0]['message']['content']
        usage = body.get('usage', {})
        usage = {k: v for k, v in usage.items()
                 if k in {'prompt_tokens', 'completion_tokens', 'total_tokens'} and type(v) is int}
        return content, usage
