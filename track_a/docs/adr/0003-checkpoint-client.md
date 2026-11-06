# ADR 0003: terminal client for the first checkpoint

Status: implemented for checkpoint 1; not the final desktop client.

The first interface is a Python terminal client using the documented HTTP v0
protocol. It creates sessions, starts commands, displays events by polling,
cancels runs, and releases sessions. Arguments remain argv arrays; the client
does not run agent commands through the host shell. Authentication is read from
the environment and is not a command-line argument.

This keeps the transport independently demonstrable while the desktop UI and
client-folder mount remain separate work for checkpoint 2. Polling returns the
current event list and deduplicates sequence numbers. It is not SSE streaming.
Closing a session destroys its temporary workdir; no user folder is mounted by
this client. Future integration must preserve the protocol tests and document
reconnection semantics before exposing a real client workdir.
