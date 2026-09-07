"""Point de passage Presidio unique du skill.

Singleton paresseux autour de ``anonymizer_presidio.TextAnonymizer``.
L'initialisation (chargement du modèle spaCy) est coûteuse — elle n'a lieu
qu'au premier appel à anonymize().

GARDE ANTI-CORRUPTION :
    N'appeler anonymize() QUE sur du texte qualitatif (extraits de documents,
    mémoire projet). Ne JAMAIS passer des données numériques par cette
    fonction — un montant pourrait être reconnu comme numéro de téléphone et
    masqué, corrompant les chiffres du livrable.

    Flux TEXTE   : filtre déterministe → anonymize() → LLM
    Flux CHIFFRES: parseur R1 / inputs → chiffres (jamais par anonymize ni LLM)
"""

from __future__ import annotations

import logging
import re

from .config import Config

logger = logging.getLogger(__name__)

# Singleton — initialisé au premier appel
_anonymizer_instance = None


def _get_anonymizer():
    global _anonymizer_instance
    if _anonymizer_instance is None:
        from .anonymizer_presidio import TextAnonymizer  # lazy : init spaCy lourd
        entities = [e.strip() for e in Config.PII_ENTITIES.split(",") if e.strip()]
        client_names = [c.strip() for c in Config.CLIENT_NAMES.split(",") if c.strip()]
        logger.info(
            "Initialisation de l'anonymiseur Presidio (entités : %s | clients : %s)",
            entities, client_names or "—",
        )
        _anonymizer_instance = TextAnonymizer(pii_entities=entities, client_names=client_names)
    return _anonymizer_instance


def mask_client_names(text: str, client_names: list[str] | None) -> str:
    """Masque de façon déterministe des noms de client saisis à la volée.

    Remplacement littéral (insensible à la casse, bornes de mots) → ``<CLIENT>``.
    Sert pour un nom fourni PAR REQUÊTE (ex. saisi dans l'appel), sans avoir à
    reconfigurer le singleton Presidio. Le nom lui-même ne sert qu'ici — il
    n'entre jamais dans le prompt de l'agent.
    """
    if not text or not client_names:
        return text
    for name in client_names:
        name = (name or "").strip()
        if not name:
            continue
        text = re.sub(rf"\b{re.escape(name)}\b", "<CLIENT>", text, flags=re.IGNORECASE)
    return text


def anonymize(text: str, client_names: list[str] | None = None) -> str:
    """Anonymise un texte qualitatif via Presidio.

    Ordre imposé : filtre déterministe → anonymize() → LLM.
    Ne pas appeler sur les chiffres (kpi_valeur, couts, donnees_op).

    Args:
        text: Texte qualitatif à anonymiser.
        client_names: Noms de client à masquer en plus (fournis par requête),
            masqués de façon déterministe (→ <CLIENT>).

    Returns:
        Texte avec PII remplacées par des marqueurs (ex. <PERSON>, <EMAIL_ADDRESS>,
        <CLIENT>).

    Comportement FAIL-SECURE : si Presidio échoue (spaCy absent, modèle non
    téléchargé, exception interne), on ne renvoie JAMAIS le texte brut — il
    partirait non masqué vers l'API publique du LLM. On renvoie un marqueur
    de rédaction et on logge une erreur.
    """
    if not text or not text.strip():
        return text
    try:
        masked = _get_anonymizer().anonymize(text)
    except Exception as exc:
        logger.error(
            "Presidio indisponible (%s) — extrait MASQUÉ par sécurité "
            "(fail-secure : pas de texte brut vers le LLM).", exc,
        )
        return "[EXTRAIT MASQUÉ — anonymisation Presidio indisponible]"
    return mask_client_names(masked, client_names)
