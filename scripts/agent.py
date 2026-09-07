"""Agent Value Tracking — suivi et réalisation des bénéfices d'un projet.

Mode 100% local : root causes par lecture filtrée déterministe des documents
du projet (``local_docs.collect_extracts``), historique par fichier JSON
(``history_store.LocalHistoryStore``), mémoire projet par fichiers .md
(``project_memory``). Aucune dépendance à un moteur de recherche externe.
"""

from typing import Any, Dict, List, Optional

from .config import Config
from .llm import chat_completion
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


# Prompt système dédié
_SYSTEM_PROMPT = """Tu es un expert en réalisation de bénéfices et en gestion de valeur de projet.

Ton rôle est de produire un bilan de valeur DÉTAILLÉ, sourcé et en français —
pas un simple commentaire des chiffres financiers.

Tu reçois :
- Des calculs déjà réalisés (variance KPI, leviers 80/20) — tu les utilises tels quels, tu ne recalcules PAS
- Un CONTEXTE DOCUMENTAIRE : extraits de documents du projet, à EXPLOITER EN DÉTAIL. Selon leur nature :
  * business case / cas de valeur → la valeur AMBITIONNÉE au départ (bénéfices attendus, cibles) : compare-la au réalisé
  * données d'usage / adoption → analyse le niveau d'ADOPTION et son effet sur les bénéfices
  * reporting delivery / COPIL → explique les causes d'écart d'exécution (retards, qualité, périmètre)
- L'historique des analyses précédentes du projet (pour signaler les recommandations non appliquées)

Tu dois produire un JSON strict avec ces clés :
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
- Réponds UNIQUEMENT en JSON valide, sans texte autour. Toutes les valeurs sont en français."""


def _mode_guidance(mode: str, seuil: float = None) -> str:
    """Cadrage du livrable selon le cas d'usage lancé (même moteur, sortie adaptée)."""
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


# ---------------------------------------------------------------------------
# Normalisation TOLÉRANTE du JSON du LLM (garde-fous — ne jamais planter)
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
            # ex. le LLM renvoie une simple phrase → on la met dans "quoi"
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


def _documents_presents_local(project: str) -> bool:
    """Vérifie qu'au moins un fichier existe dans le dossier local du projet."""
    from pathlib import Path
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


class ValueTrackingAgent:
    """Agent de suivi et réalisation des bénéfices (mode local).

    - historique   → LocalHistoryStore (fichier JSON)
    - root causes  → collect_extracts() puis anonymize() sur chaque extrait
    - documents_presents → vérification fichier local
    """

    def __init__(self):
        from .history_store import LocalHistoryStore
        self.reader = LocalHistoryStore()
        self.writer = self.reader  # même objet : lecture + écriture

    def run(self, request: AnalyzeRequest) -> AnalyzeResponse:
        """Flow : intake check → read → compute → collect docs → llm → write → return.

        Trois branches :
        - **intake** : un obligatoire (KPI ou coûts) manque → ``statut="intake"``.
        - **partiel** : obligatoires OK, recommandés manquants → livrable produit,
          ``peut_produire_partiel=True``.
        - **complet** : tout est là → livrable complet.
        """
        project = request.project or "projet-inconnu"
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
                f"il me manque : {', '.join(manquants_labels)}. "
                f"Veuillez fournir ces données chiffrées dans le champ `inputs` de la requête."
            )
            return AnalyzeResponse(
                answer=answer,
                sources=[],
                statut="intake",
                requete_documents=manquants,
                peut_produire_partiel=False,
            )

        # --- 2. Lecture de l'historique ---
        historique = self.reader.get_historique(project, top_k=3)

        # --- 3. Calculs déterministes (jamais le LLM pour les chiffres) ---
        kpi_dicts = [k.model_dump() for k in inputs.kpi_valeur]
        kpi_table = compute_variance(kpi_dicts)
        ecarts = analyse_8020(kpi_table)

        # Nom de client saisi par l'utilisateur → masqué AVANT le LLM.
        client_names = [request.client_name] if request.client_name else None

        # --- 4. Extraits documentaires locaux → Presidio ---
        chunks = collect_extracts(project, Config.LOCAL_DOCS_DIR)
        # Garde anti-corruption : anonymize uniquement sur le champ "content" (texte)
        # Les chiffres (kpi_table, ecarts) ne passent JAMAIS par anonymize
        for chunk in chunks:
            chunk["content"] = anonymize(chunk["content"], client_names=client_names)

        # --- 4bis. Mémoire projet (.MD) : analyses passées + notes ---
        memory = read_memory(project)
        for mem in memory:
            mem["content"] = anonymize(mem["content"], client_names=client_names)

        # --- 5. Appel LLM unique ---
        # Données cut-dépendantes issues d'un parser R1 (poc/couts_cumul/marge_cumul/
        # rev_cumul à la coupe + wbs_pareto snapshot) — défensif : peut être absent.
        donnees_op = inputs.donnees_op if inputs else {}

        llm_result = self._call_llm(
            question=request.question,
            kpi_table=kpi_table,
            ecarts=ecarts,
            historique=historique,
            chunks=chunks,
            peut_produire_partiel=peut_produire_partiel,
            manquants_recommandes=[m.element for m in manquants if not m.obligatoire],
            client_names=client_names,
            mode=(request.mode or "cartographie"),
            seuil=request.seuil,
            memory=memory,
            donnees_op=donnees_op,
        )

        # Garde-fous : le JSON du LLM peut renommer/oublier des clés ou changer
        # de type. On normalise sans jamais lever d'exception.
        if not isinstance(llm_result, dict):
            llm_result = {}
        synthese = _coerce_str(_first_key(llm_result, ["synthese", "synthèse", "summary"], ""))
        root_causes = _coerce_str_list(
            _first_key(llm_result, ["root_causes", "causes", "causes_racines"], [])
        )
        reco_raw = _first_key(llm_result, ["recommandations", "recommendations", "reco", "actions"], [])
        recommandations = _build_actions(reco_raw)

        # --- 6. Construction des sources ---
        sources = build_sources(chunks)

        # --- 7. Écriture dans l'historique (passe cut pour la clé locale) ---
        self.writer.upsert_analyse(
            project=project,
            synthese=synthese,
            ecarts=ecarts,
            root_causes=root_causes,
            recommandations=[r.model_dump() for r in recommandations],
            sources=[s.model_dump() for s in sources],
            cut=request.cut,
        )

        # --- 7bis. Mémoire projet (.MD) : récap lisible pour les analyses futures ---
        try:
            write_recap(
                project=project,
                cut=request.cut or "",
                mode=(request.mode or "cartographie"),
                synthese=synthese,
                ecarts=ecarts,
                root_causes=root_causes,
                recommandations=[r.model_dump() for r in recommandations],
            )
        except Exception:
            pass  # ne jamais bloquer le livrable sur l'écriture mémoire

        # --- 8. Retour du livrable ---
        deliverable = ValueTrackingDeliverable(
            synthese=synthese,
            kpi_table=kpi_table,
            ecarts=ecarts,
            root_causes=root_causes,
            recommandations=recommandations,
        )

        return AnalyzeResponse(
            answer=synthese,
            sources=sources,
            deliverable=deliverable,
            statut="complet",
            requete_documents=manquants,
            peut_produire_partiel=peut_produire_partiel,
        )

    # ------------------------------------------------------------------
    # Méthodes privées
    # ------------------------------------------------------------------

    def _build_llm_context(
        self,
        kpi_table: List[Dict],
        ecarts: List[str],
        historique: List[Dict],
        chunks: List[Dict],
        peut_produire_partiel: bool = False,
        manquants_recommandes: List[str] = None,
        memory: List[Dict] = None,
        donnees_op: Dict = None,
    ) -> str:
        """Construit le message utilisateur pour le LLM."""
        import json

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

    def _call_llm(
        self,
        question: str,
        kpi_table: List[Dict],
        ecarts: List[str],
        historique: List[Dict],
        chunks: List[Dict],
        peut_produire_partiel: bool = False,
        manquants_recommandes: List[str] = None,
        client_names: List[str] = None,
        mode: str = "cartographie",
        seuil: float = None,
        memory: List[Dict] = None,
        donnees_op: Dict = None,
    ) -> Dict[str, Any]:
        """Appel unique au LLM. Retourne un dict parsé.

        Utilise chat_completion() pour être provider-agnostique
        (anthropic | openai | azure — voir Config.LLM_PROVIDER).
        `mode` cadre le livrable selon le cas d'usage lancé.
        """
        context = self._build_llm_context(
            kpi_table, ecarts, historique, chunks,
            peut_produire_partiel=peut_produire_partiel,
            manquants_recommandes=manquants_recommandes or [],
            memory=memory or [],
            donnees_op=donnees_op or {},
        )
        user_content = f"Demande : {question}\n\n{context}"

        # Cadrage du livrable selon le cas d'usage (mode)
        system_prompt = _SYSTEM_PROMPT + _mode_guidance(mode, seuil)

        # VERROU FINAL : masquer le nom du client sur TOUT ce qui part au LLM
        # (question, id projet, extraits, historique) — juste avant l'envoi.
        user_content = mask_client_names(user_content, client_names)

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

        # max_tokens généreux : l'analyse détaillée (root causes + reco sourcées)
        # est longue, et les modèles récents (Sonnet 5, Fable 5…) ont un thinking
        # toujours actif qui consomme aussi le budget → sans marge, le JSON est
        # tronqué et devient impossible à parser. 16000 = plafond non-streaming sûr.
        raw = chat_completion(
            messages=messages,
            temperature=0.3,
            max_tokens=16000,
            json_mode=True,
        )

        return self._parse_llm_json(raw)

    @staticmethod
    def _parse_llm_json(raw: str) -> Dict[str, Any]:
        """Parse le JSON du LLM, robuste aux ```fences et au texte autour.

        Certains modèles peuvent entourer le JSON de ```json ... ``` ou d'un
        préambule. On nettoie les fences, puis on tente json.loads, puis
        l'extraction du premier objet {...}. En dernier recours : narration
        brute (pas de crash).
        """
        import json
        import re

        text = (raw or "").strip()
        # Retirer les fences ```json ... ``` ou ``` ... ```
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
            return {
                "synthese": text,
                "root_causes": [],
                "recommandations": [],
            }
