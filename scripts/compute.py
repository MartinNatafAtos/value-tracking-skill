"""Calculs déterministes pour le Value Tracking.

Règle d'or : tout chiffre du livrable provient de ce module, jamais du LLM.
Fonctions pures — aucun appel réseau, aucun appel LLM.
"""

from __future__ import annotations
from typing import List, Dict


def compute_variance(kpi_valeur: List[Dict]) -> List[Dict]:
    """Calcule les variances par KPI.

    Args:
        kpi_valeur: Liste de dicts avec clés ``libelle``, ``prevu``, ``realise``,
            et optionnellement ``realise_cumul`` (réalisé cumulé à la coupe,
            ``None`` si indisponible).

    Returns:
        Liste enrichie avec ``ecart_abs`` et ``ecart_pct`` par KPI (calculés sur
        ``realise``/FAC vs ``prevu`` — variance d'atterrissage). ``realise_cumul``
        est repris tel quel (pass-through, ``None`` si absent) et n'entre PAS
        dans le calcul de l'écart.
        Si ``prevu`` == 0, ``ecart_pct`` vaut None.
    """
    result = []
    for kpi in kpi_valeur:
        prevu = float(kpi["prevu"])
        realise = float(kpi["realise"])
        ecart_abs = realise - prevu
        # Dénominateur en valeur absolue : le SIGNE du % reflète l'impact valeur
        # (favorable = +, défavorable = −), cohérent avec la couleur du front.
        # Ex. coûts stockés négatifs : un dépassement (réalisé plus négatif que
        # prévu) → ecart_abs < 0 → ecart_pct < 0 (défavorable / rouge).
        ecart_pct = None if prevu == 0 else round((ecart_abs / abs(prevu)) * 100, 2)
        result.append({
            "libelle": kpi["libelle"],
            "prevu": prevu,
            "realise": realise,
            "realise_cumul": kpi.get("realise_cumul"),
            "ecart_abs": round(ecart_abs, 4),
            "ecart_pct": ecart_pct,
        })
    return result


def analyse_8020(kpi_table: List[Dict]) -> List[str]:
    """Identifie les leviers expliquant ~80 % de l'écart total (valeur absolue).

    Args:
        kpi_table: Sortie de ``compute_variance``.

    Returns:
        Liste de labels des leviers (triés du plus grand écart absolu au plus petit)
        dont la somme cumulée couvre au moins 80 % de l'écart total absolu.
        Si la liste est vide ou si l'écart total est nul, retourne une liste vide.
    """
    if not kpi_table:
        return []

    # Tri décroissant sur |écart absolu|
    sorted_kpis = sorted(kpi_table, key=lambda k: abs(k["ecart_abs"]), reverse=True)

    total_abs = sum(abs(k["ecart_abs"]) for k in sorted_kpis)
    if total_abs == 0:
        return []

    leviers: List[str] = []
    cumul = 0.0
    for kpi in sorted_kpis:
        leviers.append(kpi["libelle"])
        cumul += abs(kpi["ecart_abs"])
        if cumul / total_abs >= 0.80:
            break

    return leviers
