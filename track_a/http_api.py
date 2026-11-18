"""Bounded JSON HTTP, shared by the gateway and the private executor."""
import json
import math
import re
import secrets
import os
import signal
import threading
from urllib.parse import parse_qs, urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Conflict(ValueError):
    pass


def key(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', value):
        raise TypeError('Invalid request_id')


def command(value):
    if not isinstance(value, dict) or set(value) != {'request_id', 'argv', 'timeout_seconds'}:
        raise TypeError('Invalid command fields')
    key(value['request_id'])
    argv = value['argv']
    if not isinstance(argv, list) or not 1 <= len(argv) <= 100:
        raise TypeError('Invalid argv')
    if any(not isinstance(a, str) or '\0' in a for a in argv):
        raise TypeError('Invalid argv')
    timeout = value['timeout_seconds']
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not .05 <= timeout <= 10:
        raise TypeError('Invalid timeout')


class JsonHandler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, *args):
        pass

    def reply(self, status, value):
        data = json.dumps(value, allow_nan=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def reply_events(self, events):
        data = ''.join(f"id: {event['sequence']}\ndata: {json.dumps(event)}\n\n"
                       for event in events).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def handle_request(self):
        try:
            if self.path == '/v0/health' and self.command == 'GET':
                return self.reply(*self.server.app.health())
            app = self.server.app
            authorization = self.headers.get('Authorization', '')
            if hasattr(app, 'authenticate'):
                owner = app.authenticate(authorization)
                if owner is None:
                    return self.reply(401, {'error': 'unauthorized'})
            elif not secrets.compare_digest(authorization, 'Bearer ' + self.server.token):
                return self.reply(401, {'error': 'unauthorized'})
            try:
                size = int(self.headers.get('Content-Length', '0'))
            except ValueError:
                raise TypeError('Invalid Content-Length')
            if not 0 <= size <= 16384:
                return self.reply(413, {'error': 'request_too_large'})
            if self.headers.get('Transfer-Encoding'):
                raise TypeError('Chunked requests are unsupported')
            try:
                body = json.loads(self.rfile.read(size)) if size else {}
            except (ValueError, UnicodeError):
                raise TypeError('Invalid JSON')
            if not isinstance(body, dict):
                raise TypeError('Expected JSON object')
            if hasattr(app, 'authenticate'):
                url = urlsplit(self.path)
                query = parse_qs(url.query, keep_blank_values=True)
                if 'after' not in query and self.headers.get('Last-Event-ID') is not None:
                    query['after'] = [self.headers['Last-Event-ID']]
                status, value = app.route(owner, self.command, url.path, body, query)
                if status == 200 and url.path.endswith('/events'):
                    return self.reply_events(value)
            else:
                status, value = app.route(self.command, self.path, body)
            self.reply(status, value)
        except KeyError:
            self.reply(404, {'error': 'not_found'})
        except Conflict as exc:
            self.reply(409, {'error': str(exc)})
        except (TypeError, ValueError) as exc:
            self.reply(400, {'error': str(exc)})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            # Never reflect upstream bodies, tokens or arbitrary exception strings.
            self.reply(503, {'error': 'service_unavailable'})

    do_GET = handle_request
    do_POST = handle_request
    do_DELETE = handle_request

def serve(app, port):
    token = os.environ.get('PROBE_TOKEN', '')
    if not hasattr(app, 'authenticate') and (not token or token == 'replace-with-a-random-local-token'):
        raise RuntimeError('Set a non-placeholder PROBE_TOKEN')
    server = ThreadingHTTPServer((os.environ.get('BIND_HOST', '127.0.0.1'), port), JsonHandler)
    server.app, server.token = app, token
    def stop(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=.1)
    finally:
        try:
            app.shutdown()
        finally:
            server.server_close()

