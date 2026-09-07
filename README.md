# Value Tracking Skill

Skill qui produit un bilan de valeur / réalisation des bénéfices d'un projet,
à partir d'un fichier R1 Dashboard (.xlsx) et de documents projet (business
case, reporting, données d'usage) : tableau KPI (prévu/réalisé/écart),
leviers 80/20, causes racines sourcées et recommandations priorisées.

Aucune dépendance cloud imposée : tourne en local avec la clé LLM de votre
choix (Anthropic ou OpenAI), aucun compte ni service tiers requis pour le
reste (parsing, calculs, anonymisation, historique).

## Installation

```bash
pip install -r requirements.txt
python -m spacy download fr_core_news_md   # requis par Presidio (anonymisation FR)
```

Définir une clé LLM comme variable d'environnement (pas de fichier de config requis) :

```bash
export LLM_PROVIDER=anthropic        # anthropic | openai
export ANTHROPIC_API_KEY=sk-ant-...
# ou
export LLM_PROVIDER=openai
export OPENAI_API_KEY=sk-...
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

## Scripts disponibles

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
  agent.py          ValueTrackingAgent : enchaîne les étapes ci-dessus
  r1/               Parseur du format R1 Dashboard (résolution de mapping + extraction)
  cli.py            Point d'entrée en ligne de commande
tests/              Suite de tests (pytest)
```

Pas de serveur ni d'interface : ce skill s'utilise en exécutant du code (CLI
ou import Python) — c'est l'agent qui l'invoque qui décide de la présentation
du résultat, pas ce dépôt.

## Déroulé de l'analyse

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

## Confidentialité

- Le texte qualitatif (extraits de documents, mémoire projet) passe par
  Presidio avant tout envoi au LLM (PERSON, EMAIL_ADDRESS, PHONE_NUMBER, IBAN
  masqués par défaut — configurable via `PII_ENTITIES`).
- Les chiffres (R1, `inputs`) ne passent **jamais** par l'anonymiseur : un
  montant pourrait être pris pour un numéro de téléphone et corrompu.
- Avec une clé LLM personnelle (Anthropic ou OpenAI), le texte anonymisé part
  vers l'API publique du provider choisi. Presidio retire le PII nominatif
  mais **pas** la confidentialité commerciale (montants, clauses). Ne pas
  utiliser de documents réels sensibles sans validation préalable.

## Tests

```bash
pytest
```

## Limites connues

- Un seul backend d'historique (fichier JSON local).
- `statut_reco` (suivi "fait" vs "à suivre") est mis à jour manuellement entre deux analyses.
