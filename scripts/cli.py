"""Point d'entrée CLI du skill Value Tracking.

Ce skill ne fait AUCUN appel LLM en interne : c'est l'agent qui l'exécute
(Claude, ChatGPT, ou tout autre) qui rédige la narration lui-même, avec sa
propre capacité de raisonnement. D'où le déroulé en deux commandes :

    1. `analyze` — calcule tout ce qui est déterministe (KPI, écarts, extraits
       documentaires anonymisés, historique) et affiche des instructions +
       un contexte à lire, ainsi qu'un `state_file`.
    2. L'agent qui a lancé la commande lit ce JSON, rédige lui-même la
       narration (synthese/root_causes/recommandations) demandée dans
       `instructions`, l'enregistre dans un fichier JSON.
    3. `record` — fusionne cette narration avec l'état déterministe, écrit
       l'historique local, et affiche le livrable final.

Exemple :
    python -m scripts.cli analyze --r1 dashboard.xlsx --project alpha --question "Bilan de valeur"
    # -> lire "instructions" + "context", rédiger narrative.json, puis :
    python -m scripts.cli record --state <state_file> --narrative-file narrative.json
"""

from __future__ import annotations

import argparse
import json
import sys

# La sortie contient des accents et des symboles (→, «, ») : sur certaines
# consoles Windows, l'encodage par défaut (cp1252) plante dessus. On force
# l'UTF-8 pour que la sortie soit portable partout.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")


def _cmd_analyze(args: argparse.Namespace) -> int:
    from .agent import ValueTrackingAgent
    from .models import AnalyzeRequest

    inputs = None
    cut = args.cut

    if args.r1:
        import openpyxl

        from .config import Config
        from .r1.intake import process_r1_workbook

        wb = openpyxl.load_workbook(args.r1, data_only=True)
        result = process_r1_workbook(wb, cut=args.cut, cache_path=Config.R1_MAPPING_CACHE)
        inputs = result["inputs"]
        cut = result["cut"]
        print(f"[R1] coupe sélectionnée : {cut} (disponibles : {result['coupes']})", file=sys.stderr)
    elif args.inputs_json:
        from .models import ValueTrackingInputs

        with open(args.inputs_json, "r", encoding="utf-8") as f:
            inputs = ValueTrackingInputs(**json.load(f))

    request = AnalyzeRequest(
        question=args.question,
        project=args.project,
        inputs=inputs,
        cut=cut,
        client_name=args.client_name,
        mode=args.mode,
        seuil=args.seuil,
    )

    agent = ValueTrackingAgent()
    result = agent.prepare(request)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _cmd_record(args: argparse.Namespace) -> int:
    from .agent import ValueTrackingAgent

    if args.narrative_file:
        with open(args.narrative_file, "r", encoding="utf-8") as f:
            narrative = json.load(f)
    else:
        narrative = json.loads(args.narrative_json)

    agent = ValueTrackingAgent()
    response = agent.record(args.state, narrative)
    print(json.dumps(response.model_dump(), ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Value Tracking Skill — CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_analyze = sub.add_parser(
        "analyze", help="Étape 1 : calculs déterministes + briefing pour l'agent appelant"
    )
    p_analyze.add_argument("--project", required=True, help="Identifiant du projet")
    p_analyze.add_argument("--question", required=True, help="Question / angle de l'analyse")
    p_analyze.add_argument("--r1", help="Chemin d'un fichier R1 Dashboard (.xlsx)")
    p_analyze.add_argument("--inputs-json", help="Chemin d'un JSON ValueTrackingInputs (ignoré si --r1 fourni)")
    p_analyze.add_argument("--cut", help="Coupe cible YYYY.MM (défaut : dernière coupe disponible)")
    p_analyze.add_argument("--client-name", help="Nom du client à masquer avant toute lecture externe")
    p_analyze.add_argument(
        "--mode",
        choices=["cartographie", "kpi", "derives", "valeur_realisee"],
        default="cartographie",
    )
    p_analyze.add_argument("--seuil", type=float, help="Seuil d'alerte en %% — mode 'derives'")
    p_analyze.set_defaults(func=_cmd_analyze)

    p_record = sub.add_parser(
        "record", help="Étape 2 : fusionne la narration rédigée par l'agent appelant et écrit l'historique"
    )
    p_record.add_argument("--state", required=True, help="Chemin du state_file retourné par 'analyze'")
    group = p_record.add_mutually_exclusive_group(required=True)
    group.add_argument("--narrative-json", help="Narration au format JSON, inline")
    group.add_argument("--narrative-file", help="Chemin d'un fichier JSON contenant la narration")
    p_record.set_defaults(func=_cmd_record)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
