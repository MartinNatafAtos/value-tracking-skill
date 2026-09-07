# Value Tracking Agent

Bilan de valeur / réalisation des bénéfices d'un projet, à partir d'un fichier
R1 Dashboard (.xlsx) et de documents projet (business case, reporting COPIL,
données d'usage). Extrait d'un POC RAG interne — packagé ici comme un module
**autonome**, sans dépendance à Azure ni à un orchestrateur : il tourne en
local avec la clé LLM de votre choix (Anthropic ou OpenAI).

## Pourquoi ce découpage

Le module d'origine faisait partie d'un orchestrateur multi-agents avec un
mode "Azure" (Azure AI Search + Azure OpenAI) et un mode "local" (sans Azure).
Ce dossier ne reprend **que le mode local** : c'est celui qui a du sens comme
outil indépendant, réutilisable dans n'importe quel projet ou poussable dans
son propre dépôt GitHub. Il n'y a donc ici aucune dépendance Azure.

## Installation

```bash
pip install -r requirements.txt
python -m spacy download fr_core_news_md   # requis par Presidio (anonymisation FR)
cp .env.example .env
```

Renseigner dans `.env` **au moins une** clé LLM :

```
LLM_PROVIDER=anthropic        # anthropic | openai
ANTHROPIC_API_KEY=sk-ant-...
# ou
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-...
```

## Usage

```bash
python -m scripts.cli \
  --r1 /chemin/vers/dashboard.xlsx \
  --project mon-projet \
  --question "Fais le bilan de valeur du projet"
```

Ou en important les modules directement :

```python
from scripts.agent import ValueTrackingAgent
from scripts.models import AnalyzeRequest, ValueTrackingInputs, ValueKPI, Cout

agent = ValueTrackingAgent()
response = agent.run(AnalyzeRequest(
    question="Bilan de valeur",
    project="mon-projet",
    inputs=ValueTrackingInputs(
        kpi_valeur=[ValueKPI(libelle="CA", prevu=500_000, realise=420_000)],
        couts=[Cout(type="CAPEX", libelle="Infra", montant=150_000)],
    ),
))
```

## Architecture

```
scripts/
  config.py         Configuration par variables d'environnement
  models.py         Modèles Pydantic (contrats d'entrée/sortie)
  compute.py        Calculs déterministes (variance KPI, leviers 80/20) — zéro LLM
  intake.py         Manifeste des éléments requis (déterministe, pas de LLM)
  llm.py            Abstraction LLM : anthropic | openai | azure (au choix)
  anonymizer_presidio.py   Wrapper Presidio (NLP français)
  anonymize.py      Point de passage unique anonymize(text) — jamais sur les chiffres
  pdf_extract.py    Extraction de texte PDF page par page (PyMuPDF)
  local_docs.py     Collecte filtrée déterministe d'extraits projet (.pdf/.xlsx/.csv/.md/.txt)
  project_memory.py Mémoire projet lisible (.md), relue aux analyses suivantes
  history_store.py  Historique des analyses (fichier JSON local, idempotent par coupe)
  agent.py          ValueTrackingAgent : orchestre le flow complet
  r1/               Parseur du format R1 Dashboard (résolution de mapping + extraction)
  cli.py            Point d'entrée CLI (analyze)
tests/              Suite de tests (pytest)
```

Pas de serveur ni d'interface : ce skill s'utilise en exécutant du code (CLI
ou import Python), comme un skill classique — c'est l'agent qui l'invoque qui
décide de la présentation du résultat, pas ce dépôt.

## Flow de l'agent

```
1. Résolution du manifeste d'intake (déterministe)
   → obligatoire manquant (KPI ou coûts) : statut="intake", pas de livrable
2. Lecture de l'historique du projet (fichier JSON local)
3. compute.py : variance KPI + leviers 80/20 (déterministe, zéro LLM)
4. Collecte d'extraits documentaires du projet (filtre déterministe) + anonymisation Presidio
5. Lecture de la mémoire projet (.md, notes + analyses passées)
6. 1 appel LLM : synthèse + causes racines + recommandations (JSON strict)
7. Écriture de l'analyse dans l'historique local + récap .md dans la mémoire projet
8. Retour du livrable structuré (jamais de chiffre venant du LLM)
```

## Format attendu du fichier R1

Le parseur R1 (`scripts/r1/`) attend un classeur avec 3 onglets détectables
par mots-clés : `Performance` (lignes REVENUE/COSTS/MARGIN), `To Date`
(colonne PoC% + cumuls par période) et `WBS Summary` (marge par lot). Le
mapping colonne↔champ est résolu par heuristique de libellés, avec repli sur
position si ambigu, et mis en cache (`.r1_mapping_cache.json`) par signature
de format. Les onglets contenant des données RH/temps passé (`Time Bookings`,
`Work Units`, `Employee name`, `HR`) sont **automatiquement exclus, jamais
lus** — garde-fou PII sur la source elle-même.

Un fichier d'exemple anonymisé est fourni par le dépôt d'origine
(`R1_Dashboard_SAMPLE_anonymise.xlsx`) — utile pour un premier test.

## Confidentialité

- Le texte qualitatif (extraits de documents, mémoire projet) passe par
  Presidio avant tout envoi au LLM (PERSON, EMAIL_ADDRESS, PHONE_NUMBER, IBAN
  masqués par défaut — configurable via `PII_ENTITIES`).
- Les chiffres (R1, `inputs`) ne passent **jamais** par l'anonymiseur : un
  montant pourrait être pris pour un numéro de téléphone et corrompu.
- Avec une clé LLM personnelle (Anthropic ou OpenAI), le texte anonymisé part
  vers l'API publique du provider choisi. Presidio retire le PII nominatif
  mais **pas** la confidentialité commerciale (montants, clauses). Ne pas
  utiliser de documents réels sensibles sans validation de gouvernance.

## Tests

```bash
pytest
```

## Limites connues / pistes v2

- Un seul backend d'historique (JSON local) — pas d'Azure AI Search ici par
  design (voir "Pourquoi ce découpage").
- `statut_reco` (suivi "fait" vs "à suivre") est manuel entre deux analyses.
