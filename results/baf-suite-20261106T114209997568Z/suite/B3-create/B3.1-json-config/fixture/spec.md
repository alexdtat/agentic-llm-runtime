# Configuration specification

Create `config.json` in the workdir root. It must be a JSON object with exactly these fields:

- `service`: string `reports`.
- `host`: string `127.0.0.1`.
- `port`: integer `8137`.
- `debug`: boolean `false`.
- `limits`: an object containing only integer `timeout_seconds` equal to `30` and integer `retries` equal to `2`.

No additional fields are allowed. Do not modify this specification.
