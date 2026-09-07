"""Mémoire de projet en fichiers .MD (lisible / éditable).

Complète l'historique JSON déterministe : après chaque analyse, l'agent écrit un
récap .MD dans ``MEMORY_DIR/<projet>/``. Les analyses suivantes RELISENT ces .MD
(auto + notes manuelles) et les injectent dans le contexte du LLM, pour que la
réponse tienne compte de l'évolution du projet (ex. 2024 puis 2025).

- Isolation stricte par projet : on ne lit QUE ``MEMORY_DIR/<projet>/``.
- Idempotence : un récap est nommé par la coupe (``2024.12.md``) → réécrit au même
  nom si on relance la même coupe (pas de doublon).
- On peut ouvrir / éditer / ajouter des .MD dans ce dossier : ils sont relus.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from .config import Config

logger = logging.getLogger(__name__)

_MAX_FILES = 20        # nb max de fichiers mémoire relus
_MAX_CHARS = 30_000    # taille totale max de mémoire injectée


def _safe(name: str) -> str:
    return os.path.basename((name or "").strip())


def _project_dir(project: str) -> Path:
    return Path(Config.MEMORY_DIR) / _safe(project)


def read_memory(project: str) -> List[Dict[str, Any]]:
    """Lit les .MD mémoire du projet → extraits (même schéma que collect_extracts).

    Triés par nom (donc chronologiques si nommés par coupe). Plafonnés.
    Retourne [] si pas de dossier (isolation stricte, jamais toute la racine).
    """
    safe = _safe(project)
    if not safe:
        return []
    d = _project_dir(safe)
    if not d.is_dir():
        return []
    extracts: List[Dict[str, Any]] = []
    total = 0
    for path in sorted(d.glob("*.md")):
        if len(extracts) >= _MAX_FILES or total >= _MAX_CHARS:
            break
        try:
            content = path.read_text(encoding="utf-8", errors="replace").strip()
        except Exception as exc:
            logger.warning("Lecture mémoire '%s' impossible : %s", path, exc)
            continue
        if not content:
            continue
        extracts.append({
            "file": f"mémoire/{path.name}",
            "page": 1,
            "content": content,
            "title": path.stem,
            "url": None,
            "score": 1.0,
        })
        total += len(content)
    return extracts


def write_recap(
    project: str,
    cut: str,
    mode: str,
    synthese: str,
    ecarts: List[str],
    root_causes: List[str],
    recommandations: List[Dict[str, str]],
) -> str:
    """Écrit un récap .MD lisible de l'analyse dans MEMORY_DIR/<projet>/.

    Nom = ``<coupe>.md`` (idempotent par coupe) ou date si pas de coupe.
    Retourne le chemin écrit (ou "" en cas d'échec — jamais d'exception fatale).
    """
    safe = _safe(project)
    if not safe:
        return ""
    d = _project_dir(safe)
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        logger.warning("Création dossier mémoire '%s' impossible : %s", d, exc)
        return ""

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    name = (_safe(cut) or stamp) + ".md"
    lines = [
        f"# Analyse — coupe {cut or stamp}",
        f"*Projet : {safe} · cas : {mode} · généré le {stamp}*",
        "",
        "## Synthèse",
        synthese or "—",
        "",
        "## Écarts / leviers",
    ]
    lines += [f"- {e}" for e in (ecarts or [])] or ["- —"]
    lines += ["", "## Causes racines"]
    lines += [f"- {c}" for c in (root_causes or [])] or ["- —"]
    lines += ["", "## Recommandations"]
    if recommandations:
        for r in recommandations:
            lines.append(
                f"- **{r.get('qui', '—')}** : {r.get('quoi', '—')} "
                f"(échéance {r.get('quand', '—')}) → {r.get('impact_attendu', '—')}"
            )
    else:
        lines.append("- —")
    lines.append("")

    path = d / name
    try:
        path.write_text("\n".join(lines), encoding="utf-8")
    except Exception as exc:
        logger.warning("Écriture récap mémoire '%s' impossible : %s", path, exc)
        return ""
    logger.info("Mémoire projet mise à jour : %s", path)
    return str(path)


def list_projects_with_memory() -> List[str]:
    """Projets ayant un dossier mémoire (pour une éventuelle liste déroulante)."""
    root = Path(Config.MEMORY_DIR)
    if not root.is_dir():
        return []
    return [p.name for p in root.iterdir() if p.is_dir()]
