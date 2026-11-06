"""OpenAI-compatible vision client; the local key never enters artifacts."""
import base64
import json
import time
import urllib.request
from pathlib import Path


def local_api_key():
    path = Path.home() / ".config/hyperqwen/env"
    if path.exists():
        for line in path.read_text().splitlines():
            if line.startswith("VLLM_API_KEY="):
                return line.split("=", 1)[1].strip().strip("\"'")
    return ""


def parse_object(text):
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1])
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("Expected one JSON object")
    return value


def action_argv(action):
    name = action.get("action")
    if name == "click":
        x, y = action.get("x"), action.get("y")
        if (type(x) is not int or type(y) is not int or
                not 0 <= x < 1280 or not 0 <= y < 900):
            raise ValueError("Click outside the screenshot")
        return ["mousemove", str(x), str(y), "click", "1"]
    if name == "key":
        keys = {"ctrl+a", "ctrl+s", "ctrl+shift+f", "F1", "Escape", "Tab", "Return", "shift+Tab"}
        if action.get("key") not in keys:
            raise ValueError("Key not allowed in the bounded recolor diagnostic")
        return ["key", "--clearmodifiers", action["key"]]
    if name == "type":
        text = action.get("text")
        if not isinstance(text, str) or len(text) > 100 or any(c in text for c in "\r\n\x00"):
            raise ValueError("Invalid text")
        return ["type", "--clearmodifiers", "--", text]
    if name == "done":
        return []
    raise ValueError("Unsupported action")


class VisionClient:
    def __init__(self, base_url, model, key, out, config):
        # Never forward a local credential to an arbitrary remote host.
        if not base_url.startswith("http://127.0.0.1:"):
            raise ValueError("This diagnostic only accepts a loopback HyperQwen endpoint")
        self.base = base_url.rstrip("/")
        self.model, self.key, self.out, self.config = model, key, out, config
        self.calls = []

    def ask(self, screenshot, prompt):
        encoded = base64.b64encode(Path(screenshot).read_bytes()).decode()
        request_body = {
            "model": self.model,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + encoded}},
            ]}],
            "max_tokens": self.config["max_output_tokens"],
            "temperature": self.config["temperature"],
            "chat_template_kwargs": {"enable_thinking": self.config["enable_thinking"]},
            "stream": False,
        }
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = "Bearer " + self.key
        request = urllib.request.Request(self.base + "/chat/completions",
                                         data=json.dumps(request_body).encode(), headers=headers)
        start = time.monotonic()
        event = {"screenshot": str(screenshot), "prompt": prompt}
        try:
            with urllib.request.urlopen(request, timeout=self.config["request_timeout_seconds"]) as r:
                response = json.load(r)
            event["response"] = response
            content = response["choices"][0]["message"]["content"]
            return parse_object(content)
        except Exception as exc:
            event["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            event["seconds"] = time.monotonic() - start
            self.calls.append(event)
            (self.out / f"model-call-{len(self.calls):02d}.json").write_text(
                json.dumps(event, ensure_ascii=False, indent=2) + "\n")
