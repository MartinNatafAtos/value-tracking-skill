"""Configuration du skill Value Tracking via variables d'environnement.

Lit directement les variables d'environnement du processus (export/set,
secrets d'un environnement d'agent, etc.) — aucun fichier de config requis.
Aucune clé LLM ici : ce skill ne fait aucun appel LLM en interne, voir
scripts/agent.py (prepare/record).
"""

import os


class Config:
    """Configuration centralisée."""

    # --- Stores locaux ---
    LOCAL_DOCS_DIR = os.getenv("LOCAL_DOCS_DIR", "./local-docs")
    HISTORY_JSON_PATH = os.getenv("HISTORY_JSON_PATH", "./local-history.json")
    MEMORY_DIR = os.getenv("MEMORY_DIR", "./local-memory")
    R1_MAPPING_CACHE = os.getenv("R1_MAPPING_CACHE", "./.r1_mapping_cache.json")
    # État intermédiaire entre agent.prepare() et agent.record().
    STATE_DIR = os.getenv("STATE_DIR", "./.value-tracking-state")

    # --- Anonymisation ---
    PII_ENTITIES = os.getenv("PII_ENTITIES", "PERSON,EMAIL_ADDRESS,PHONE_NUMBER,IBAN")
    CLIENT_NAMES = os.getenv("CLIENT_NAMES", "")
