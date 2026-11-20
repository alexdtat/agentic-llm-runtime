"""Build a bounded model context and run the sequential model/tool loop."""
import json

from track_a.contracts import model_response

SYSTEM = """Work only through the provided workspace tools. Return one JSON object:
{"answer": "final answer"} or {"calls": [{"id":"unique-id","tool":"ToolName","args":{...}}]}.
Tools: RunCommand {"command":"shell command"}, ReadFile {"path":"relative/path"},
WriteFile {"path":"relative/path","data":"base64 bytes"}. Check tool results before finishing."""


class RunError(RuntimeError):
    pass


def run_agent(history, message, config, generate, execute, emit, check_active):
    messages = [{'role': 'system', 'content': SYSTEM}, *history, {'role': 'user', 'content': message}]
    tool_ids = set()
    usage = []
    for step in range(config['steps']):
        check_active()
        if len(json.dumps(messages).encode()) > config['context_bytes']:
            raise RunError('context_limit')
        emit('model.started', step=step)
        response = generate(messages)
        check_active()
        action = model_response(response['action'], response['usage'])['action']
        usage.append(response['usage'])
        emit('model.completed', step=step, action=action, usage=response['usage'])
        if 'answer' in action:
            return action['answer'], usage
        if tool_ids.intersection(call['id'] for call in action['calls']):
            raise RunError('duplicate_tool_call_id')
        messages.append({'role': 'assistant', 'content': json.dumps(action)})
        observations = []
        for call in action['calls']:
            check_active()
            tool_ids.add(call['id'])
            observations.append({'id': call['id'], 'result': execute(call)})
        messages.append({'role': 'user', 'content': 'Tool observations: ' + json.dumps(observations)})
    raise RunError('step_limit')
