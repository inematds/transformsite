"""Provedores de LLM e embeddings.

Padrão: Ollama local (nada sai da máquina). Alternativas sem API key:
- `openai_compat`: qualquer servidor local compatível com OpenAI (vLLM, LM Studio, llama.cpp).
- `claude_cli`: usa o Claude Code instalado e logado (`claude -p`) — pela assinatura.
- `codex_cli`: usa o Codex CLI instalado e logado (`codex exec`) — pela assinatura.
- `fake`: determinístico, para testes e demonstração offline.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx

from .config import LLMConfig


class LLMError(RuntimeError):
    pass


def parse_json(text: str) -> Any:
    """Extrai o primeiro objeto JSON de um texto (tolera cercas ``` e raciocínio vazado)."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S)
    if m:
        text = m.group(1)
    start = text.find("{")
    if start < 0:
        raise LLMError(f"sem JSON na resposta: {text[:200]!r}")
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start : i + 1])
    raise LLMError(f"JSON incompleto: {text[:200]!r}")


@dataclass
class LLM:
    cfg: LLMConfig
    fake_fn: Callable[[list[dict], bool], str] | None = None

    # ---------- chat ----------
    def chat(self, messages: list[dict], json_mode: bool = False) -> str:
        p = self.cfg.provider
        if p == "fake":
            return (self.fake_fn or _fake_default)(messages, json_mode)
        if p == "ollama":
            return self._ollama_chat(messages, json_mode)
        if p == "openai_compat":
            return self._openai_chat(messages, json_mode)
        if p in ("claude_cli", "codex_cli"):
            return self._cli_chat(messages, json_mode)
        raise LLMError(f"provider desconhecido: {p}")

    def chat_json(self, messages: list[dict]) -> Any:
        out = self.chat(messages, json_mode=True)
        try:
            return parse_json(out)
        except (LLMError, json.JSONDecodeError):
            # 1 nova tentativa pedindo só JSON
            out = self.chat(messages + [{"role": "user", "content": "Responda APENAS com o objeto JSON válido."}], True)
            return parse_json(out)

    def _ollama_chat(self, messages, json_mode):
        body: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": messages,
            "stream": False,
            "think": False,
            "options": {"temperature": self.cfg.temperature},
        }
        if json_mode:
            body["format"] = "json"
        r = self._post_retry(f"{self.cfg.base_url.rstrip('/')}/api/chat", body, "ollama")
        return r.json()["message"]["content"]

    def _post_retry(self, url: str, body: dict, label: str, attempts: int = 4) -> httpx.Response:
        """POST com nova tentativa em erro 5xx/conexão (ex.: Ollama recarregando o modelo)."""
        import time

        last: Exception | None = None
        for i in range(attempts):
            try:
                r = httpx.post(url, json=body, timeout=self.cfg.timeout)
                if r.status_code < 500:
                    r.raise_for_status()
                    return r
                last = httpx.HTTPStatusError(f"{r.status_code}: {r.text[:200]}", request=r.request, response=r)
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
                last = e
            except httpx.HTTPError as e:
                raise LLMError(f"{label}: {e}") from e
            time.sleep(min(2 * 2**i, 15))
        raise LLMError(f"{label}: {last}")

    def _openai_chat(self, messages, json_mode):
        body: dict[str, Any] = {"model": self.cfg.model, "messages": messages, "temperature": self.cfg.temperature}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        r = self._post_retry(f"{self.cfg.base_url.rstrip('/')}/chat/completions", body, "openai_compat")
        return r.json()["choices"][0]["message"]["content"]

    def _cli_chat(self, messages, json_mode):
        prompt = "\n\n".join(f"[{m['role'].upper()}]\n{m['content']}" for m in messages)
        if json_mode:
            prompt += "\n\nResponda APENAS com um objeto JSON válido, sem texto antes ou depois."
        try:
            if self.cfg.provider == "claude_cli":
                cmd = ["claude", "-p", "--output-format", "text"]
                if self.cfg.model and not self.cfg.model.startswith(("qwen", "llama")):
                    cmd += ["--model", self.cfg.model]
                r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=self.cfg.timeout)
                if r.returncode != 0:
                    raise LLMError(f"claude -p falhou: {r.stderr[-300:]}")
                return r.stdout
            with tempfile.TemporaryDirectory() as td:
                out = Path(td) / "last.txt"
                cmd = ["codex", "exec", "--skip-git-repo-check", "-o", str(out), "-"]
                r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=self.cfg.timeout, cwd=td)
                if r.returncode != 0:
                    raise LLMError(f"codex exec falhou: {r.stderr[-300:]}")
                return out.read_text(encoding="utf-8") if out.exists() else r.stdout
        except FileNotFoundError as e:
            raise LLMError(f"CLI não encontrado: {e}") from e
        except subprocess.TimeoutExpired as e:
            raise LLMError("timeout no CLI") from e

    # ---------- embeddings ----------
    @property
    def embed_provider(self) -> str:
        if self.cfg.embed_provider:
            return self.cfg.embed_provider
        return self.cfg.provider if self.cfg.provider in ("ollama", "openai_compat", "fake") else "none"

    def embed(self, texts: list[str]) -> list[list[float]] | None:
        p = self.embed_provider
        if p == "none" or not texts:
            return None
        if p == "fake":
            return [_hash_embed(t) for t in texts]
        out: list[list[float]] = []
        for i in range(0, len(texts), 32):
            batch = texts[i : i + 32]
            try:
                if p == "ollama":
                    r = self._post_retry(f"{self.cfg.base_url.rstrip('/')}/api/embed", {"model": self.cfg.embed_model, "input": batch}, "embed")
                    out.extend(r.json()["embeddings"])
                else:
                    r = self._post_retry(f"{self.cfg.base_url.rstrip('/')}/embeddings", {"model": self.cfg.embed_model, "input": batch}, "embed")
                    out.extend(d["embedding"] for d in r.json()["data"])
            except httpx.HTTPError as e:
                raise LLMError(f"embed: {e}") from e
        return out


def _hash_embed(text: str, dim: int = 256) -> list[float]:
    """Embedding determinístico (bag of hashed words) — só para testes/offline."""
    v = [0.0] * dim
    for w in re.findall(r"\w+", text.lower()):
        h = int(hashlib.md5(w.encode()).hexdigest(), 16)
        v[h % dim] += 1.0
    n = sum(x * x for x in v) ** 0.5 or 1.0
    return [x / n for x in v]


def _fake_default(messages: list[dict], json_mode: bool) -> str:
    """LLM falso: suficiente para os fluxos rodarem sem modelo.

    - Pedido de resposta RAG: cita o trecho [1] se existir, senão "não sei".
    - Demais pedidos JSON: objeto vazio (o motor cai nas regras determinísticas).
    """
    sys = messages[0]["content"] if messages else ""
    user = messages[-1]["content"] if messages else ""
    if "TAREFA: RESPONDER_COM_FONTE" in sys:
        m = re.search(r"\[1\][^\n]*\n(.+?)(?:\n\[2\]|\n</trechos>)", user, flags=re.S)
        if not m or "SEM_TRECHOS" in user:
            return json.dumps({"nao_sei": True, "resposta": "", "fontes": []})
        frase = re.split(r"(?<=[.!?])\s", m.group(1).strip())[0][:300]
        return json.dumps({"nao_sei": False, "resposta": frase, "fontes": [1]})
    return "{}" if json_mode else "ok"
