"""Only this service needs provider settings. The mock implements the same wire DTO."""
import base64
import os

from track_a.contracts import fields, model_response
from track_a.http_api import serve
from track_a.model import Model
from vision_client import parse_object


class ModelProxy:
    def __init__(self, model):
        self.model = model

    def health(self):
        return 200, {'status': 'ok', 'backend': self.model.backend}

    def shutdown(self):
        pass

    def route(self, method, path, body):
        if method != 'POST' or path != '/generate':
            return 404, {'error': 'unknown_route'}
        fields(body, 'messages')
        messages = body['messages']
        if not isinstance(messages, list) or not messages:
            raise TypeError('Invalid messages')
        for message in messages:
            fields(message, 'role', 'content')
            if message['role'] not in ('system', 'user', 'assistant') or not isinstance(message['content'], str):
                raise TypeError('Invalid model message')
        if self.model.backend == 'mock':
            return 200, model_response(self.mock(messages), None)
        content, usage = self.model.generate(messages, 10)
        return 200, model_response(parse_object(content), usage)

    @staticmethod
    def mock(messages):
        last = messages[-1]['content']
        if last.startswith('Tool observations: '):
            return {'answer': 'Fixture tool loop completed.'}
        if last == 'fixture:history':
            return {'answer': f"Prior assistant messages: {sum(m['role'] == 'assistant' for m in messages)}"}
        if last == 'fixture:no-write':
            return {'answer': 'Completed without a tool.'}
        if last == 'fixture:nonzero':
            return {'calls': [{'id': 'cmd', 'tool': 'RunCommand', 'args': {'command': 'fixture:unsupported'}}]}
        data = base64.b64encode(b'mock-ok\n').decode()
        return {'calls': [
            {'id': 'write', 'tool': 'WriteFile', 'args': {'path': 'mock.txt', 'data': data}},
            {'id': 'read', 'tool': 'ReadFile', 'args': {'path': 'mock.txt'}},
        ]}


if __name__ == '__main__':
    serve(ModelProxy(Model()), int(os.environ.get('PORT', '8082')))
