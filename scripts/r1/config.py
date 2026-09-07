"""Config statique du format R1 Dashboard — connaissance du FORMAT, pas du fichier."""

# Champs canoniques obligatoires (fail-loud si non résolus)
REQUIRED_FIELDS = [
    "to_date_period", "to_date_poc", "to_date_rev_cumul",
    "perf_total_fac", "perf_cbc", "perf_ibc",
    "wbs_label", "wbs_margin",
]

R1_STATIC_CONFIG = {
    # Candidats d'onglets (match insensible casse/espaces)
    "sheets": {
        "performance": ["Performance"],
        "to_date":     ["To Date"],
        "wbs":         ["WBS Summary"],
        "this_year":   ["This Year"],
    },

    # Blocklist PII — tout onglet dont le nom contient un de ces mots est refusé
    "pii_blocklist": ["Time Bookings", "Work Units", "Employee name", "HR"],

    # Marqueurs pour ancrer la ligne d'entête (scanner ≤ header_scan_rows)
    "header_markers": {
        "performance": ["CBC", "Variance FAC - CBC"],
        "to_date":     ["PoC%"],
        "wbs":         ["WBS Element", "Margin"],
    },

    # Synonymes par champ canonique (heuristique de matching)
    # Clé = champ canonique, valeur = liste de synonymes (insensibles casse/accents)
    "synonyms": {
        # To Date
        "to_date_period":      [],              # pas de label → position uniquement
        "to_date_costs_cumul": ["total"],       # ambiguë → position si multi-match
        "to_date_poc":         ["poc%", "poc", "% avancement", "percentage of completion"],
        "to_date_rev_cumul":   ["total"],       # ambiguë → position si multi-match
        "to_date_margin_cumul":["total"],       # ambiguë → position si multi-match
        # Performance
        "perf_total_fac":      ["total", "fac", "forecast at completion"],
        "perf_actuals_to_date":["actuals to date", "actuals cumul"],
        "perf_cbc":            ["cbc", "contract budget cost", "budget contractuel"],
        "perf_ibc":            ["ibc", "of which, ibc", "internal budget", "budget interne"],
        "perf_var_cbc":        ["variance fac - cbc", "ecart cbc", "variance cbc"],
        "perf_var_ibc":        ["variance fac - ibc", "ecart ibc", "variance ibc"],
        # WBS
        "wbs_label":           [],              # pas de label → position uniquement
        "wbs_margin":          ["margin", "marge"],
        "wbs_costs":           ["costs", "coûts", "couts"],
    },

    # Position fallback pour les colonnes sans label ou ambiguës
    # Calées sur le fichier d'exemple, ajustables si un autre R1 diverge
    "position_fallback": {
        "to_date_period":      {"sheet": "to_date",     "index": 0},
        "to_date_costs_cumul": {"sheet": "to_date",     "index": 1},
        "to_date_poc":         {"sheet": "to_date",     "index": 2},
        "to_date_rev_cumul":   {"sheet": "to_date",     "index": 8},
        "to_date_margin_cumul":{"sheet": "to_date",     "index": 12},
        "perf_total_fac":      {"sheet": "performance", "index": 9},
        "perf_actuals_to_date":{"sheet": "performance", "index": 7},
        "perf_cbc":            {"sheet": "performance", "index": 10},
        "perf_ibc":            {"sheet": "performance", "index": 11},
        "wbs_label":           {"sheet": "wbs",         "index": 1},
        "wbs_margin":          {"sheet": "wbs",         "index": 17},
        "wbs_costs":           {"sheet": "wbs",         "index": 16},
    },

    # Lignes KPI à extraire de Performance (libellés col 0, après entête)
    # À vérifier sur le fichier réel utilisé
    "kpi_rows":  ["REVENUE", "COSTS", "MARGIN"],

    # Associe chaque ligne KPI Performance au champ cumulé To Date correspondant,
    # pour le réalisé à la coupe (ValueKPI.realise_cumul) — varie selon la coupe,
    # contrairement à prevu (budget IBC) et realise (FAC) qui sont figés.
    "kpi_cumul_map": {
        "REVENUE": "to_date_rev_cumul",
        "COSTS":   "to_date_costs_cumul",
        "MARGIN":  "to_date_margin_cumul",
    },

    # Lignes de coûts détaillés → ValueTrackingInputs.couts
    "cost_rows": {
        "Costs Personnel":          "OPEX",
        "Costs Subcos":             "OPEX",
        "Costs Outsourced Service": "OPEX",
        "Costs Travel":             "OPEX",
        "Costs Other":              "OPEX",
        "Costs of Goods Sold":      "OPEX",
    },

    # Mapping métier prévu/réalisé (⚠️ à valider selon le contexte du fichier)
    # Choix possible : "perf_ibc" ou "perf_cbc" pour prevu ; "perf_total_fac" ou "perf_actuals_to_date" pour realise
    "mapping": {
        "prevu":  "perf_ibc",       # budget interne — alternative : "perf_cbc"
        "realise":"perf_total_fac", # FAC total projet
        "ecart":  "perf_var_ibc",   # variance pré-calculée (recoupée)
    },

    # Formule earned-value (à valider) :
    # ecart_earned_value = to_date_rev_cumul(cut) - perf_cbc(revenue_label) * poc(cut)
    # Vérification : rev_cumul(coupe) - cbc_budget * poc = variance earned_value
    "earned_value_formula": "realise_cumul - prevu * poc",

    # Libellé de la ligne de revenus dans Performance (pour la formule earned-value)
    # ⚠️ À vérifier sur le vrai fichier si ce libellé diffère
    "revenue_label": "REVENUE",

    # Modèle LLM de secours (petit modèle — classification uniquement, PAS de PII envoyée)
    "llm_model": "gpt-4.1-mini",

    # Code WBS : colonne (index 0-based) et préfixe de filtrage
    "wbs_code_col": 0,
    "wbs_code_prefix": "WBS-",  # Préfixe attendu pour les codes WBS (configurable)

    # Cadence découverte des coupes
    "cadence": "annuelle",          # annuelle | trimestrielle | mensuelle

    # Pareto 80/20
    "pareto": {
        "seuil":     0.8,
        "dimension": "wbs_margin",  # marge par lot (Project To Date)
        "groupby":   "wbs_label",   # clé = nom du lot
    },

    # Divers
    "tolerance_variance": 0.01,
    "devise": "K EUR",
    "header_scan_rows": 20,
}
