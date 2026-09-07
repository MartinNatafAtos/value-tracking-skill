"""Store d'historique local (fichier JSON) — capitalisation des analyses.

Clé d'idempotence : sha256(project + cut) si cut fourni,
                    sha256(project + date_analyse) sinon.
Pour une démo « plusieurs coupes » : une entrée par coupe, stable d'un run à
l'autre.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .config import Config

logger = logging.getLogger(__name__)

# Verrou d'écriture (mono-process) pour l'historique JSON.
_SAVE_LOCK = threading.Lock()


class LocalHistoryStore:
    """Reader + Writer d'historique sur fichier JSON local.

    Exemple :
        store = LocalHistoryStore()
        doc_id = store.upsert_analyse(project="alpha", ..., cut="2025.12")
        records = store.get_historique("alpha", top_k=3)
        store.update_reco_statut(doc_id, "fait")
    """

    def __init__(self, path: Optional[str] = None):
        self._path = path or Config.HISTORY_JSON_PATH

    # ------------------------------------------------------------------
    # Lecture
    # ------------------------------------------------------------------

    def get_historique(self, project: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """Retourne les analyses passées d'un projet, triées par date décroissante.

        Args:
            project: Identifiant du projet.
            top_k: Nombre maximum de résultats.

        Returns:
            Liste de dicts (id, project, date_analyse, synthese, ecarts, …).
        """
        records = self._load()
        matching = [r for r in records if r.get("project") == project]
        matching.sort(key=lambda r: r.get("date_analyse", ""), reverse=True)

        result = []
        for r in matching[:top_k]:
            result.append({
                "id": r.get("id"),
                "project": r.get("project"),
                "date_analyse": r.get("date_analyse"),
                "synthese": r.get("synthese", ""),
                "ecarts": _parse_json(r.get("ecarts", "[]")),
                "root_causes": _parse_json(r.get("root_causes", "[]")),
                "recommandations": _parse_json(r.get("recommandations", "[]")),
                "statut_reco": r.get("statut_reco", "à suivre"),
                "sources": _parse_json(r.get("sources", "[]")),
            })
        return result

    # ------------------------------------------------------------------
    # Écriture
    # ------------------------------------------------------------------

    def upsert_analyse(
        self,
        project: str,
        synthese: str,
        ecarts: List[str],
        root_causes: List[str],
        recommandations: List[Dict[str, str]],
        sources: List[Dict[str, Any]],
        statut_reco: str = "à suivre",
        date_analyse: Optional[str] = None,
        cut: Optional[str] = None,
    ) -> str:
        """Insère ou met à jour une analyse dans le fichier JSON.

        Clé : sha256(project + cut) si cut fourni, sha256(project + date_analyse) sinon.
        Si un record avec le même id existe, il est remplacé (idempotence).

        Args:
            project: Identifiant du projet.
            synthese: Texte de synthèse (généré par le LLM).
            ecarts: Leviers identifiés par analyse_8020.
            root_causes: Causes racines narratives.
            recommandations: Liste de dicts Action.
            sources: Sources issues de collect_extracts.
            statut_reco: "à suivre" (défaut) ou "fait".
            date_analyse: ISO-8601 complet ; si None → maintenant UTC.
            cut: Coupe de référence (YYYY.MM). Utilisé pour la clé si fourni.

        Returns:
            L'id (sha256) du document.
        """
        if date_analyse is None:
            date_analyse = datetime.now(timezone.utc).isoformat()

        key_suffix = cut if cut else date_analyse
        doc_id = _make_id(project, key_suffix)

        record = {
            "id": doc_id,
            "project": project,
            "date_analyse": date_analyse,
            "cut": cut,
            "synthese": synthese,
            "ecarts": json.dumps(ecarts, ensure_ascii=False),
            "root_causes": json.dumps(root_causes, ensure_ascii=False),
            "recommandations": json.dumps(recommandations, ensure_ascii=False),
            "statut_reco": statut_reco,
            "sources": json.dumps(sources, ensure_ascii=False),
        }

        records = self._load()
        existing_idx = next(
            (i for i, r in enumerate(records) if r.get("id") == doc_id), None
        )
        if existing_idx is not None:
            records[existing_idx] = record
        else:
            records.append(record)

        self._save(records)
        logger.info("LocalHistoryStore: upsert id=%s (project=%s, cut=%s).", doc_id[:12], project, cut)
        return doc_id

    def update_reco_statut(self, doc_id: str, statut: str) -> None:
        """Met à jour le statut des recommandations d'une analyse.

        Args:
            doc_id: Id du document (retourné par upsert_analyse).
            statut: "fait" ou "à suivre".
        """
        records = self._load()
        for r in records:
            if r.get("id") == doc_id:
                r["statut_reco"] = statut
                self._save(records)
                logger.info("LocalHistoryStore: statut %s → '%s'.", doc_id[:12], statut)
                return
        logger.warning("LocalHistoryStore: update_reco_statut — id '%s' introuvable.", doc_id)

    # ------------------------------------------------------------------
    # Persistance JSON robuste
    # ------------------------------------------------------------------

    def _load(self) -> List[Dict[str, Any]]:
        """Charge le fichier JSON. Retourne [] si absent ou corrompu."""
        if not os.path.exists(self._path):
            return []
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
            logger.warning(
                "LocalHistoryStore: format inattendu dans '%s' (attendu list). Reset.",
                self._path,
            )
            return []
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "LocalHistoryStore: erreur lecture '%s': %s. Reset.", self._path, exc
            )
            return []

    def _save(self, records: List[Dict[str, Any]]) -> None:
        """Persiste la liste de records dans le fichier JSON.

        Écriture ATOMIQUE (fichier temporaire + os.replace) : un crash en cours
        d'écriture ne laisse jamais un JSON tronqué qui ferait perdre tout
        l'historique. Verrou pour les écritures concurrentes en mono-process.
        """
        parent = os.path.dirname(os.path.abspath(self._path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with _SAVE_LOCK:
            tmp_path = f"{self._path}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(records, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self._path)


# ---------------------------------------------------------------------------
# Fonctions utilitaires module-level
# ---------------------------------------------------------------------------

def _make_id(project: str, suffix: str) -> str:
    raw = f"{project}:{suffix}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _parse_json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return value
    return value
