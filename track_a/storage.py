"""One-process durable state. Commit intent before launching a side effect."""
import json
import sqlite3
from pathlib import Path


class Journal:
    def __init__(self, path=':memory:', *, isolated=False):
        if path != ':memory:':
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        if isolated:
            self.db.execute('PRAGMA journal_mode=DELETE')
            self.db.execute('PRAGMA secure_delete=ON')
        else:
            self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS state (id TEXT PRIMARY KEY, data TEXT NOT NULL)')
        self.db.commit()

    def load(self):
        row = self.db.execute("SELECT data FROM state WHERE id='snapshot'").fetchone()
        return json.loads(row[0]) if row else {}

    def save(self, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO state VALUES ('snapshot', ?)",
                            (json.dumps(value, allow_nan=False),))


class SessionJournals:
    """One removable payload database per session. Closed entries become tombstones."""

    def __init__(self, root=None):
        self.root = Path(root) if root else None
        self.journals = {}
        if self.root:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)

    def journal(self, sid):
        if sid not in self.journals:
            path = self.root / (sid + '.sqlite') if self.root else ':memory:'
            self.journals[sid] = Journal(path, isolated=True)
        return self.journals[sid]

    def load(self):
        if self.root:
            for path in self.root.glob('*.sqlite'):
                self.journal(path.stem)
        return {sid: state for sid, journal in self.journals.items() if (state := journal.load())}

    def save(self, session):
        self.journal(session['id']).save(session)
