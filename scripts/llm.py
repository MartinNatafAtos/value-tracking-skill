"""Abstraction du provider LLM — Anthropic, OpenAI, ou Azure OpenAI.

C'est ce module qui rend le skill « pilotable par n'importe quel LLM » côté
génération : le choix du modèle qui rédige la synthèse/les recommandations
est une variable d'environnement (Config.LLM_PROVIDER), pas un choix figé
dans le code.

Utilisation :
    from scripts.llm import chat_completion
    text = chat_completion(messages, json_mode=True)

Les clients sont instanciés de façon paresseuse (lazy) et mémoïsés : seul le
client du provider actif est jamais créé (pas d'erreur de clé manquante pour
un provider non utilisé).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from .config import Config

logger = logging.getLogger(__name__)

# ---- singletons paresseux ----
_openai_client = None
_azure_client = None
_anthropic_client = None


def _get_openai_client():
    global _openai_client
    if _openai_client is None:
        from openai import OpenAI  # lazy
        _openai_client = OpenAI(api_key=Config.OPENAI_API_KEY)
    return _openai_client


def _get_azure_client():
    global _azure_client
    if _azure_client is None:
        from openai import AzureOpenAI  # lazy
        _azure_client = AzureOpenAI(
            azure_endpoint=Config.AZURE_OPENAI_ENDPOINT,
            api_key=Config.AZURE_OPENAI_API_KEY,
            api_version=Config.AZURE_OPENAI_API_VERSION,
        )
    return _azure_client


def _get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is None:
        import anthropic  # lazy : ne pas imposer la dep si provider != anthropic
        _anthropic_client = anthropic.Anthropic(api_key=Config.ANTHROPIC_API_KEY)
    return _anthropic_client


def chat_completion(
    messages: List[Dict[str, Any]],
    temperature: float = 0.3,
    max_tokens: int = 4000,
    json_mode: bool = False,
) -> str:
    """Appel LLM unifié, provider déterminé par Config.LLM_PROVIDER.

    Args:
        messages: Liste de messages OpenAI-style [{"role": ..., "content": ...}].
            Le rôle "system" est extrait et passé en paramètre séparé côté Anthropic.
        temperature: Température de génération (ignorée par les modèles Claude récents).
        max_tokens: Nombre maximal de tokens en sortie.
        json_mode: Si True, demande une réponse JSON stricte.

    Returns:
        Texte brut de la réponse du modèle.
    """
    if Config.LLM_PROVIDER == "anthropic":
        return _call_anthropic(messages, temperature, max_tokens, json_mode)
    if Config.LLM_PROVIDER == "openai":
        return _call_openai_compatible(
            _get_openai_client(), Config.OPENAI_MODEL, messages, temperature, max_tokens, json_mode
        )
    return _call_openai_compatible(
        _get_azure_client(), Config.AZURE_OPENAI_DEPLOYMENT, messages, temperature, max_tokens, json_mode
    )


# ---------------------------------------------------------------------------
# Implémentations internes
# ---------------------------------------------------------------------------

def _call_openai_compatible(
    client,
    model: str,
    messages: List[Dict[str, Any]],
    temperature: float,
    max_tokens: int,
    json_mode: bool,
) -> str:
    """Commun à OpenAI et Azure OpenAI : même SDK, même forme d'appel."""
    kwargs: Dict[str, Any] = dict(
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    response = client.chat.completions.create(**kwargs)
    return response.choices[0].message.content


def _call_anthropic(
    messages: List[Dict[str, Any]],
    temperature: float,
    max_tokens: int,
    json_mode: bool,
) -> str:
    """Appelle l'API Anthropic Messages.

    Le message "system" OpenAI-style est transformé en paramètre ``system``
    d'Anthropic. Les messages non-system sont transmis tels quels.
    """
    client = _get_anthropic_client()

    system_parts: List[str] = []
    non_system: List[Dict[str, Any]] = []
    for m in messages:
        if m.get("role") == "system":
            system_parts.append(m["content"])
        else:
            non_system.append(m)

    system_text = "\n\n".join(system_parts)
    if json_mode:
        json_instruction = "Réponds UNIQUEMENT en JSON valide, sans texte autour."
        system_text = f"{system_text}\n\n{json_instruction}".strip() if system_text else json_instruction

    # Anthropic exige au moins un message non-system
    if not non_system:
        non_system = [{"role": "user", "content": "Traite la demande."}]

    # NB : `temperature` n'est PAS envoyé. Les modèles Claude récents (Sonnet 5,
    # Opus 4.7+, Fable 5) le rejettent en 400 ; l'omettre est valide sur tous
    # les modèles Claude.
    kwargs: Dict[str, Any] = dict(
        model=Config.ANTHROPIC_MODEL,
        messages=non_system,
        max_tokens=max_tokens,
    )
    if system_text:
        kwargs["system"] = system_text

    response = client.messages.create(**kwargs)

    return "".join(
        block.text for block in response.content if hasattr(block, "text")
    )
