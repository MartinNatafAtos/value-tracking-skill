"""Tests pour la couche d'abstraction LLM — clients mockés, aucun appel réseau."""

from unittest.mock import MagicMock

import scripts.llm as llm_module
from scripts.config import Config


def _fake_openai_response(text: str):
    choice = MagicMock()
    choice.message.content = text
    response = MagicMock()
    response.choices = [choice]
    return response


def test_provider_openai_appelle_le_bon_client(monkeypatch):
    monkeypatch.setattr(Config, "LLM_PROVIDER", "openai")
    monkeypatch.setattr(Config, "OPENAI_MODEL", "gpt-4.1")
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = _fake_openai_response("bonjour")
    monkeypatch.setattr(llm_module, "_get_openai_client", lambda: fake_client)

    result = llm_module.chat_completion([{"role": "user", "content": "salut"}])

    assert result == "bonjour"
    kwargs = fake_client.chat.completions.create.call_args.kwargs
    assert kwargs["model"] == "gpt-4.1"


def test_provider_azure_appelle_le_bon_client(monkeypatch):
    monkeypatch.setattr(Config, "LLM_PROVIDER", "azure")
    monkeypatch.setattr(Config, "AZURE_OPENAI_DEPLOYMENT", "mon-deploiement")
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = _fake_openai_response("ok")
    monkeypatch.setattr(llm_module, "_get_azure_client", lambda: fake_client)

    result = llm_module.chat_completion([{"role": "user", "content": "salut"}])

    assert result == "ok"
    kwargs = fake_client.chat.completions.create.call_args.kwargs
    assert kwargs["model"] == "mon-deploiement"


def test_provider_anthropic_extrait_le_system_prompt(monkeypatch):
    monkeypatch.setattr(Config, "LLM_PROVIDER", "anthropic")
    monkeypatch.setattr(Config, "ANTHROPIC_MODEL", "claude-test")

    block = MagicMock()
    block.text = "réponse claude"
    response = MagicMock()
    response.content = [block]
    fake_client = MagicMock()
    fake_client.messages.create.return_value = response
    monkeypatch.setattr(llm_module, "_get_anthropic_client", lambda: fake_client)

    messages = [
        {"role": "system", "content": "Tu es un expert."},
        {"role": "user", "content": "Analyse ceci."},
    ]
    result = llm_module.chat_completion(messages, json_mode=True)

    assert result == "réponse claude"
    kwargs = fake_client.messages.create.call_args.kwargs
    assert "Tu es un expert." in kwargs["system"]
    assert "JSON" in kwargs["system"]
    assert "temperature" not in kwargs
    assert kwargs["messages"] == [{"role": "user", "content": "Analyse ceci."}]
