"""Agent Value Tracking — suivi et réalisation des bénéfices d'un projet.

Aucun appel LLM interne : ce skill est invoqué PAR un agent LLM (Claude,
ChatGPT, ou tout autre) qui possède déjà sa propre capacité de raisonnement.
Le flow se déroule donc en deux étapes explicites :

1. ``prepare(request)`` — tout ce qui est déterministe : validation d'intake,
   calculs KPI (``compute.py``), collecte + anonymisation des extraits
   documentaires, lecture de l'historique/mémoire. Retourne soit une réponse
   d'intake finale, soit un « briefing » (instructions + contexte) que
   l'agent appelant doit lire pour rédiger lui-même la synthèse, les causes
   racines et les recommandations — sans jamais recalculer un chiffre.
2. ``record(state_file, narrative)`` — reprend l'état déterministe persisté
   par ``prepare``, y fusionne la narration rédigée par l'agent appelant,
   écrit l'analyse dans l'historique local, et retourne le livrable final.

Mode 100% local : root causes par lecture filtrée déterministe des documents
du projet (``local_docs.collect_extracts``), historique par fichier JSON
(``history_store.LocalHistoryStore``), mémoire projet par fichiers .md
(``project_memory``).
"""

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import Config
from .models import (
    Action,
    AnalyzeRequest,
    AnalyzeResponse,
    DocumentRequis,
    Source,
    ValueTrackingDeliverable,
)
from .compute import compute_variance, analyse_8020
from .intake import resoudre_manifeste
from .local_docs import collect_extracts
from .anonymize import anonymize, mask_client_names
from .project_memory import read_memory, write_recap


# Instructions pour l'agent appelant (pas un "system prompt" envoyé à une API
# interne — c'est ce que l'agent qui invoque ce skill doit suivre lui-même).
NARRATIVE_INSTRUCTIONS = """Tu es en train de produire un bilan de valeur et de réalisation des bénéfices
d'un projet, à partir du CONTEXTE fourni ci-dessous.

Le contexte contient :
- Des calculs déjà réalisés (variance KPI, leviers 80/20) — utilise-les tels quels, ne les recalcule PAS
- Un CONTEXTE DOCUMENTAIRE : extraits de documents du projet, à EXPLOITER EN DÉTAIL. Selon leur nature :
  * business case / cas de valeur → la valeur AMBITIONNÉE au départ (bénéfices attendus, cibles) : compare-la au réalisé
  * données d'usage / adoption → analyse le niveau d'ADOPTION et son effet sur les bénéfices
  * reporting delivery / COPIL → explique les causes d'écart d'exécution (retards, qualité, périmètre)
- L'historique des analyses précédentes du projet (pour signaler les recommandations non appliquées)

Rédige un JSON strict avec ces clés :
{
  "synthese": "Synthèse narrative riche (5-8 phrases) : situation valeur vs coûts, écart au business case, niveau d'adoption",
  "root_causes": ["cause détaillée et SOURCÉE (cite le document), ..."],
  "recommandations": [
    {"qui": "...", "quoi": "action concrète ancrée dans les documents", "quand": "...", "impact_attendu": "chiffré si possible"}
  ]
}

Règles :
- EXPLOITE réellement les documents fournis : cite-les dans les root_causes et recommandations (ex. « d'après le business case… », « le reporting COPIL indique… »). Ne te contente PAS de paraphraser les chiffres.
- Compare systématiquement le RÉALISÉ à l'AMBITION du business case quand il est fourni ; si l'adoption est faible, relie-la à l'écart de bénéfices.
- Les root_causes croisent documents ET chiffres, et expliquent POURQUOI (pas seulement QUOI).
- 3 recommandations maximum, concrètes, priorisées, réalisables, chiffrées si possible, ancrées dans les documents.
- Si des documents manquent (ex. pas de business case fourni), dis-le explicitement dans la synthèse au lieu d'inventer.
- Si des recommandations précédentes n'ont pas été appliquées (statut_reco = "à suivre"), signale-le dans la synthèse.
- Toutes les valeurs sont en français.
- Une fois ce JSON rédigé, passe-le à `record()` / `python -m scripts.cli record` avec le `state_file` fourni."""


def mode_guidance(mode: str, seuil: Optional[float] = None) -> str:
    """Cadrage du livrable selon le cas d'usage lancé (mêmes données, sortie adaptée)."""
    if mode == "kpi":
        return (
            "\n\n=== MODE : EXTRACTION DES KPI ===\n"
            "Objectif : restituer les indicateurs clés, pas d'analyse causale.\n"
            "- synthese : 2-3 phrases factuelles sur la lecture des KPI (prévu/réalisé/écart).\n"
            "- root_causes : [] (vide).\n"
            "- recommandations : [] (vide)."
        )
    if mode == "derives":
        s = f" (seuil d'alerte : {seuil:g}% d'écart)" if seuil is not None else ""
        return (
            f"\n\n=== MODE : DÉTECTION DES DÉRIVES{s} ===\n"
            "Objectif : repérer les ÉCARTS prévu vs réalisé et ALERTER sur les dérives.\n"
            "- synthese : centrée sur les dérives et leur gravité ; signale ce qui dépasse le seuil.\n"
            "- root_causes : causes des dérives significatives (croisées docs + chiffres).\n"
            "- recommandations : actions CORRECTIVES priorisées sur les dérives."
        )
    if mode == "valeur_realisee":
        return (
            "\n\n=== MODE : VALEUR RÉALISÉE ===\n"
            "Objectif : comparer le RÉALISÉ à l'AMBITION / au prévu — quelle valeur a été "
            "effectivement délivrée par rapport à ce qui était promis.\n"
            "- synthese : orientée valeur délivrée vs ambition initiale (business case si fourni)."
        )
    # cartographie (défaut) : bilan complet, aucun cadrage restrictif.
    return ""


def build_context(
    kpi_table: List[Dict],
    ecarts: List[str],
    historique: List[Dict],
    chunks: List[Dict],
    peut_produire_partiel: bool = False,
    manquants_recommandes: Optional[List[str]] = None,
    memory: Optional[List[Dict]] = None,
    donnees_op: Optional[Dict] = None,
) -> str:
    """Construit le bloc de contexte que l'agent appelant doit lire pour rédiger la narration."""
    parts = []

    # Calculs déterministes — ne pas recalculer
    parts.append("=== CALCULS KPI (déterministes, ne pas recalculer) ===")
    parts.append(json.dumps(kpi_table, ensure_ascii=False, indent=2))
    parts.append(
        f"\nLeviers expliquant ~80% de l'écart : "
        f"{', '.join(ecarts) if ecarts else 'aucun écart significatif'}"
    )
    parts.append(
        "\nNote KPI : `realise` = FAC (atterrissage prévu en fin de projet, stable). "
        "`realise_cumul` = réalisé cumulé À LA COUPE (avancement réel à la date "
        "choisie, varie selon la coupe). L'écart chiffré (ecart_abs) compare "
        "FAC↔budget."
    )

    # --- Situation cumulée à la coupe (déterministe, cut-dépendante) ---
    # Défensif : donnees_op peut être None/vide (ex. inputs sans R1 parsé) ;
    # on n'ajoute la section que si au moins une valeur exploitable est présente.
    if donnees_op:
        lignes = []
        cut = donnees_op.get("cut")
        if cut is not None:
            lignes.append(f"Coupe analysée : {cut}")
        poc = donnees_op.get("poc")
        if poc is not None:
            try:
                lignes.append(f"PoC (avancement) : {float(poc) * 100:.1f}%")
            except (TypeError, ValueError):
                lignes.append(f"PoC (avancement) : {poc}")
        rev_cumul = donnees_op.get("rev_cumul")
        if rev_cumul is not None:
            lignes.append(f"CA cumulé à la coupe : {rev_cumul} K EUR")
        couts_cumul = donnees_op.get("couts_cumul")
        if couts_cumul is not None:
            lignes.append(
                f"Coûts cumulés à la coupe : {couts_cumul} K EUR "
                f"(valeur négative = coûts, telle que dans le R1)"
            )
        marge_cumul = donnees_op.get("marge_cumul")
        if marge_cumul is not None:
            lignes.append(f"Marge cumulée à la coupe : {marge_cumul} K EUR")

        if lignes:
            parts.append(
                "\n=== SITUATION CUMULÉE À LA COUPE (déterministe, ne pas recalculer) ==="
            )
            parts.append(
                "Ces valeurs VARIENT selon la coupe : c'est la situation réelle du "
                "projet à la date choisie. Appuie-toi dessus pour la synthèse et les "
                "tendances (comparaison avec l'historique des coupes précédentes)."
            )
            parts.extend(lignes)

        wbs_pareto = donnees_op.get("wbs_pareto")
        if wbs_pareto:
            parts.append(
                "\n=== VENTILATION PAR LOT WBS (PHOTO au JJ.MM du fichier — "
                "NON cut-dépendante) ==="
            )
            parts.append(
                "ATTENTION : cette ventilation par lot est la photo à la date du "
                "fichier et ne reflète PAS la coupe passée choisie (le R1 ne contient "
                "pas l'historique par lot). Ne présente donc PAS ces montants par lot "
                "comme étant « à la coupe » si la coupe diffère de la date du fichier."
            )
            for lot in wbs_pareto:
                libelle = lot.get("libelle") or lot.get("wbs_label") or lot.get("wbs_code") or "?"
                marge = lot.get("marge")
                if marge is None:
                    marge = lot.get("wbs_margin")
                parts.append(f"  - {libelle} : {marge} K EUR")

    if peut_produire_partiel and manquants_recommandes:
        parts.append("\n=== NOTE MODE PARTIEL ===")
        parts.append(
            "Les documents suivants ne sont pas encore déposés pour ce projet. "
            "Les root causes et recommandations s'appuieront uniquement sur les données chiffrées. "
            "Signale cette limite dans la synthèse."
        )
        for doc in manquants_recommandes:
            parts.append(f"  - {doc}")

    if historique:
        parts.append("\n=== HISTORIQUE DES ANALYSES PRÉCÉDENTES ===")
        for h in historique:
            statut = h.get("statut_reco", "à suivre")
            parts.append(
                f"[{h.get('date_analyse', '')}] Statut reco : {statut}\n"
                f"Synthèse : {h.get('synthese', '')}\n"
                f"Recommandations précédentes : {h.get('recommandations', [])}"
            )
    else:
        parts.append("\n=== HISTORIQUE : aucune analyse précédente pour ce projet ===")

    if memory:
        parts.append(
            "\n=== MÉMOIRE DU PROJET (analyses passées + notes, anonymisée) ===\n"
            "Tiens compte de l'ÉVOLUTION du projet dans le temps (ex. d'une "
            "année/coupe à l'autre) et signale les tendances et les points de suivi."
        )
        for m in memory:
            parts.append(f"[{m.get('title', 'mémoire')}]\n{m.get('content', '')}")

    if chunks:
        parts.append("\n=== CONTEXTE DOCUMENTAIRE (anonymisé) ===")
        for i, chunk in enumerate(chunks, 1):
            title = chunk.get("title") or chunk.get("file", "")
            parts.append(
                f"[Doc {i}] {title} - p.{chunk.get('page', '?')}\n{chunk.get('content', '')}"
            )

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Normalisation TOLÉRANTE de la narration fournie par l'agent appelant
# (garde-fous — ne jamais planter, quel que soit le LLM qui a rédigé le JSON)
# ---------------------------------------------------------------------------

def _first_key(d: dict, keys: list, default=None):
    """Retourne la 1re clé présente parmi `keys` (tolère renommages/synonymes)."""
    for k in keys:
        if isinstance(d, dict) and k in d and d[k] is not None:
            return d[k]
    return default


def _coerce_str(value) -> str:
    """Force une valeur en chaîne (une liste devient des puces)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        return " ; ".join(_coerce_str(v) for v in value if v is not None)
    if isinstance(value, dict):
        return " ; ".join(f"{k}: {_coerce_str(v)}" for k, v in value.items())
    return str(value)


def _coerce_str_list(value) -> list:
    """Force une valeur en liste de chaînes non vides."""
    if value is None:
        return []
    if isinstance(value, str):
        s = value.strip()
        return [s] if s else []
    if isinstance(value, (list, tuple)):
        return [s for s in (_coerce_str(v) for v in value) if s]
    return [_coerce_str(value)] if _coerce_str(value) else []


def _build_actions(reco_raw, maximum: int = 5) -> list:
    """Construit des Action robustes : clés tolérantes, valeurs manquantes → '—',
    éléments malformés ignorés (jamais d'exception)."""
    if isinstance(reco_raw, dict):
        reco_raw = [reco_raw]
    if not isinstance(reco_raw, (list, tuple)):
        return []
    actions = []
    for item in reco_raw:
        if not isinstance(item, dict):
            # ex. une simple phrase → on la met dans "quoi"
            txt = _coerce_str(item)
            if txt:
                actions.append(Action(qui="—", quoi=txt, quand="—", impact_attendu="—"))
            continue
        qui = _coerce_str(_first_key(item, ["qui", "who", "responsable", "acteur"], "—")) or "—"
        quoi = _coerce_str(_first_key(item, ["quoi", "what", "action", "description", "recommandation"], "")) or "—"
        quand = _coerce_str(_first_key(item, ["quand", "when", "echeance", "échéance", "delai", "délai", "date"], "—")) or "—"
        impact = _coerce_str(_first_key(item, ["impact_attendu", "impact", "impact_expected", "benefice", "bénéfice", "gain"], "—")) or "—"
        # Ignorer un élément totalement vide
        if quoi == "—" and impact == "—" and qui == "—":
            continue
        actions.append(Action(qui=qui, quoi=quoi, quand=quand, impact_attendu=impact))
        if len(actions) >= maximum:
            break
    return actions


def parse_narrative_json(raw: str) -> Dict[str, Any]:
    """Parse la narration rédigée par l'agent appelant, robuste aux ```fences
    et au texte autour (un agent peut entourer son JSON de ```json ... ``` ou
    d'un préambule). En dernier recours : narration brute (pas de crash)."""
    import re

    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
        return {"synthese": text, "root_causes": [], "recommandations": []}


def _documents_presents_local(project: str) -> bool:
    """Vérifie qu'au moins un fichier existe dans le dossier local du projet."""
    docs_dir = Path(Config.LOCAL_DOCS_DIR)
    project_dir = docs_dir / project
    if not project_dir.is_dir():
        return False
    return any(project_dir.rglob("*.*"))


def build_sources(chunks: List[Dict[str, Any]]) -> List[Source]:
    """Construit la liste des sources à partir des chunks. Déduplique par (file, page)."""
    seen = set()
    sources = []
    for chunk in chunks:
        key = (chunk["file"], chunk["page"])
        if key not in seen:
            seen.add(key)
            sources.append(Source(
                title=chunk.get("title") or chunk.get("file", ""),
                file=chunk["file"],
                page=chunk["page"],
                url=chunk.get("url"),
                score=chunk["score"],
            ))
    return sources


def _safe(name: str) -> str:
    return os.path.basename((name or "").strip()) or "projet"


class ValueTrackingAgent:
    """Agent de suivi et réalisation des bénéfices (mode local, sans LLM interne).

    - historique   → LocalHistoryStore (fichier JSON)
    - root causes  → collect_extracts() puis anonymize() sur chaque extrait
    - documents_presents → vérification fichier local
    """

    def __init__(self):
        from .history_store import LocalHistoryStore
        self.reader = LocalHistoryStore()
        self.writer = self.reader  # même objet : lecture + écriture

    # ------------------------------------------------------------------
    # Étape 1 — déterministe
    # ------------------------------------------------------------------

    def prepare(self, request: AnalyzeRequest) -> Dict[str, Any]:
        """Prépare l'analyse : intake, calculs, extraits, historique.

        Retourne soit :
        - ``{"statut": "intake", "answer": ..., "sources": [], "requete_documents": [...], "peut_produire_partiel": False}``
          → aucune suite à donner, à renvoyer tel quel.
        - ``{"statut": "besoin_narratif", "instructions": str, "context": str, "state_file": str}``
          → l'agent appelant doit lire `instructions` + `context`, rédiger le JSON de
          narration demandé, puis appeler ``record(state_file, narrative)``.
        """
        project = request.project
        inputs = request.inputs

        # --- 1. Résolution du manifeste d'intake (déterministe, pas de LLM) ---
        inputs_kpi_ok = bool(inputs and inputs.kpi_valeur)
        inputs_couts_ok = bool(inputs and inputs.couts)
        docs_presents = _documents_presents_local(project)

        manquants, obligatoires_manquants, peut_produire_partiel = resoudre_manifeste(
            inputs_kpi_ok=inputs_kpi_ok,
            inputs_couts_ok=inputs_couts_ok,
            docs_presents=docs_presents,
        )

        if obligatoires_manquants:
            manquants_labels = [m.element for m in manquants if m.obligatoire]
            answer = (
                f"Pour produire le bilan de valeur du projet « {project} », "
                f"il manque : {', '.join(manquants_labels)}. "
                f"Fournir ces données chiffrées dans le champ `inputs` de la requête."
            )
            return {
                "statut": "intake",
                "answer": answer,
                "sources": [],
                "requete_documents": [m.model_dump() for m in manquants],
                "peut_produire_partiel": False,
            }

        # --- 2. Lecture de l'historique ---
        historique = self.reader.get_historique(project, top_k=3)

        # --- 3. Calculs déterministes (jamais recalculés par un LLM) ---
        kpi_dicts = [k.model_dump() for k in inputs.kpi_valeur]
        kpi_table = compute_variance(kpi_dicts)
        ecarts = analyse_8020(kpi_table)

        # Nom de client saisi par l'utilisateur → masqué avant toute lecture externe.
        client_names = [request.client_name] if request.client_name else None

        # --- 4. Extraits documentaires locaux → Presidio ---
        chunks = collect_extracts(project, Config.LOCAL_DOCS_DIR)
        # Garde anti-corruption : anonymize uniquement sur le champ "content" (texte).
        # Les chiffres (kpi_table, ecarts) ne passent JAMAIS par anonymize.
        for chunk in chunks:
            chunk["content"] = anonymize(chunk["content"], client_names=client_names)

        # --- 4bis. Mémoire projet (.MD) : analyses passées + notes ---
        memory = read_memory(project)
        for mem in memory:
            mem["content"] = anonymize(mem["content"], client_names=client_names)

        # --- 5. Contexte + instructions pour l'agent appelant ---
        donnees_op = inputs.donnees_op if inputs else {}
        manquants_recommandes = [m.element for m in manquants if not m.obligatoire]

        context = build_context(
            kpi_table, ecarts, historique, chunks,
            peut_produire_partiel=peut_produire_partiel,
            manquants_recommandes=manquants_recommandes,
            memory=memory,
            donnees_op=donnees_op or {},
        )
        context = f"Demande : {request.question}\n\n{context}"
        # VERROU FINAL : masquer le nom du client sur tout ce qui sera lu par l'agent appelant.
        context = mask_client_names(context, client_names)

        instructions = NARRATIVE_INSTRUCTIONS + mode_guidance(request.mode or "cartographie", request.seuil)

        sources = build_sources(chunks)

        state_file = self._write_state({
            "project": project,
            "cut": request.cut,
            "mode": request.mode or "cartographie",
            "kpi_table": kpi_table,
            "ecarts": ecarts,
            "sources": [s.model_dump() for s in sources],
            "manquants": [m.model_dump() for m in manquants],
            "peut_produire_partiel": peut_produire_partiel,
        })

        return {
            "statut": "besoin_narratif",
            "instructions": instructions,
            "context": context,
            "state_file": state_file,
        }

    # ------------------------------------------------------------------
    # Étape 2 — fusion de la narration rédigée par l'agent appelant
    # ------------------------------------------------------------------

    def record(self, state_file: str, narrative: Any) -> AnalyzeResponse:
        """Fusionne la narration (rédigée par l'agent appelant) avec l'état
        déterministe de ``prepare()``, écrit l'historique, retourne le livrable.

        Args:
            state_file: Chemin retourné par ``prepare()``.
            narrative: dict ``{"synthese", "root_causes", "recommandations"}``,
                ou une chaîne JSON (éventuellement entourée de ```fences```) —
                normalisée de façon tolérante dans les deux cas.
        """
        with open(state_file, "r", encoding="utf-8") as f:
            state = json.load(f)

        if isinstance(narrative, str):
            narrative = parse_narrative_json(narrative)
        if not isinstance(narrative, dict):
            narrative = {}

        synthese = _coerce_str(_first_key(narrative, ["synthese", "synthèse", "summary"], ""))
        root_causes = _coerce_str_list(
            _first_key(narrative, ["root_causes", "causes", "causes_racines"], [])
        )
        reco_raw = _first_key(narrative, ["recommandations", "recommendations", "reco", "actions"], [])
        recommandations = _build_actions(reco_raw)

        sources = [Source(**s) for s in state["sources"]]
        manquants = [DocumentRequis(**m) for m in state["manquants"]]

        self.writer.upsert_analyse(
            project=state["project"],
            synthese=synthese,
            ecarts=state["ecarts"],
            root_causes=root_causes,
            recommandations=[r.model_dump() for r in recommandations],
            sources=[s.model_dump() for s in sources],
            cut=state.get("cut"),
        )

        try:
            write_recap(
                project=state["project"],
                cut=state.get("cut") or "",
                mode=state.get("mode", "cartographie"),
                synthese=synthese,
                ecarts=state["ecarts"],
                root_causes=root_causes,
                recommandations=[r.model_dump() for r in recommandations],
            )
        except Exception:
            pass  # ne jamais bloquer le livrable sur l'écriture mémoire

        deliverable = ValueTrackingDeliverable(
            synthese=synthese,
            kpi_table=state["kpi_table"],
            ecarts=state["ecarts"],
            root_causes=root_causes,
            recommandations=recommandations,
        )

        return AnalyzeResponse(
            answer=synthese,
            sources=sources,
            deliverable=deliverable,
            statut="complet",
            requete_documents=manquants,
            peut_produire_partiel=state["peut_produire_partiel"],
        )

    # ------------------------------------------------------------------
    # Persistance de l'état intermédiaire entre prepare() et record()
    # ------------------------------------------------------------------

    def _write_state(self, state: Dict[str, Any]) -> str:
        state_dir = Path(Config.STATE_DIR)
        state_dir.mkdir(parents=True, exist_ok=True)

        key = f"{state['project']}:{state.get('cut') or ''}:{state.get('mode') or ''}"
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
        path = state_dir / f"{_safe(state['project'])}__{digest}.json"

        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)

        return str(path)
