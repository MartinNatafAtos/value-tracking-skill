---
name: value-tracking
description: Produit un bilan de valeur projet (KPI, écarts, causes racines, recommandations) à partir d'un tableau R1 Dashboard (.xlsx) et de documents projet (business case, reporting, usage). Utiliser quand on demande un suivi de réalisation des bénéfices, une analyse d'écart prévu/réalisé, ou un bilan de valeur pour un projet.
license: Apache-2.0
---

# Value Tracking — bilan de réalisation des bénéfices

Agent autonome (sans dépendance cloud obligatoire) qui transforme un fichier R1
Dashboard (.xlsx) + des documents projet en un livrable de suivi de valeur :
tableau KPI (prévu/réalisé/écart), leviers 80/20, causes racines sourcées et
jusqu'à 3 recommandations priorisées.

Ce dossier est un **skill classique** au sens des dépôts GitHub d'agent
skills : un manifeste `SKILL.md` + ses propres scripts, sans dépendance à un
dépôt parent, sans interface dédiée. Il peut être :
- copié dans `.claude/skills/` (ou équivalent) d'un autre projet Claude Code,
- déposé tel quel dans son propre dépôt GitHub,
- piloté par **n'importe quel agent LLM capable d'exécuter du code**
  (Claude Code, ou tout agent équivalent qui sait lancer une commande shell ou
  importer un module Python) — pas de serveur ni d'UI à faire tourner.

## Règle d'or

Tout chiffre du livrable (`kpi_table`, `ecarts`) vient de code déterministe
(`scripts/compute.py`), **jamais du LLM**. Le LLM ne fait que la narration
(synthèse, causes racines, recommandations) à partir de ces chiffres et
d'extraits documentaires anonymisés.

## Quand déclencher ce skill

- « Fais-moi un bilan de valeur / suivi des bénéfices du projet X »
- « Analyse l'écart entre le prévu et le réalisé sur ce projet »
- « Ce projet délivre-t-il la valeur promise dans le business case ? »
- Un fichier R1 Dashboard (.xlsx) est fourni et il faut en extraire des KPI

## Deux façons de piloter ce skill

### 1. CLI (agent capable d'exécuter du code — Claude Code, etc.)

```bash
pip install -r requirements.txt
cp .env.example .env   # renseigner au moins une clé LLM (Anthropic OU OpenAI)

python -m scripts.cli \
  --r1 /chemin/vers/dashboard.xlsx \
  --project mon-projet \
  --question "Fais le bilan de valeur du projet"
```

Affiche le livrable JSON sur stdout (`answer`, `deliverable`, `sources`,
`statut`). Si des éléments obligatoires manquent, le CLI renvoie une requête
d'intake au lieu d'inventer des chiffres.

### 2. Import direct des modules (agent avec exécution de code Python)

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

## Fichiers du skill

Voir `README.md` pour le détail de l'architecture, `scripts/` pour le code,
`tests/` pour la suite de tests.

## Garde-fous conservés (à ne pas contourner)

- Chiffres = code déterministe uniquement (`compute.py`). Jamais recalculés
  ou inventés par le LLM.
- Flux TEXTE (extraits documentaires) → Presidio (anonymisation) → LLM.
  Flux CHIFFRES (R1, inputs) → jamais anonymisé, jamais transformé par le LLM.
- L'agent réclame les éléments obligatoires manquants (`statut: "intake"`)
  au lieu de produire un livrable non fiable.
- Isolation stricte par projet : les documents/mémoire d'un projet ne sont
  jamais mélangés avec ceux d'un autre.
