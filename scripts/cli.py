"""Point d'entrée CLI du skill Value Tracking.

Usage :
    python -m scripts.cli --r1 dashboard.xlsx --project alpha --question "Bilan de valeur"
    python -m scripts.cli --project alpha --question "..." --inputs-json inputs.json
"""

from __future__ import annotations

import argparse
import json
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description="Value Tracking Agent — CLI")
    parser.add_argument("--project", required=True, help="Identifiant du projet")
    parser.add_argument("--question", required=True, help="Question / angle de l'analyse")
    parser.add_argument("--r1", help="Chemin d'un fichier R1 Dashboard (.xlsx)")
    parser.add_argument("--inputs-json", help="Chemin d'un JSON ValueTrackingInputs (ignoré si --r1 fourni)")
    parser.add_argument("--cut", help="Coupe cible YYYY.MM (défaut : dernière coupe disponible)")
    parser.add_argument("--client-name", help="Nom du client à masquer avant tout envoi au LLM")
    parser.add_argument(
        "--mode",
        choices=["cartographie", "kpi", "derives", "valeur_realisee"],
        default="cartographie",
    )
    parser.add_argument("--seuil", type=float, help="Seuil d'alerte en %% — mode 'derives'")
    args = parser.parse_args()

    from .agent import ValueTrackingAgent
    from .config import Config
    from .models import AnalyzeRequest

    try:
        Config.validate()
    except ValueError as exc:
        print(f"Erreur de configuration : {exc}", file=sys.stderr)
        return 1

    inputs = None
    cut = args.cut

    if args.r1:
        import openpyxl

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
    response = agent.run(request)
    print(json.dumps(response.model_dump(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
