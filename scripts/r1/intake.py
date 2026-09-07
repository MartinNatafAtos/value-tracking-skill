"""
Helper R1 — résolution de mapping + extraction, utilisé par ``cli.py`` (ou
tout autre point d'entrée) avant de lancer l'analyse.

Utilise UN SEUL R1Resolver (le cache SHA256 couvre les appels répétés sur le
même format de fichier, mais partager l'objet évite la double instantiation
et le double calcul de signature).
"""

import logging
from typing import Any, Dict, List, Optional

import openpyxl

from .config import R1_STATIC_CONFIG
from .parser import (
    _anchor_headers,
    _detect_sheets,
    _extract_periodes,
    decouvrir_coupes,
    extract,
)
from .resolver import R1ConfigError, R1Resolver

logger = logging.getLogger(__name__)

# Chemin du cache par défaut (racine du projet, créé à la première résolution)
_DEFAULT_CACHE = ".r1_mapping_cache.json"


def _merge_overrides(
    mapping: Dict[str, Dict[str, Any]],
    overrides: Optional[Dict[str, Dict[str, Any]]],
    available_columns: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, Dict[str, Any]]:
    """
    Fusionne des overrides manuels par-dessus le mapping résolu automatiquement.

    Un override a la forme ``{"<champ_canonique>": {"sheet": "<sheet_key>",
    "col_index": int}, ...}``. Chaque override est validé défensivement contre
    ``available_columns`` (onglet existant + col_index dans les bornes de
    l'entête de cet onglet) : un override invalide est IGNORÉ et loggué en
    warning, il ne fait jamais planter le traitement. Un override valide
    écrase ``{sheet, col_index}`` du champ correspondant dans le mapping.

    Args:
        mapping: Mapping résolu par `R1Resolver.resolve()`.
        overrides: Overrides, ou None/vide (aucun changement).
        available_columns: Colonnes disponibles par onglet (voir
            `R1Resolver.get_available_columns`), utilisées comme référence de
            validation (bornes de col_index, existence de l'onglet).

    Returns:
        Le mapping fusionné (nouvelle dict — ne mute pas `mapping`).
    """
    if not overrides:
        return mapping

    merged = dict(mapping)
    for field_name, loc in overrides.items():
        if not isinstance(loc, dict):
            logger.warning(
                "Override ignoré pour '%s' : forme invalide (%r), attendu {sheet, col_index}",
                field_name, loc,
            )
            continue

        sheet_key = loc.get("sheet")
        col_index = loc.get("col_index")

        columns = available_columns.get(sheet_key)
        if not columns:
            logger.warning(
                "Override ignoré pour '%s' : onglet '%s' inexistant ou non disponible",
                field_name, sheet_key,
            )
            continue

        if not isinstance(col_index, int) or isinstance(col_index, bool) or col_index < 0 or col_index >= len(columns):
            logger.warning(
                "Override ignoré pour '%s' : col_index=%r invalide pour l'onglet '%s' (%d colonnes disponibles)",
                field_name, col_index, sheet_key, len(columns),
            )
            continue

        merged[field_name] = {"sheet": sheet_key, "col_index": col_index}
        logger.info(
            "Override appliqué : %s -> sheet=%s col_index=%d", field_name, sheet_key, col_index
        )

    return merged


def process_r1_workbook(
    wb: openpyxl.Workbook,
    cut: Optional[str] = None,
    openai_client=None,
    cache_path: str = _DEFAULT_CACHE,
    config: Optional[Dict] = None,
    overrides: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Traite un workbook R1 ouvert et retourne les données de confirmation.

    Résout le mapping, expose les colonnes disponibles par onglet, applique
    les éventuels overrides, découvre les coupes disponibles, sélectionne la
    coupe cible, puis obtient des échantillons de valeurs RÉELLES à cette
    coupe (pour la confirmation) et extrait les ValueTrackingInputs — tout en
    n'utilisant qu'un seul R1Resolver.

    Args:
        wb: Workbook openpyxl chargé avec data_only=True.
        cut: Coupe cible au format 'YYYY.MM', ou None → dernière coupe découverte.
        openai_client: Client OpenAI-compatible pour le fallback (optionnel ;
                       None toléré — le resolver utilise alors les fallbacks de
                       position et le cache SHA256 si disponible).
        cache_path: Chemin du fichier de cache JSON du mapping.
        config: Config R1 (dict R1_STATIC_CONFIG) ou None → défaut utilisé.
        overrides: Corrections du mapping résolu, appliquées APRÈS
            `resolver.resolve(wb)` et AVANT les samples/l'extraction, forme
            ``{"<champ_canonique>": {"sheet": "<sheet_key>", "col_index": int}}``.
            Un override dont le sheet/col_index est invalide est ignoré
            (warning), sans jamais planter. None/vide → aucun changement.

    Returns:
        Dict contenant :
        - "mapping"            : {champ_canonique: {"sheet": str, "col_index": int}, ...}
                                  (après fusion des overrides, le cas échéant)
        - "available_columns"  : {sheet_key: [{"col_index", "header", "samples"}, ...]}
        - "samples"            : {champ_canonique: [v1, v2, v3], ...}  (valeurs brutes)
        - "coupes"              : ["2015.12", ..., "2025.12"]
        - "cut"                 : "2025.12"
        - "inputs"              : ValueTrackingInputs

    Raises:
        ValueError: Si un onglet PII est détecté (garde PII du resolver).
        R1ConfigError: Si un champ obligatoire est manquant (avant ou après
                       fusion des overrides), si aucune coupe n'est disponible,
                       ou si la coupe demandée est absente.
    """
    if config is None:
        config = R1_STATIC_CONFIG

    # --- 1. Résoudre le mapping (1 seul resolver) ---
    resolver = R1Resolver(
        config=config,
        openai_client=openai_client,
        cache_path=str(cache_path),
    )
    mapping = resolver.resolve(wb)
    logger.info("Mapping R1 résolu : %d champs", len(mapping))

    # --- 1bis. Colonnes disponibles par onglet (pour une éventuelle correction) ---
    available_columns = resolver.get_available_columns(wb, n=3)

    # --- 1ter. Fusionner les overrides validés (facultatif) ---
    if overrides:
        mapping = _merge_overrides(mapping, overrides, available_columns)
        # Fail-loud inchangé : si un champ obligatoire finit non résolu après
        # fusion (les overrides invalides étant ignorés, cela ne devrait
        # arriver que si un override valide a « poussé dehors » un champ —
        # ce qui n'est structurellement pas possible ici, mais on revalide
        # par sécurité).
        resolver._validate_required(mapping)

    # --- 2. Découvrir les coupes disponibles ---
    sheet_names = _detect_sheets(wb, config)
    header_rows = _anchor_headers(wb, sheet_names, config)
    periodes = _extract_periodes(wb, sheet_names, header_rows, mapping)
    cadence = config.get("cadence", "annuelle")
    coupes = decouvrir_coupes(periodes, cadence)

    logger.info("Coupes découvertes (cadence=%s) : %s", cadence, coupes)

    # --- 3. Sélectionner la coupe cible ---
    if cut is not None:
        if cut not in coupes:
            raise R1ConfigError(
                f"Coupe '{cut}' introuvable dans ce fichier. "
                f"Coupes disponibles : {coupes}"
            )
        selected_cut = cut
    else:
        if not coupes:
            raise R1ConfigError("Aucune coupe disponible dans ce fichier.")
        selected_cut = coupes[-1]
        logger.info("Coupe sélectionnée par défaut : %s", selected_cut)

    # --- 4. Échantillons pour la confirmation (valeurs réelles à la coupe) ---
    # IMPORTANT : appelé APRÈS la sélection de la coupe, avec cut=selected_cut,
    # pour que les champs de l'onglet To Date reflètent la ligne réellement lue
    # à l'extraction (et non les 3 premières lignes du classeur).
    samples = resolver.get_sample_values(wb, mapping, n=3, cut=selected_cut)

    periode_sample = samples.get("to_date_period") or []
    realise_sample = samples.get("to_date_rev_cumul") or []
    poc_sample = samples.get("to_date_poc") or []
    logger.info(
        "Confirmation coupe=%s → ligne To Date=%s, réalisé=%s, poc=%s",
        selected_cut,
        periode_sample[0] if periode_sample else None,
        realise_sample[0] if realise_sample else None,
        poc_sample[0] if poc_sample else None,
    )

    # --- 5. Extraire les données à la coupe sélectionnée ---
    inputs = extract(wb, selected_cut, mapping, config)

    logger.info(
        "Extraction R1 terminée : coupe=%s, %d KPI, %d coûts",
        selected_cut,
        len(inputs.kpi_valeur),
        len(inputs.couts),
    )

    return {
        "mapping": mapping,
        "available_columns": available_columns,
        "samples": samples,
        "coupes": coupes,
        "cut": selected_cut,
        "inputs": inputs,
        # Onglets PII détectés et exclus (jamais lus) — pour information.
        "pii_sheets_ignores": list(resolver.pii_sheets_ignores),
    }
