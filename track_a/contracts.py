"""Shared v2 wire validation for model actions and execution tools."""
import base64
from pathlib import PurePosixPath

from track_a.http_api import key


def fields(value, *expected):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise TypeError('Expected fields: ' + ', '.join(expected))


def file_path(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 256 or '\0' in value:
        raise TypeError('Invalid relative path')
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or '..' in path.parts or '.git' in path.parts:
        raise TypeError('Invalid relative path')
    return str(path)


def tool_call(value):
    fields(value, 'id', 'tool', 'args')
    key(value['id'])
    tool, args = value['tool'], value['args']
    if tool == 'RunCommand':
        fields(args, 'command')
        if not isinstance(args['command'], str) or not 1 <= len(args['command']) <= 4000 or '\0' in args['command']:
            raise TypeError('Invalid command')
    elif tool == 'ReadFile':
        fields(args, 'path')
        file_path(args['path'])
    elif tool == 'WriteFile':
        fields(args, 'path', 'data')
        file_path(args['path'])
        if not isinstance(args['data'], str) or len(args['data']) > 8192:
            raise TypeError('Invalid base64 data')
        base64.b64decode(args['data'], validate=True)
    else:
        raise TypeError('Unknown tool')
    return value


def fixture_files(files):
    if not isinstance(files, dict) or len(files) > 32:
        raise TypeError('Invalid fixture files')
    normalized = {}
    for path, data in files.items():
        tool_call({'id': 'fixture', 'tool': 'WriteFile', 'args': {'path': path, 'data': data}})
        name = file_path(path)
        if name in normalized:
            raise TypeError('Duplicate fixture path')
        normalized[name] = data
    return normalized


def model_response(action, usage):
    if not isinstance(action, dict):
        raise TypeError('Expected a model action')
    if 'answer' in action:
        fields(action, 'answer')
        if not isinstance(action['answer'], str) or len(action['answer']) > 8000:
            raise TypeError('Invalid final answer')
    else:
        fields(action, 'calls')
        calls = action['calls']
        if not isinstance(calls, list) or not 1 <= len(calls) <= 4:
            raise TypeError('Expected one to four calls')
        for call in calls:
            tool_call(call)
        if len({call['id'] for call in calls}) != len(calls):
            raise TypeError('Duplicate tool call ID')
    return {'action': action, 'usage': usage or None}
