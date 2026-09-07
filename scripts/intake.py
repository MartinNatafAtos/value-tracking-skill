"""Manifeste d'intake — résolution déterministe des éléments requis.

Le LLM ne décide jamais ce qui est obligatoire : c'est ce module qui tranche.
Pour ajouter / retirer un élément, éditer uniquement ``MANIFESTE`` ci-dessous.
"""

from typing import List, Tuple

from .models import DocumentRequis


# ---------------------------------------------------------------------------
# Manifeste codé en dur — point unique d'édition
# ---------------------------------------------------------------------------

MANIFESTE: List[DocumentRequis] = [
    DocumentRequis(
        element="KPI valeur (prévu / réalisé)",
        type="chiffré",
        obligatoire=True,
        pourquoi="Indispensable au calcul de variance et au tableau KPI (compute.py)",
        format_attendu="inputs.kpi_valeur : liste de {libelle, prevu, realise}",
    ),
    DocumentRequis(
        element="Coûts CAPEX / OPEX",
        type="chiffré",
        obligatoire=True,
        pourquoi="Indispensable à la visibilité valeur vs coûts",
        format_attendu="inputs.couts : liste de {type, libelle, montant}",
    ),
    DocumentRequis(
        element="Business case / cas de valeur initial",
        type="document",
        obligatoire=False,
        pourquoi="Éclaire l'écart vs ambition initiale, enrichit les root causes",
        format_attendu="Fichier déposé dans local-docs/<project>/",
    ),
    DocumentRequis(
        element="Reporting delivery / COPIL récent",
        type="document",
        obligatoire=False,
        pourquoi="Fournit les root causes retard & qualité",
        format_attendu="Fichier déposé dans local-docs/<project>/",
    ),
    DocumentRequis(
        element="Données d'usage / adoption",
        type="document",
        obligatoire=False,
        pourquoi="Permet des recommandations ciblées sur l'adoption",
        format_attendu="Fichier (.xlsx/.csv/.pdf) déposé dans local-docs/<project>/",
    ),
]


# ---------------------------------------------------------------------------
# Résolution déterministe
# ---------------------------------------------------------------------------

def resoudre_manifeste(
    inputs_kpi_ok: bool,
    inputs_couts_ok: bool,
    docs_presents: bool,
) -> Tuple[List[DocumentRequis], bool, bool]:
    """Résout le manifeste et retourne l'état de l'intake.

    Args:
        inputs_kpi_ok: ``inputs.kpi_valeur`` fourni et non vide.
        inputs_couts_ok: ``inputs.couts`` fourni et non vide.
        docs_presents: Au moins un document déposé pour ce projet (local-docs/<project>/).

    Returns:
        Tuple (manquants, obligatoires_manquants, peut_produire_partiel) :

        - ``manquants`` — éléments du manifeste non satisfaits.
        - ``obligatoires_manquants`` — True si au moins un obligatoire est absent
          → l'agent doit retourner ``statut: "intake"``, pas de livrable.
        - ``peut_produire_partiel`` — True si les obligatoires sont OK mais des
          recommandés manquent → l'agent produit le livrable et signale les lacunes.
    """
    manquants: List[DocumentRequis] = []

    for item in MANIFESTE:
        if item.obligatoire:
            if item.element.startswith("KPI") and not inputs_kpi_ok:
                manquants.append(item)
            elif item.element.startswith("Coût") and not inputs_couts_ok:
                manquants.append(item)
        else:
            # Document recommandé : manquant si aucun doc n'est présent pour ce projet
            if not docs_presents:
                manquants.append(item)

    obligatoires_manquants = any(m.obligatoire for m in manquants)
    peut_produire_partiel = (not obligatoires_manquants) and bool(manquants)

    return manquants, obligatoires_manquants, peut_produire_partiel
