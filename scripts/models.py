"""Modèles Pydantic — contrats d'entrée/sortie du skill Value Tracking."""

from typing import Dict, List, Literal, Optional
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Données métier
# ---------------------------------------------------------------------------

class ValueKPI(BaseModel):
    libelle: str
    prevu: float
    realise: float
    # Réalisé cumulé à la coupe (lu dans To Date), varie selon la coupe —
    # contrairement à prevu (budget IBC) et realise (FAC), tous deux figés.
    # None = non disponible (champ non mappé/résolu), à distinguer de 0.0.
    realise_cumul: Optional[float] = None


class Cout(BaseModel):
    type: Literal["CAPEX", "OPEX"]
    libelle: str
    montant: float


class ValueTrackingInputs(BaseModel):
    kpi_valeur: List[ValueKPI]
    couts: List[Cout]
    donnees_op: Optional[Dict] = None


class Action(BaseModel):
    qui: str
    quoi: str
    quand: str
    impact_attendu: str


class ValueTrackingDeliverable(BaseModel):
    synthese: str
    kpi_table: List[Dict]
    ecarts: List[str]
    root_causes: List[str]
    recommandations: List[Action]


class DocumentRequis(BaseModel):
    """Un élément réclamé par le manifeste d'intake."""
    element: str = Field(..., description="Libellé de l'élément manquant")
    type: Literal["chiffré", "document"] = Field(..., description="Nature de l'élément")
    obligatoire: bool = Field(..., description="Bloquant si absent")
    pourquoi: str = Field(..., description="Ce que cet élément débloque dans le livrable")
    format_attendu: str = Field(..., description="Comment fournir cet élément")


class Source(BaseModel):
    """Source d'un extrait exploité."""
    title: str = Field(..., description="Titre du document")
    file: str = Field(..., description="Nom/chemin du fichier source")
    page: int = Field(..., description="Numéro de page")
    url: Optional[str] = Field(None, description="URL (aucune en mode local)")
    score: float = Field(..., description="Score de pertinence")


# ---------------------------------------------------------------------------
# Contrats de l'API
# ---------------------------------------------------------------------------

class AnalyzeRequest(BaseModel):
    """Requête POST /analyze."""
    question: str = Field(..., min_length=1, description="Question / angle de l'analyse")
    project: str = Field(..., description="Identifiant du projet (dossiers local-docs/local-memory)")
    inputs: Optional[ValueTrackingInputs] = Field(
        None, description="Données chiffrées (KPI + coûts). Voir POST /upload-r1 pour les extraire d'un R1."
    )
    cut: Optional[str] = Field(None, description="Coupe de référence YYYY.MM")
    client_name: Optional[str] = Field(
        None,
        description=(
            "Nom du client à masquer (→ <CLIENT>) avant tout envoi au LLM. "
            "Sert uniquement à configurer l'anonymisation ; n'est jamais inséré dans le prompt."
        ),
    )
    mode: Optional[Literal["cartographie", "kpi", "derives", "valeur_realisee"]] = Field(
        None, description="Angle du livrable : cartographie complète (défaut), extraction KPI, dérives, ou valeur réalisée."
    )
    seuil: Optional[float] = Field(None, description="Seuil d'alerte sur l'écart (%) — mode 'derives'.")


class AnalyzeResponse(BaseModel):
    """Réponse POST /analyze."""
    answer: str = Field(..., description="Synthèse en français")
    sources: List[Source] = Field(default_factory=list, description="Sources exploitées (toujours présent)")
    deliverable: Optional[ValueTrackingDeliverable] = Field(None, description="Livrable structuré")
    statut: Literal["intake", "complet"] = Field(
        "complet", description="intake = en attente de documents ; complet = livrable produit"
    )
    requete_documents: List[DocumentRequis] = Field(
        default_factory=list, description="Éléments réclamés par le manifeste (mode intake ou partiel)"
    )
    peut_produire_partiel: bool = Field(
        False, description="True si les obligatoires sont OK mais des recommandés manquent"
    )
