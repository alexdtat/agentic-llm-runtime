"""Synthetic model and process-free B5 command for the offline smoke suite."""
import base64
import csv
import json
import re
import statistics
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

from track_a.fixture_executor import FixtureExecutor
from track_a.model_proxy import ModelProxy

STATS_COMMAND = 'python3 stats.py data.csv'
OBSERVATIONS = 'Tool observations: '


class SmokeExecutor(FixtureExecutor):
    @staticmethod
    def apply(files, call):
        if call['tool'] != 'RunCommand' or call['args']['command'] != STATS_COMMAND:
            return FixtureExecutor.apply(files, call)
        try:
            # Only CSV data is interpreted. stats.py is never evaluated or invoked.
            if 'stats.py' not in files:
                raise KeyError('stats.py')
            rows = csv.DictReader(base64.b64decode(files['data.csv']).decode().splitlines())
            values = [float(row['value']) for row in rows]
            output = {'count': len(values), 'mean': statistics.mean(values),
                      'median': statistics.median(values)}
            return dict(exit_code=0, stdout=json.dumps(output), stderr='', error_kind=None, truncated=False)
        except (KeyError, ValueError):
            return dict(exit_code=1, stdout='', stderr='Invalid stats fixture',
                        error_kind='command_failure', truncated=False)


def read(path):
    return 'ReadFile', {'path': path}


def write(path, content):
    return 'WriteFile', {'path': path, 'data': base64.b64encode(content.encode()).decode()}


def command(value):
    return 'RunCommand', {'command': value}


def content(result):
    return base64.b64decode(result['data']).decode()


def read_port():
    [config] = yield [read('config/app.yaml')]
    port = re.search(r'^    port: (\d+)$', content(config), re.MULTILINE)[1]
    yield [write('answer.txt', port + '\n')]


def search_definition():
    [listing] = yield [command('fixture:list')]
    candidates = [path for path in listing['stdout'].splitlines() if path.endswith('.py')]
    results = yield [read(path) for path in candidates]
    definition = next(path for path, result in zip(candidates, results)
                      if re.search(r'^def parse_date\(', content(result), re.MULTILINE))
    yield [write('answer.txt', definition + '\n')]


def create_config():
    yield [read('spec.md')]
    config = {'service': 'reports', 'host': '127.0.0.1', 'port': 8137, 'debug': False,
              'limits': {'timeout_seconds': 30, 'retries': 2}}
    yield [write('config.json', json.dumps(config, indent=2) + '\n')]


def edit_readme():
    [readme] = yield [read('README.md')]
    updated = content(readme).replace('pip install example-package==1.2',
                                      'pip install example-package==1.3', 1)
    yield [write('README.md', updated)]


def run_stats():
    [stats] = yield [command(STATS_COMMAND)]
    median = json.loads(stats['stdout'])['median']
    yield [write('answer.txt', f'{median:g}\n')]


def write_slug():
    yield [read('spec.md')]
    source = ('import re\n\n\n'
              'def slugify(text):\n'
              '    return re.sub(r"[^A-Za-z0-9]+", "-", text).lower().strip("-")\n')
    yield [write('slug.py', source)]


def reason_connection():
    [schedule] = yield [read('schedule.csv')]
    rows = csv.DictReader(content(schedule).splitlines())
    connection = next(row for row in rows if (
        row['city'], row['arriving_service'], row['departing_service']) == ('Y', 'PN311', 'PN720'))
    arrival = datetime.strptime(connection['arrival'], '%H:%M')
    departure = datetime.strptime(connection['departure'], '%H:%M')
    minutes = (departure - arrival).seconds // 60
    answer = 'yes' if minutes >= int(connection['minimum_connection_minutes']) else 'no'
    yield [write('answer.txt', f'{answer}\n{minutes}\n')]


def summarize_sales():
    [listing] = yield [command('fixture:list')]
    sources = [path for path in listing['stdout'].splitlines() if path.endswith('.csv')]
    total = Decimal(0)
    for start in range(0, len(sources), 4):
        results = yield [read(path) for path in sources[start:start + 4]]
        for result in results:
            rows = csv.DictReader(content(result).splitlines())
            total += sum(Decimal(row['amount']) for row in rows
                         if '2026-04-01' <= row['date'] <= '2026-06-30')
    yield [write('answer.txt', f'{total:.2f}\n')]


SCRIPTS = {'B1.1': read_port, 'B2.1': search_definition, 'B3.1': create_config,
           'B4.1': edit_readme, 'B5.1': run_stats, 'B6.1': write_slug,
           'B7.1': reason_connection, 'B8.1': summarize_sales}


class ScriptedModel(ModelProxy):
    def __init__(self, task_id):
        super().__init__(SimpleNamespace(backend='mock'))
        self.script = SCRIPTS[task_id]()
        self.pending = []
        self.count = 0

    def mock(self, messages):
        try:
            if self.pending:
                latest = messages[-1]['content']
                if not latest.startswith(OBSERVATIONS):
                    raise ValueError('Missing tool observations')
                observations = json.loads(latest[len(OBSERVATIONS):])
                if [row['id'] for row in observations] != self.pending:
                    raise ValueError('Tool observations out of sequence')
                results = [row['result'] for row in observations]
                if any(result['exit_code'] != 0 for result in results):
                    self.script.close()
                    return {'answer': 'A smoke tool failed.'}
                operations = self.script.send(results)
            else:
                operations = next(self.script)
        except StopIteration:
            return {'answer': 'Smoke script finished.'}
        calls = []
        for tool, args in operations:
            self.count += 1
            calls.append({'id': f'call-{self.count}', 'tool': tool, 'args': args})
        self.pending = [call['id'] for call in calls]
        return {'calls': calls}
