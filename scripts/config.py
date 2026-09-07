"""Configuration du skill Value Tracking via variables d'environnement.

Lit directement les variables d'environnement du processus (export/set,
secrets d'un environnement d'agent, etc.) — aucun fichier de config requis.
"""

import os


class Config:
    """Configuration centralisée."""

    # --- Provider LLM ---
    LLM_PROVIDER = os.getenv("LLM_PROVIDER", "anthropic")  # anthropic | openai | azure

    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
    ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")

    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
    OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1")

    AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "")
    AZURE_OPENAI_API_KEY = os.getenv("AZURE_OPENAI_API_KEY", "")
    AZURE_OPENAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-15-preview")
    AZURE_OPENAI_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4.1")

    # --- Stores locaux ---
    LOCAL_DOCS_DIR = os.getenv("LOCAL_DOCS_DIR", "./local-docs")
    HISTORY_JSON_PATH = os.getenv("HISTORY_JSON_PATH", "./local-history.json")
    MEMORY_DIR = os.getenv("MEMORY_DIR", "./local-memory")
    R1_MAPPING_CACHE = os.getenv("R1_MAPPING_CACHE", "./.r1_mapping_cache.json")

    # --- Anonymisation ---
    PII_ENTITIES = os.getenv("PII_ENTITIES", "PERSON,EMAIL_ADDRESS,PHONE_NUMBER,IBAN")
    CLIENT_NAMES = os.getenv("CLIENT_NAMES", "")

    @classmethod
    def validate(cls) -> None:
        """Vérifie que le provider LLM actif a bien sa clé/config renseignée."""
        missing = []
        if cls.LLM_PROVIDER == "anthropic":
            if not cls.ANTHROPIC_API_KEY:
                missing.append("ANTHROPIC_API_KEY")
        elif cls.LLM_PROVIDER == "openai":
            if not cls.OPENAI_API_KEY:
                missing.append("OPENAI_API_KEY")
        elif cls.LLM_PROVIDER == "azure":
            if not cls.AZURE_OPENAI_ENDPOINT:
                missing.append("AZURE_OPENAI_ENDPOINT")
            if not cls.AZURE_OPENAI_API_KEY:
                missing.append("AZURE_OPENAI_API_KEY")
        else:
            raise ValueError(
                f"LLM_PROVIDER='{cls.LLM_PROVIDER}' invalide (attendu : anthropic | openai | azure)"
            )

        if missing:
            raise ValueError(f"Variables d'environnement manquantes : {', '.join(missing)}")
