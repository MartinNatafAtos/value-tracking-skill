"""Tests pour ValueTrackingAgent — flow en deux étapes (prepare / record),
sans aucun appel LLM interne. ``anonymize`` est monkeypatché en identité pour
garder ces tests rapides et sans dépendance au modèle NLP de Presidio."""

from unittest.mock import MagicMock

import scripts.agent as agent_module
from scripts.config import Config
from scripts.models import AnalyzeRequest, Cout, ValueKPI, ValueTrackingInputs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_request(with_inputs: bool = True, project: str = "projet-alpha") -> AnalyzeRequest:
    inputs = None
    if with_inputs:
        inputs = ValueTrackingInputs(
            kpi_valeur=[
                ValueKPI(libelle="CA réalisé", prevu=500_000.0, realise=420_000.0),
                ValueKPI(libelle="Productivité", prevu=20.0, realise=18.0),
            ],
            couts=[Cout(type="CAPEX", libelle="Infra", montant=150_000.0)],
        )
    return AnalyzeRequest(
        question="Analyse les bénéfices du projet Alpha",
        project=project,
        inputs=inputs,
    )


def make_agent(monkeypatch, tmp_path, docs_presents: bool = True, chunks=None, historique=None):
    monkeypatch.setattr(Config, "STATE_DIR", str(tmp_path / "state"))

    agent = agent_module.ValueTrackingAgent.__new__(agent_module.ValueTrackingAgent)
    agent.reader = MagicMock()
    agent.reader.get_historique.return_value = historique if historique is not None else []
    agent.writer = MagicMock()
    agent.writer.upsert_analyse.return_value = "fake-id"

    monkeypatch.setattr(agent_module, "_documents_presents_local", lambda project: docs_presents)
    monkeypatch.setattr(agent_module, "collect_extracts", lambda project, docs_dir: list(chunks or []))
    monkeypatch.setattr(agent_module, "anonymize", lambda text, client_names=None: text)
    monkeypatch.setattr(agent_module, "read_memory", lambda project: [])
    monkeypatch.setattr(agent_module, "write_recap", lambda **kwargs: "")
    return agent


# ---------------------------------------------------------------------------
# Intake — obligatoires manquants (aucune narration nécessaire)
# ---------------------------------------------------------------------------

class TestIntake:
    def test_statut_intake_quand_kpi_manquant(self, monkeypatch, tmp_path):
        agent = make_agent(monkeypatch, tmp_path, docs_presents=False)
        result = agent.prepare(make_request(with_inputs=False))
        assert result["statut"] == "intake"
        assert result["sources"] == []
        assert "state_file" not in result

    def test_requete_documents_contient_obligatoires(self, monkeypatch, tmp_path):
        agent = make_agent(monkeypatch, tmp_path, docs_presents=False)
        result = agent.prepare(make_request(with_inputs=False))
        obligatoires = [m for m in result["requete_documents"] if m["obligatoire"]]
        assert len(obligatoires) >= 2

    def test_peut_produire_partiel_false_en_intake(self, monkeypatch, tmp_path):
        agent = make_agent(monkeypatch, tmp_path, docs_presents=False)
        result = agent.prepare(make_request(with_inputs=False))
        assert result["peut_produire_partiel"] is False


# ---------------------------------------------------------------------------
# prepare() -> besoin_narratif -> record()
# ---------------------------------------------------------------------------

class TestPrepareRecord:
    _CHUNK = {
        "content": "Les retards sont liés à la migration infrastructure.",
        "file": "rapport_q1.pdf",
        "page": 3,
        "title": "Rapport Q1",
        "url": None,
        "score": 0.92,
    }

    def test_prepare_retourne_besoin_narratif_avec_state_file(self, monkeypatch, tmp_path):
        agent = make_agent(monkeypatch, tmp_path, docs_presents=True, chunks=[self._CHUNK])
        result = agent.prepare(make_request())

        assert result["statut"] == "besoin_narratif"
        assert "instructions" in result and result["instructions"]
        assert "context" in result and "CALCULS KPI" in result["context"]
        assert result["state_file"]

        import os
        assert os.path.exists(result["state_file"])

    def test_record_construit_le_livrable(self, monkeypatch, tmp_path):
        agent = make_agent(monkeypatch, tmp_path, docs_presents=True, chunks=[self._CHUNK])
        prepared = agent.prepare(make_request())

        narrative = {
            "synthese": "Le projet Alpha présente un écart de réalisation de -16%.",
            "root_causes": ["Retard de migration", "Sous-adoption"],
            "recommandations": [
                {"qui": "PMO", "quoi": "Accélérer la migration", "quand": "T3 2026", "impact_attendu": "+5% productivité"}
            ],
        }
        response = agent.record(prepared["state_file"], narrative)

        assert response.statut == "complet"
        assert response.deliverable is not None
        assert len(response.deliverable.kpi_table) == 2
        assert len(response.deliverable.ecarts) >= 1
        assert response.deliverable.root_causes == ["Retard de migration", "Sous-adoption"]
        assert len(response.deliverable.recommandations) == 1
        assert response.answer == narrative["synthese"]

    def test_chiffres_du_state_jamais_modifies_par_la_narration(self, monkeypatch, tmp_path):
        from scripts.compute import analyse_8020, compute_variance

        agent = make_agent(monkeypatch, tmp_path, docs_presents=True, chunks=[self._CHUNK])
        request = make_request()
        prepared = agent.prepare(request)

        # Une narration qui tente d'insérer des chiffres n'a aucune prise sur kpi_table/ecarts :
        # ils viennent exclusivement du state_file écrit par prepare().
        narrative = {"synthese": "Peu importe", "root_causes": [], "recommandations": []}
        response = agent.record(prepared["state_file"], narrative)

        kpi_dicts = [k.model_dump() for k in request.inputs.kpi_valeur]
        expected_table = compute_variance(kpi_dicts)
        expected_ecarts = analyse_8020(expected_table)

        assert response.deliverable.kpi_table == expected_table
        assert response.deliverable.ecarts == expected_ecarts

    def test_sources_toujours_presentes_et_dedupliquees(self, monkeypatch, tmp_path):
        chunks = [
            {"content": "A", "file": "doc.pdf", "page": 1, "title": "Doc", "url": None, "score": 0.9},
            {"content": "B", "file": "doc.pdf", "page": 1, "title": "Doc", "url": None, "score": 0.8},
        ]
        agent = make_agent(monkeypatch, tmp_path, docs_presents=True, chunks=chunks)
        prepared = agent.prepare(make_request())
        response = agent.record(prepared["state_file"], {"synthese": "OK", "root_causes": [], "recommandations": []})
        assert isinstance(response.sources, list)
        assert len(response.sources) == 1

    def test_writer_appele_a_l_enregistrement(self, monkeypatch, tmp_path):
        agent = make_agent(monkeypatch, tmp_path, docs_presents=True)
        prepared = agent.prepare(make_request())
        agent.record(prepared["state_file"], {"synthese": "OK", "root_causes": [], "recommandations": []})
        agent.writer.upsert_analyse.assert_called_once()

    def test_record_tolere_narration_en_chaine_json_avec_fences(self, monkeypatch, tmp_path):
        agent = make_agent(monkeypatch, tmp_path, docs_presents=True)
        prepared = agent.prepare(make_request())

        raw = '```json\n{"synthese": "OK texte", "root_causes": [], "recommandations": []}\n```'
        response = agent.record(prepared["state_file"], raw)
        assert response.answer == "OK texte"


# ---------------------------------------------------------------------------
# Mode partiel — obligatoires OK, documents recommandés absents
# ---------------------------------------------------------------------------

class TestModePartiel:
    def test_livrable_produit_en_mode_partiel(self, monkeypatch, tmp_path):
        agent = make_agent(monkeypatch, tmp_path, docs_presents=False)
        prepared = agent.prepare(make_request())
        assert prepared["statut"] == "besoin_narratif"
        assert prepared["peut_produire_partiel"] is True if "peut_produire_partiel" in prepared else True

        response = agent.record(
            prepared["state_file"],
            {"synthese": "Analyse partielle.", "root_causes": [], "recommandations": []},
        )
        assert response.statut == "complet"
        assert response.deliverable is not None
        assert response.peut_produire_partiel is True

    def test_recommandes_manquants_dans_requete_documents(self, monkeypatch, tmp_path):
        agent = make_agent(monkeypatch, tmp_path, docs_presents=False)
        prepared = agent.prepare(make_request())
        response = agent.record(
            prepared["state_file"], {"synthese": "OK", "root_causes": [], "recommandations": []}
        )
        recommandes = [m for m in response.requete_documents if not m.obligatoire]
        assert len(recommandes) >= 1


# ---------------------------------------------------------------------------
# build_context() — donnees_op (cumulés à la coupe)
# ---------------------------------------------------------------------------

class TestBuildContextDonneesOp:
    """build_context() doit exploiter donnees_op (cut-dépendant) sans casser
    la rétro-compatibilité (donnees_op=None/{})."""

    def test_deux_coupes_produisent_des_contextes_distincts(self):
        kpi_table = [{"libelle": "CA", "prevu": 1, "realise": 1, "ecart_pct": 0}]

        donnees_op_2024 = {
            "cut": "2024.12", "poc": 0.93, "couts_cumul": -10621.26,
            "marge_cumul": 2510.06, "rev_cumul": 13131.32, "wbs_pareto": [],
        }
        donnees_op_2025 = {
            "cut": "2025.12", "poc": 0.97, "couts_cumul": -20999.99,
            "marge_cumul": 4200.5, "rev_cumul": 25200.49, "wbs_pareto": [],
        }

        ctx_2024 = agent_module.build_context(kpi_table, [], [], [], donnees_op=donnees_op_2024)
        ctx_2025 = agent_module.build_context(kpi_table, [], [], [], donnees_op=donnees_op_2025)

        assert "2024.12" in ctx_2024
        assert "-10621.26" in ctx_2024
        assert "2510.06" in ctx_2024

        assert "2025.12" in ctx_2025
        assert "-20999.99" in ctx_2025
        assert "4200.5" in ctx_2025

        assert ctx_2024 != ctx_2025
        assert "-10621.26" not in ctx_2025
        assert "-20999.99" not in ctx_2024

    def test_section_wbs_etiquetee_photo(self):
        donnees_op = {
            "cut": "2024.12",
            "wbs_pareto": [
                {"libelle": "Lot Infra", "marge": 120.5},
                {"libelle": "Lot Data", "marge": -30.2},
            ],
        }
        ctx = agent_module.build_context([], [], [], [], donnees_op=donnees_op)
        assert "PHOTO" in ctx
        assert "NON cut-dépendante" in ctx
        assert "Lot Infra" in ctx
        assert "Lot Data" in ctx

    def test_donnees_op_none_ne_leve_pas_et_najoute_pas_section(self):
        ctx = agent_module.build_context([], [], [], [], donnees_op=None)
        assert "SITUATION CUMULÉE" not in ctx
        assert "PHOTO" not in ctx

    def test_donnees_op_vide_ne_leve_pas_et_najoute_pas_section(self):
        ctx = agent_module.build_context([], [], [], [], donnees_op={})
        assert "SITUATION CUMULÉE" not in ctx
        assert "PHOTO" not in ctx

    def test_build_context_sans_donnees_op_reste_compatible(self):
        ctx = agent_module.build_context([], [], [], [])
        assert "SITUATION CUMULÉE" not in ctx

    def test_prepare_inclut_donnees_op_dans_le_contexte(self, monkeypatch, tmp_path):
        agent = make_agent(monkeypatch, tmp_path, docs_presents=True)

        donnees_op = {"cut": "2024.12", "poc": 0.9, "couts_cumul": -100.0}
        request = AnalyzeRequest(
            question="Analyse le projet",
            project="projet-alpha",
            inputs=ValueTrackingInputs(
                kpi_valeur=[ValueKPI(libelle="CA", prevu=1.0, realise=1.0)],
                couts=[Cout(type="CAPEX", libelle="Infra", montant=1.0)],
                donnees_op=donnees_op,
            ),
        )

        result = agent.prepare(request)
        assert "2024.12" in result["context"]
        assert "-100.0" in result["context"]
