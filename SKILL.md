---
name: value-tracking
description: Produit un bilan de valeur projet (KPI, écarts, causes racines, recommandations) à partir d'un tableau R1 Dashboard (.xlsx) et de documents projet (business case, reporting, usage). Utiliser quand on demande un suivi de réalisation des bénéfices, une analyse d'écart prévu/réalisé, ou un bilan de valeur pour un projet.
license: Apache-2.0
---

# Value Tracking — bilan de réalisation des bénéfices

Skill qui transforme un fichier R1 Dashboard (.xlsx) + des documents projet en
un livrable de suivi de valeur : tableau KPI (prévu/réalisé/écart), leviers
80/20, causes racines sourcées et recommandations priorisées.

Ce dossier est un **skill classique** au sens des dépôts GitHub d'agent
skills : un manifeste `SKILL.md` + ses propres scripts, sans dépendance à un
dépôt parent, sans interface dédiée, sans clé LLM à configurer.

**Aucun appel LLM interne.** Un skill est invoqué PAR un agent LLM (toi qui
lis ce fichier) qui a déjà sa propre capacité de raisonnement — il serait
absurde qu'il appelle un second LLM avec sa propre clé API pour rédiger le
texte. Les scripts font uniquement le travail déterministe (calculs,
extraction, anonymisation) ; c'est **toi, l'agent qui exécutes ce skill**, qui
rédiges la synthèse/les causes/les recommandations, avec ton propre
raisonnement, à partir des données préparées par les scripts.

## Règle d'or

Tout chiffre du livrable (`kpi_table`, `ecarts`) vient de code déterministe
(`scripts/compute.py`), **jamais reformulé, recalculé ou inventé par toi**.
Ton rôle se limite à la narration (synthèse, causes racines, recommandations)
à partir de ces chiffres et des extraits documentaires anonymisés fournis.

## Quand déclencher ce skill

- « Fais-moi un bilan de valeur / suivi des bénéfices du projet X »
- « Analyse l'écart entre le prévu et le réalisé sur ce projet »
- « Ce projet délivre-t-il la valeur promise dans le business case ? »
- Un fichier R1 Dashboard (.xlsx) est fourni et il faut en extraire des KPI

## Comment l'utiliser : deux appels, un entre-deux rédigé par toi

```bash
pip install -r requirements.txt
```

**Étape 1 — calculs déterministes.** Lance l'analyse (via un fichier R1 ou
des KPI/coûts au format JSON) :

```bash
python -m scripts.cli analyze \
  --r1 /chemin/vers/dashboard.xlsx \
  --project mon-projet \
  --question "Fais le bilan de valeur du projet"
```

Deux résultats possibles :
- `statut: "intake"` → des éléments obligatoires manquent (KPI ou coûts).
  Réponds directement avec le contenu retourné, n'invente aucun chiffre.
- `statut: "besoin_narratif"` → la commande retourne `instructions` (ce
  qu'il faut rédiger), `context` (les données à exploiter : calculs KPI,
  extraits documentaires anonymisés, historique) et `state_file`.

**Étape 2 — c'est TOI qui rédiges.** Lis `instructions` + `context`, et
rédige toi-même le JSON `{"synthese", "root_causes", "recommandations"}`
demandé — c'est ton travail de raisonnement, pas celui d'un script.
Enregistre ce JSON dans un fichier.

**Étape 3 — enregistrement.** Fusionne ta narration avec les données
déterministes et écris le résultat dans l'historique local :

```bash
python -m scripts.cli record --state <state_file> --narrative-file narrative.json
```

Affiche le livrable final complet (`answer`, `deliverable`, `sources`).

### Alternative : import direct des modules Python

```python
from scripts.agent import ValueTrackingAgent
from scripts.models import AnalyzeRequest, ValueTrackingInputs, ValueKPI, Cout

agent = ValueTrackingAgent()
prepared = agent.prepare(AnalyzeRequest(
    question="Bilan de valeur",
    project="mon-projet",
    inputs=ValueTrackingInputs(
        kpi_valeur=[ValueKPI(libelle="CA", prevu=500_000, realise=420_000)],
        couts=[Cout(type="CAPEX", libelle="Infra", montant=150_000)],
    ),
))
# prepared["statut"] == "besoin_narratif" -> lire prepared["instructions"] + prepared["context"],
# rédiger toi-même la narration, puis :
response = agent.record(prepared["state_file"], {
    "synthese": "...", "root_causes": [...], "recommandations": [...],
})
```

## Fichiers du skill

Voir `README.md` pour le détail de l'architecture, `scripts/` pour le code,
`tests/` pour la suite de tests.

## Garde-fous conservés (à ne pas contourner)

- Chiffres = code déterministe uniquement (`compute.py`). Jamais recalculés
  ou inventés dans la narration.
- Flux TEXTE (extraits documentaires) → Presidio (anonymisation) → toi.
  Flux CHIFFRES (R1, inputs) → jamais anonymisé, jamais reformulé par toi.
- Le skill réclame les éléments obligatoires manquants (`statut: "intake"`)
  au lieu de produire un livrable non fiable.
- Isolation stricte par projet : les documents/mémoire d'un projet ne sont
  jamais mélangés avec ceux d'un autre.
