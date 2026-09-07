"""Moteur d'extraction R1 Dashboard — lecture cellules + production ValueTrackingInputs."""

import logging
from typing import List, Optional, Tuple

import openpyxl

from ..models import Cout, ValueKPI, ValueTrackingInputs
from .config import R1_STATIC_CONFIG
from .resolver import R1ConfigError, R1Resolver

logger = logging.getLogger(__name__)


def decouvrir_coupes(periodes: List[str], cadence: str) -> List[str]:
    """
    Retourne les coupes dérivées des périodes disponibles.

    Args:
        periodes: Liste de périodes au format 'YYYY.MM'
        cadence: "annuelle" | "trimestrielle" | "mensuelle"

    Returns:
        Liste des coupes triées chronologiquement

    Raises:
        ValueError: Si cadence invalide
    """
    if cadence not in ["annuelle", "trimestrielle", "mensuelle"]:
        raise ValueError(f"Cadence invalide : {cadence}. Attendu : annuelle, trimestrielle, mensuelle")

    if not periodes:
        return []

    # Trier les périodes chronologiquement
    periodes_sorted = sorted(periodes)

    if cadence == "mensuelle":
        return periodes_sorted

    coupes = []

    if cadence == "annuelle":
        # Dernière période de chaque année
        years_seen = set()
        for p in reversed(periodes_sorted):
            try:
                year = int(p.split('.')[0])
                if year not in years_seen:
                    coupes.append(p)
                    years_seen.add(year)
            except (ValueError, IndexError):
                logger.warning(f"Période invalide ignorée : {p}")
        coupes.reverse()

    elif cadence == "trimestrielle":
        # Dernière période de chaque trimestre (MM in {03,06,09,12})
        quarters_seen = set()
        for p in reversed(periodes_sorted):
            try:
                year, month = p.split('.')
                year_int = int(year)
                month_int = int(month)

                # Mapper le mois au trimestre
                if month_int in [1, 2, 3]:
                    quarter = 1
                    target_month = 3
                elif month_int in [4, 5, 6]:
                    quarter = 2
                    target_month = 6
                elif month_int in [7, 8, 9]:
                    quarter = 3
                    target_month = 9
                else:  # 10, 11, 12
                    quarter = 4
                    target_month = 12

                quarter_key = (year_int, quarter)

                # On ne garde que la dernière période du trimestre
                if month_int == target_month and quarter_key not in quarters_seen:
                    coupes.append(p)
                    quarters_seen.add(quarter_key)
            except (ValueError, IndexError):
                logger.warning(f"Période invalide ignorée : {p}")
        coupes.reverse()

    return coupes


def extract(
    wb: openpyxl.Workbook,
    cut: str,
    mapping: dict,
    config: dict,
) -> ValueTrackingInputs:
    """
    Extrait les données à la coupe 'cut' (ex. '2025.12').

    Args:
        wb: Workbook openpyxl (data_only=True)
        cut: Période cible au format 'YYYY.MM'
        mapping: Mapping résolu {champ: {"sheet": str, "col_index": int}}
        config: R1_STATIC_CONFIG ou équivalent

    Returns:
        ValueTrackingInputs prêt pour compute.py

    Raises:
        R1ConfigError: Si données manquantes ou incohérentes
    """
    # 1. Retrouver les noms d'onglets réels
    sheet_names = _detect_sheets(wb, config)

    # 2. Retrouver les lignes d'entête
    header_rows = _anchor_headers(wb, sheet_names, config)

    # 3. Extraire les KPI depuis Performance
    kpi_valeur = _extract_kpi(
        wb=wb,
        cut=cut,
        sheet_names=sheet_names,
        header_rows=header_rows,
        mapping=mapping,
        config=config
    )

    # 4. Extraire les coûts détaillés depuis Performance
    couts = _extract_couts(
        wb=wb,
        sheet_names=sheet_names,
        header_rows=header_rows,
        mapping=mapping,
        config=config
    )

    # 5. Extraire les données opérationnelles
    donnees_op = _extract_donnees_op(
        wb=wb,
        cut=cut,
        sheet_names=sheet_names,
        header_rows=header_rows,
        mapping=mapping,
        config=config
    )

    return ValueTrackingInputs(
        kpi_valeur=kpi_valeur,
        couts=couts,
        donnees_op=donnees_op
    )


def _detect_sheets(wb, config: dict) -> dict:
    """Détecte les onglets R1 présents (les onglets PII sont exclus, jamais lus)."""
    from .resolver import detect_pii_sheets
    pii = set(detect_pii_sheets(wb.sheetnames, config))
    result = {}
    for key, candidates in config["sheets"].items():
        found = None
        for candidate in candidates:
            for sheet_name in wb.sheetnames:
                if sheet_name in pii:
                    continue
                if _normalize(sheet_name) == _normalize(candidate):
                    found = sheet_name
                    break
            if found:
                break
        result[key] = found
    return result


def _anchor_headers(wb, sheet_names: dict, config: dict) -> dict:
    """Ancre les lignes d'entête."""
    result = {}
    max_scan = config.get("header_scan_rows", 20)

    for sheet_key, ws_name in sheet_names.items():
        if ws_name is None:
            continue
        markers = config["header_markers"].get(sheet_key, [])
        if not markers:
            continue

        ws = wb[ws_name]
        row_idx = _find_header_row(ws, markers, max_scan)
        if row_idx is None:
            raise R1ConfigError(f"Entête introuvable dans '{ws_name}'")
        result[sheet_key] = row_idx

    return result


def _find_header_row(ws, markers: List[str], max_rows: int) -> Optional[int]:
    """Trouve la ligne contenant tous les marqueurs."""
    for row_idx in range(max_rows):
        row = list(ws.iter_rows(min_row=row_idx+1, max_row=row_idx+1, values_only=True))[0]
        row_str = [str(c).strip() if c else "" for c in row]

        if all(any(_normalize(marker) in _normalize(cell) for cell in row_str) for marker in markers):
            return row_idx

    return None


def _normalize(s: str) -> str:
    """Normalise une chaîne."""
    import unicodedata
    s = s.lower().strip()
    s = unicodedata.normalize('NFKD', s).encode('ascii', 'ignore').decode('ascii')
    s = ' '.join(s.split())
    return s


def _extract_kpi(wb, cut: str, sheet_names: dict, header_rows: dict, mapping: dict, config: dict) -> List[ValueKPI]:
    """
    Extrait REVENUE, COSTS, MARGIN depuis Performance.

    prevu = valeur col mapping['prevu'] (budget IBC, figé quelle que soit la coupe)
    realise = valeur col mapping['realise'] (FAC total, figé quelle que soit la coupe)
    realise_cumul = réalisé cumulé À LA COUPE `cut`, lu dans To Date via
        config["kpi_cumul_map"][libelle] + `_read_to_date_field(..., default=None)`.
        None si le libellé KPI n'est pas mappé ou si le champ n'est pas résolu
        (ne fait jamais échouer l'extraction).
    """
    ws_name = sheet_names.get("performance")
    if not ws_name:
        raise R1ConfigError("Onglet Performance manquant")

    ws = wb[ws_name]
    header_row = header_rows.get("performance", 0)

    # Colonnes prévu et réalisé
    prevu_field = config["mapping"]["prevu"]
    realise_field = config["mapping"]["realise"]

    if prevu_field not in mapping or realise_field not in mapping:
        raise R1ConfigError(f"Champs {prevu_field} ou {realise_field} non résolus")

    col_prevu = mapping[prevu_field]["col_index"]
    col_realise = mapping[realise_field]["col_index"]

    # Lignes KPI à extraire
    kpi_rows = config.get("kpi_rows", ["REVENUE", "COSTS", "MARGIN"])

    # Mapping libellé KPI → champ cumulé To Date (réalisé à la coupe)
    kpi_cumul_map = config.get("kpi_cumul_map", {})

    kpi_valeur = []

    # Scanner les lignes après l'entête
    for row_idx in range(header_row + 1, ws.max_row):
        row = list(ws.iter_rows(min_row=row_idx+1, max_row=row_idx+1, values_only=True))[0]
        if not row or len(row) == 0:
            continue

        libelle = str(row[0]).strip() if row[0] else ""

        if libelle in kpi_rows:
            prevu_val = row[col_prevu] if col_prevu < len(row) else None
            realise_val = row[col_realise] if col_realise < len(row) else None

            # Convertir en float
            try:
                prevu = float(prevu_val) if prevu_val is not None else 0.0
            except (ValueError, TypeError):
                prevu = 0.0

            try:
                realise = float(realise_val) if realise_val is not None else 0.0
            except (ValueError, TypeError):
                realise = 0.0

            # Réalisé cumulé à la coupe (To Date) — varie selon `cut`, contrairement
            # à prevu/realise ci-dessus. None si non mappé/résolu (jamais bloquant).
            realise_cumul = None
            cumul_field = kpi_cumul_map.get(libelle)
            if cumul_field:
                realise_cumul = _read_to_date_field(
                    wb, cut, sheet_names, header_rows, mapping, cumul_field, default=None
                )

            kpi_valeur.append(ValueKPI(
                libelle=libelle,
                prevu=prevu,
                realise=realise,
                realise_cumul=realise_cumul
            ))

            logger.info(
                f"KPI extrait : {libelle} | prevu={prevu}, realise={realise}, "
                f"realise_cumul={realise_cumul}"
            )

    if not kpi_valeur:
        raise R1ConfigError(f"Aucun KPI trouvé parmi {kpi_rows} dans Performance")

    return kpi_valeur


def _extract_couts(wb, sheet_names: dict, header_rows: dict, mapping: dict, config: dict) -> List[Cout]:
    """Extrait les coûts détaillés depuis Performance."""
    ws_name = sheet_names.get("performance")
    if not ws_name:
        raise R1ConfigError("Onglet Performance manquant")

    ws = wb[ws_name]
    header_row = header_rows.get("performance", 0)

    # Colonne réalisé (montant)
    realise_field = config["mapping"]["realise"]
    if realise_field not in mapping:
        raise R1ConfigError(f"Champ {realise_field} non résolu")

    col_realise = mapping[realise_field]["col_index"]

    # Lignes de coûts
    cost_rows = config.get("cost_rows", {})

    couts = []

    for row_idx in range(header_row + 1, ws.max_row):
        row = list(ws.iter_rows(min_row=row_idx+1, max_row=row_idx+1, values_only=True))[0]
        if not row:
            continue

        libelle = str(row[0]).strip() if row[0] else ""

        if libelle in cost_rows:
            type_cout = cost_rows[libelle]
            montant_val = row[col_realise] if col_realise < len(row) else None

            try:
                montant = float(montant_val) if montant_val is not None else 0.0
            except (ValueError, TypeError):
                montant = 0.0

            couts.append(Cout(
                type=type_cout,
                libelle=libelle,
                montant=montant
            ))

            logger.info(f"Coût extrait : {libelle} | type={type_cout}, montant={montant}")

    return couts


def _extract_donnees_op(
    wb, cut: str, sheet_names: dict, header_rows: dict, mapping: dict, config: dict
) -> dict:
    """
    Extrait les données opérationnelles.

    Returns:
        {
            "cut": cut,
            "poc": poc à la coupe,
            "couts_cumul": coûts cumulés à la coupe (To Date, NÉGATIFS),
            "marge_cumul": marge cumulée à la coupe (To Date),
            "rev_cumul": CA cumulé à la coupe (To Date),
            "coupes": toutes les coupes disponibles,
            "wbs_pareto": liste des lots WBS avec marge,
            "ecart_earned_value": formule earned value
        }
    """
    # 1. Découvrir toutes les périodes disponibles dans To Date
    periodes = _extract_periodes(wb, sheet_names, header_rows, mapping)

    # 2. Découvrir les coupes selon la cadence
    cadence = config.get("cadence", "annuelle")
    coupes = decouvrir_coupes(periodes, cadence)

    # 3. Lire le PoC à la coupe
    poc = _read_poc(wb, cut, sheet_names, header_rows, mapping)

    # 3bis. Lire les cumuls To Date à la coupe (champs utiles mais non
    # bloquants : absents du mapping → 0.0, ne fait jamais échouer l'extraction).
    # NB signe : dans le R1 réel, to_date_costs_cumul est NÉGATIF (ce sont des
    # coûts, ex. -10621.26) — on expose la valeur brute telle que lue, sans
    # changer le signe.
    couts_cumul = _read_to_date_field(
        wb, cut, sheet_names, header_rows, mapping, "to_date_costs_cumul"
    )
    marge_cumul = _read_to_date_field(
        wb, cut, sheet_names, header_rows, mapping, "to_date_margin_cumul"
    )
    rev_cumul = _read_to_date_field(
        wb, cut, sheet_names, header_rows, mapping, "to_date_rev_cumul"
    )

    # 4. Lire les lots WBS (pour Pareto)
    wbs_pareto = _extract_wbs_pareto(wb, sheet_names, header_rows, mapping, config)

    # 5. Calculer écart earned value (optionnel)
    ecart_ev = _compute_earned_value(wb, cut, sheet_names, header_rows, mapping, config, poc)

    return {
        "cut": cut,
        "poc": poc,
        "couts_cumul": couts_cumul,   # coûts cumulés à la coupe (To Date)
        "marge_cumul": marge_cumul,   # marge cumulée à la coupe (To Date)
        "rev_cumul": rev_cumul,       # CA cumulé à la coupe (To Date)
        "coupes": coupes,
        "wbs_pareto": wbs_pareto,
        "ecart_earned_value": ecart_ev,
    }


def _extract_periodes(wb, sheet_names: dict, header_rows: dict, mapping: dict) -> List[str]:
    """Extrait toutes les périodes disponibles depuis To Date."""
    ws_name = sheet_names.get("to_date")
    if not ws_name:
        raise R1ConfigError("Onglet To Date manquant")

    ws = wb[ws_name]
    header_row = header_rows.get("to_date", 0)

    # La colonne période est en index 0 (position)
    if "to_date_period" not in mapping:
        raise R1ConfigError("Champ to_date_period non résolu")

    col_period = mapping["to_date_period"]["col_index"]

    periodes = []

    # Scanner les lignes après l'entête (ignorer la ligne 'To Date' en totaux si présente)
    for row_idx in range(header_row + 1, ws.max_row):
        row = list(ws.iter_rows(min_row=row_idx+1, max_row=row_idx+1, values_only=True))[0]
        if not row or col_period >= len(row):
            continue

        period_val = row[col_period]
        if not period_val:
            continue

        period_str = str(period_val).strip()

        # Ignorer 'To Date' (ligne de totaux)
        if period_str.lower() == "to date":
            continue

        # Valider le format YYYY.MM
        if '.' in period_str:
            try:
                parts = period_str.split('.')
                int(parts[0])  # année
                int(parts[1])  # mois
                periodes.append(period_str)
            except (ValueError, IndexError):
                pass

    return periodes


def _read_poc(wb, cut: str, sheet_names: dict, header_rows: dict, mapping: dict) -> float:
    """Lit le PoC% à la coupe donnée."""
    ws_name = sheet_names.get("to_date")
    if not ws_name:
        raise R1ConfigError("Onglet To Date manquant")

    ws = wb[ws_name]
    header_row = header_rows.get("to_date", 0)

    if "to_date_period" not in mapping or "to_date_poc" not in mapping:
        raise R1ConfigError("Champs to_date_period ou to_date_poc non résolus")

    col_period = mapping["to_date_period"]["col_index"]
    col_poc = mapping["to_date_poc"]["col_index"]

    # Trouver la ligne correspondant à cut
    for row_idx in range(header_row + 1, ws.max_row):
        row = list(ws.iter_rows(min_row=row_idx+1, max_row=row_idx+1, values_only=True))[0]
        if not row or col_period >= len(row):
            continue

        period_str = str(row[col_period]).strip() if row[col_period] else ""

        if period_str == cut:
            poc_val = row[col_poc] if col_poc < len(row) else None
            try:
                return float(poc_val) if poc_val is not None else 0.0
            except (ValueError, TypeError):
                return 0.0

    raise R1ConfigError(f"Période {cut} introuvable dans To Date")


def _read_to_date_field(
    wb,
    cut: str,
    sheet_names: dict,
    header_rows: dict,
    mapping: dict,
    field_name: str,
    default: Optional[float] = 0.0,
) -> Optional[float]:
    """
    Lit la valeur d'un champ cumulatif de l'onglet To Date, à la coupe donnée.

    Généralise le motif de `_read_poc` : scanne l'onglet To Date, ignore la
    ligne de totaux 'To Date', matche la colonne période == `cut`, lit la
    valeur à la colonne du champ demandé et la convertit en float (défensif :
    `default` si vide/non convertible/IndexError).

    Contrairement à `_read_poc` (champ obligatoire, fail-loud), ce helper est
    utilisé pour des champs "utiles mais non bloquants" (ex. couts_cumul,
    marge_cumul) : si `field_name` n'est pas résolu dans le mapping, ou si
    l'onglet To Date / le champ 'to_date_period' est absent, ou si la coupe
    n'est pas trouvée, on retourne `default` SANS lever d'exception.

    Args:
        wb: Workbook openpyxl (data_only=True)
        cut: Période cible au format 'YYYY.MM'
        sheet_names: {sheet_key: nom réel de l'onglet}
        header_rows: {sheet_key: index 0-based de la ligne d'entête}
        mapping: Mapping résolu {champ: {"sheet": str, "col_index": int}}
        field_name: Champ canonique à lire (ex. "to_date_costs_cumul")
        default: Valeur de repli si le champ/la coupe est introuvable.
            Peut valoir `None` (ex. ValueKPI.realise_cumul) pour distinguer
            explicitement "non disponible" de "zéro" — dans ce cas `None`
            n'est JAMAIS converti en 0.0, il est propagé tel quel.

    Returns:
        La valeur du champ à la coupe, ou `default` (peut être `None`).
    """
    ws_name = sheet_names.get("to_date")
    if not ws_name:
        return default

    if "to_date_period" not in mapping or field_name not in mapping:
        return default

    ws = wb[ws_name]
    header_row = header_rows.get("to_date", 0)

    col_period = mapping["to_date_period"]["col_index"]
    col_field = mapping[field_name]["col_index"]

    for row_idx in range(header_row + 1, ws.max_row):
        try:
            row = list(ws.iter_rows(min_row=row_idx+1, max_row=row_idx+1, values_only=True))[0]
        except IndexError:
            continue
        if not row or col_period >= len(row):
            continue

        period_str = str(row[col_period]).strip() if row[col_period] else ""

        if period_str == cut:
            try:
                field_val = row[col_field] if col_field < len(row) else None
            except IndexError:
                field_val = None
            try:
                return float(field_val) if field_val is not None else default
            except (ValueError, TypeError):
                return default

    return default


def _extract_wbs_pareto(wb, sheet_names: dict, header_rows: dict, mapping: dict, config: dict) -> List[dict]:
    """Extrait les lots WBS avec leur marge (Project To Date)."""
    ws_name = sheet_names.get("wbs")
    if not ws_name:
        logger.warning("Onglet WBS Summary manquant, pareto WBS non disponible")
        return []

    ws = wb[ws_name]
    header_row = header_rows.get("wbs", 0)

    if "wbs_label" not in mapping or "wbs_margin" not in mapping:
        logger.warning("Champs WBS non résolus, pareto WBS non disponible")
        return []

    col_wbs_code = config.get("wbs_code_col", 0)  # Index colonne code WBS (configurable)
    col_wbs_label = mapping["wbs_label"]["col_index"]
    col_wbs_margin = mapping["wbs_margin"]["col_index"]

    lots = []

    for row_idx in range(header_row + 1, ws.max_row):
        row = list(ws.iter_rows(min_row=row_idx+1, max_row=row_idx+1, values_only=True))[0]
        if not row:
            continue

        wbs_code = str(row[col_wbs_code]).strip() if row[col_wbs_code] else ""
        wbs_label = str(row[col_wbs_label]).strip() if col_wbs_label < len(row) and row[col_wbs_label] else ""

        # Ignorer 'Total' et codes WBS avec sous-codes (ex. FR.FTLBIL...)
        # Protection IndexError : vérifier la longueur avant slicing
        if wbs_code.lower() == "total" or (len(wbs_code) > 4 and "." in wbs_code[4:]):
            continue

        # Codes WBS attendus : préfixe configurable (ex. WBS-, LOT-, etc.)
        wbs_prefix = config.get("wbs_code_prefix", "WBS-")
        if not wbs_code.startswith(wbs_prefix):
            continue

        margin_val = row[col_wbs_margin] if col_wbs_margin < len(row) else None
        try:
            margin = float(margin_val) if margin_val is not None else 0.0
        except (ValueError, TypeError):
            margin = 0.0

        lots.append({
            "wbs_code": wbs_code,
            "wbs_label": wbs_label,
            "wbs_margin": margin
        })

    # Trier par marge absolue décroissante (pour le Pareto)
    lots.sort(key=lambda x: abs(x["wbs_margin"]), reverse=True)

    return lots


def _compute_earned_value(
    wb, cut: str, sheet_names: dict, header_rows: dict, mapping: dict, config: dict, poc: float
) -> float:
    """
    Calcule l'écart earned value selon la formule :
    ecart_earned_value = to_date_rev_cumul(cut) - perf_cbc(REVENUE) * poc(cut)
    """
    # 1. Lire to_date_rev_cumul à la coupe
    ws_name = sheet_names.get("to_date")
    if not ws_name:
        return 0.0

    ws = wb[ws_name]
    header_row = header_rows.get("to_date", 0)

    if "to_date_period" not in mapping or "to_date_rev_cumul" not in mapping:
        return 0.0

    col_period = mapping["to_date_period"]["col_index"]
    col_rev_cumul = mapping["to_date_rev_cumul"]["col_index"]

    rev_cumul = None
    for row_idx in range(header_row + 1, ws.max_row):
        row = list(ws.iter_rows(min_row=row_idx+1, max_row=row_idx+1, values_only=True))[0]
        if not row or col_period >= len(row):
            continue

        period_str = str(row[col_period]).strip() if row[col_period] else ""

        if period_str == cut:
            rev_val = row[col_rev_cumul] if col_rev_cumul < len(row) else None
            try:
                rev_cumul = float(rev_val) if rev_val is not None else 0.0
            except (ValueError, TypeError):
                rev_cumul = 0.0
            break

    if rev_cumul is None:
        return 0.0

    # 2. Lire perf_cbc(REVENUE) depuis Performance
    ws_perf_name = sheet_names.get("performance")
    if not ws_perf_name:
        return 0.0

    ws_perf = wb[ws_perf_name]
    header_row_perf = header_rows.get("performance", 0)

    if "perf_cbc" not in mapping:
        return 0.0

    col_cbc = mapping["perf_cbc"]["col_index"]

    perf_cbc_revenue = None
    for row_idx in range(header_row_perf + 1, ws_perf.max_row):
        row = list(ws_perf.iter_rows(min_row=row_idx+1, max_row=row_idx+1, values_only=True))[0]
        if not row:
            continue

        libelle = str(row[0]).strip() if row[0] else ""

        revenue_label = config.get("revenue_label", "REVENUE")
        if libelle == revenue_label:
            cbc_val = row[col_cbc] if col_cbc < len(row) else None
            try:
                perf_cbc_revenue = float(cbc_val) if cbc_val is not None else 0.0
            except (ValueError, TypeError):
                perf_cbc_revenue = 0.0
            break

    if perf_cbc_revenue is None:
        return 0.0

    # 3. Appliquer la formule
    ecart_ev = rev_cumul - (perf_cbc_revenue * poc)

    return ecart_ev


def parse_r1(
    filepath: str,
    config: dict = None,
    cut: str = None,
    openai_client=None,
    cache_path: str = ".r1_mapping_cache.json",
    interactive: bool = False,
) -> Tuple[ValueTrackingInputs, List[str], dict]:
    """
    Point d'entrée principal pour parser un fichier R1 Dashboard.

    Args:
        filepath: Chemin vers le fichier .xlsx
        config: R1_STATIC_CONFIG ou None (utilise la config par défaut)
        cut: Période cible au format 'YYYY.MM', ou None (dernière coupe)
        openai_client: Client OpenAI-compatible pour le fallback (optionnel)
        cache_path: Chemin du cache de mapping
        interactive: Si True, demande confirmation du mapping

    Returns:
        (inputs, coupes, mapping)
        - inputs: ValueTrackingInputs prêt pour compute.py
        - coupes: liste des coupes découvertes
        - mapping: le mapping résolu (pour traçabilité)

    Raises:
        R1ConfigError: Si format invalide ou champs manquants
    """
    if config is None:
        config = R1_STATIC_CONFIG

    # 1. Charger le workbook
    logger.info(f"Chargement de {filepath}...")
    wb = openpyxl.load_workbook(filepath, data_only=True)

    # 2. Résoudre le mapping
    logger.info("Résolution du mapping...")
    resolver = R1Resolver(config, openai_client, cache_path)
    mapping = resolver.resolve(wb)

    # 3. Confirmation (si interactive)
    if interactive:
        samples = resolver.get_sample_values(wb, mapping)
        if not resolver.confirm(mapping, samples, interactive=True):
            raise R1ConfigError("Mapping refusé par l'utilisateur")

    # 4. Découvrir les coupes
    sheet_names = _detect_sheets(wb, config)
    header_rows = _anchor_headers(wb, sheet_names, config)
    periodes = _extract_periodes(wb, sheet_names, header_rows, mapping)
    cadence = config.get("cadence", "annuelle")
    coupes = decouvrir_coupes(periodes, cadence)

    logger.info(f"Coupes découvertes ({cadence}) : {coupes}")

    # 5. Déterminer la coupe cible
    if cut is None:
        if not coupes:
            raise R1ConfigError("Aucune coupe disponible dans le fichier")
        cut = coupes[-1]  # Dernière coupe
        logger.info(f"Coupe par défaut : {cut}")

    if cut not in coupes:
        raise R1ConfigError(f"Coupe {cut} introuvable. Coupes disponibles : {coupes}")

    # 6. Extraire les données
    logger.info(f"Extraction des données à la coupe {cut}...")
    inputs = extract(wb, cut, mapping, config)

    logger.info(f"Extraction terminée : {len(inputs.kpi_valeur)} KPI, {len(inputs.couts)} coûts")

    return inputs, coupes, mapping
