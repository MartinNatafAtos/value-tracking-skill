"""Collecte d'extraits documentaires locaux.

Filtre DÉTERMINISTE en Python — jamais un document entier envoyé au LLM.

Schéma de retour compatible avec agent.build_sources et history_store :
    {
        "file":    str,   # chemin du fichier (str)
        "page":    int,   # numéro de page (1 si non applicable)
        "content": str,   # extrait filtré
        "title":   str,   # nom du fichier
        "url":     None,  # pas d'URL en local
        "score":   1.0,   # score fixe (filtre déterministe, pas sémantique)
    }

Plafonds (éditables) :
    MAX_EXTRACTS = 40   extraits max retournés
    MAX_CHARS    = 60 000 caractères max totaux
Ces plafonds évitent d'envoyer un volume trop important au LLM.
"""

from __future__ import annotations

import csv
import logging
import os
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Mots-clés d'intérêt — liste éditable
# ---------------------------------------------------------------------------
# Mots-clés d'INTÉRÊT (pas seulement les écarts) : on veut aussi capter le
# contenu d'un business case (valeur ambitionnée, bénéfices, ROI) et des données
# d'usage/adoption, pour une analyse détaillée — pas seulement les problèmes.
KEYWORDS_ECART: List[str] = [
    # Écarts / problèmes
    "retard", "retards", "anomalie", "anomalies", "risque", "risques",
    "blocage", "blocages", "validation", "périmètre", "qualité",
    "défaut", "défauts", "incident", "incidents", "dérive", "dérives",
    "alerte", "alertes", "problème", "problèmes", "issue", "issues",
    "écart", "écarts", "impact", "impacts", "urgent", "critique", "critiques",
    "delay", "risk", "blocker", "bloquant",
    # Valeur / bénéfices / business case
    "bénéfice", "bénéfices", "valeur", "objectif", "objectifs", "cible", "cibles",
    "ambition", "gain", "gains", "roi", "rentabilité", "business case",
    "cas de valeur", "enjeu", "enjeux", "kpi", "indicateur", "indicateurs",
    "économie", "économies", "productivité",
    # Adoption / usage
    "adoption", "usage", "utilisation", "utilisateur", "utilisateurs",
    "engagement", "satisfaction", "formation", "conduite du changement",
    "taux", "fréquentation", "actifs",
    # Delivery / exécution
    "livraison", "livrable", "livrables", "jalon", "jalons", "planning",
    "échéance", "recette", "déploiement", "mise en production", "production",
    "copil", "comité",
]

# Plafonds — ajuster ici si besoin
MAX_EXTRACTS = 40       # nombre max d'extraits retournés (business case + usage + COPIL)
MAX_CHARS = 60_000      # taille totale max du texte retourné (tous extraits)
_ROW_MAX_CHARS = 3000   # taille max d'une ligne tabulaire (RAID à colonnes nombreuses)
_SKIP_SHEET_NAMES = {"styles"}  # onglets techniques ignorés


# ---------------------------------------------------------------------------
# Point d'entrée public
# ---------------------------------------------------------------------------

def collect_extracts(project: str, docs_dir: str) -> List[Dict[str, Any]]:
    """Collecte les extraits pertinents depuis le dossier local DU PROJET.

    **Isolation stricte par projet** : on ne lit QUE `docs_dir/<project>/`.
    Si ce sous-dossier n'existe pas → aucun extrait (on ne retombe JAMAIS sur la
    racine, sinon on mélangerait les documents de plusieurs projets).

    Filtre déterministe par type :
      - .txt / .md   : paragraphes contenant des mots-clés d'intérêt.
      - .xlsx / .csv : lignes de données (RAID, usage) en 'colonne: valeur'.
      - .pdf         : pages filtrées par mots-clés (via pdf_extract).

    La taille totale est plafonnée à MAX_EXTRACTS extraits / MAX_CHARS caractères.

    Args:
        project: Identifiant du projet (= nom du sous-dossier obligatoire).
        docs_dir: Dossier racine des documents locaux (Config.LOCAL_DOCS_DIR).

    Returns:
        Liste de dicts compatibles agent.build_sources (vide si pas de dossier projet).
    """
    base = Path(docs_dir)
    if not base.exists():
        logger.warning("LOCAL_DOCS_DIR '%s' n'existe pas — aucun extrait.", docs_dir)
        return []

    # Nom de projet sain, cohérent avec le endpoint d'upload (os.path.basename).
    safe_project = os.path.basename((project or "").strip())
    if not safe_project:
        logger.warning("Aucun projet fourni — aucun extrait (isolation stricte).")
        return []

    target = base / safe_project
    if not target.is_dir():
        # Isolation stricte : pas de dossier projet → 0 extrait (jamais la racine).
        logger.info(
            "Aucun dossier de documents pour le projet '%s' (%s) — 0 extrait.",
            safe_project, target,
        )
        return []
    logger.info("Collecte locale depuis '%s' (projet=%s)", target, safe_project)

    extracts: List[Dict[str, Any]] = []
    total_chars = 0

    for path in sorted(target.rglob("*")):
        if not path.is_file():
            continue
        if len(extracts) >= MAX_EXTRACTS or total_chars >= MAX_CHARS:
            logger.info(
                "Plafond atteint (extraits=%d / %d, chars=%d / %d) — arrêt collecte.",
                len(extracts), MAX_EXTRACTS, total_chars, MAX_CHARS,
            )
            break

        ext = path.suffix.lower()
        try:
            if ext in (".txt", ".md"):
                new = _extract_text_file(path)
            elif ext == ".xlsx":
                new = _extract_xlsx(path)
            elif ext == ".csv":
                new = _extract_csv(path)
            elif ext == ".pdf":
                new = _extract_pdf(path)
            else:
                continue
        except Exception as exc:
            logger.warning("Erreur lecture '%s' : %s — ignoré.", path, exc)
            continue

        for item in new:
            if len(extracts) >= MAX_EXTRACTS or total_chars >= MAX_CHARS:
                break
            extracts.append(item)
            total_chars += len(item.get("content", ""))

    logger.info(
        "Collecte locale terminée : %d extraits, %d caractères.", len(extracts), total_chars
    )
    return extracts


# ---------------------------------------------------------------------------
# Helpers internes
# ---------------------------------------------------------------------------

def _keyword_match(text: str) -> bool:
    """True si le texte contient au moins un mot-clé d'écart."""
    t = text.lower()
    return any(kw in t for kw in KEYWORDS_ECART)


def _make_extract(path: Path, page: int, content: str) -> Dict[str, Any]:
    return {
        "file": str(path),
        "page": page,
        "content": content.strip(),
        "title": path.name,
        "url": None,
        "score": 1.0,
    }


def _extract_text_file(path: Path) -> List[Dict[str, Any]]:
    """Paragraphes (.txt / .md) contenant des mots-clés d'écart."""
    text = path.read_text(encoding="utf-8", errors="replace")
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    return [_make_extract(path, 1, p) for p in paragraphs if _keyword_match(p)]


def _best_header_index(rows: list, scan: int = 20) -> tuple:
    """Ligne d'en-tête = la ligne la PLUS remplie parmi les premières.

    Robuste aux rapports réels (lignes de titre / logo / date au-dessus du vrai
    en-tête). Retourne (index, nb_cellules_non_vides).
    """
    best_i, best_n = 0, -1
    for i, r in enumerate(rows[:scan]):
        n = sum(1 for c in r if c not in (None, ""))
        if n > best_n:
            best_n, best_i = n, i
    return best_i, best_n


def _rows_to_extracts(path: Path, sheet_label: str, rows: list) -> List[Dict[str, Any]]:
    """Convertit des lignes tabulaires en extraits texte 'colonne: valeur'.

    Toutes les lignes de données non vides sont incluses (le LLM fait le tri) —
    on ne filtre PLUS sur une colonne statut fragile (source de 0 extrait sur les
    rapports réels). Réduction : lignes vides ignorées, taille par ligne plafonnée.
    """
    if not rows:
        return []
    header_idx, header_n = _best_header_index(rows)
    if header_n < 2:  # pas de vraie ligne d'en-tête → onglet non tabulaire
        return []
    header = rows[header_idx]
    out: List[Dict[str, Any]] = []
    for r in rows[header_idx + 1:]:
        pairs = []
        for i in range(min(len(header), len(r))):
            h, v = header[i], r[i]
            if h in (None, "") or v in (None, ""):
                continue
            pairs.append(f"{str(h).strip()}: {str(v).strip()}")
        if not pairs:
            continue
        content = " | ".join(pairs)
        if len(content) > _ROW_MAX_CHARS:
            content = content[:_ROW_MAX_CHARS] + "…"
        out.append(_make_extract(path, 1, f"[{sheet_label}] {content}"))
    return out


def _extract_csv(path: Path) -> List[Dict[str, Any]]:
    """Toutes les lignes de données d'un CSV (RAID, usage…) en 'colonne: valeur'."""
    try:
        with path.open(encoding="utf-8", errors="replace", newline="") as f:
            rows = [tuple(r) for r in csv.reader(f)]
    except Exception as exc:
        logger.warning("CSV lecture '%s' : %s", path, exc)
        return []
    return _rows_to_extracts(path, path.stem, rows)


def _extract_xlsx(path: Path) -> List[Dict[str, Any]]:
    """Toutes les lignes de données de chaque onglet d'un .xlsx (RAID, usage…).

    Détection d'en-tête robuste (ligne la plus remplie), onglets techniques
    (styles) et vides ignorés. read_only NON utilisé (il tronque les lignes).
    """
    try:
        import openpyxl
    except ImportError:
        logger.warning("openpyxl non disponible — skip '%s'.", path)
        return []

    result: List[Dict[str, Any]] = []
    wb = openpyxl.load_workbook(str(path), data_only=True)
    try:
        for ws in wb.worksheets:
            if ws.title.strip().lower() in _SKIP_SHEET_NAMES:
                continue
            rows = list(ws.iter_rows(values_only=True))
            result.extend(_rows_to_extracts(path, ws.title, rows))
    finally:
        wb.close()
    return result


def _extract_pdf(path: Path) -> List[Dict[str, Any]]:
    """Pages PDF contenant des mots-clés d'écart (via pdf_extract)."""
    from .pdf_extract import extract_text_from_pdf

    result: List[Dict[str, Any]] = []
    with open(str(path), "rb") as f:
        pages = extract_text_from_pdf(f, path.name)
    for page in pages:
        if _keyword_match(page.text):
            result.append(_make_extract(path, page.page_number, page.text))
    return result
