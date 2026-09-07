"""Tests pour ValueTrackingAgent — reader/writer/LLM mockés, docs/anonymize monkeypatchés.

Presidio (spaCy) n'est PAS exercé ici : ``anonymize`` est monkeypatché en
identité pour garder ces tests rapides et sans dépendance modèle NLP.
"""

from unittest.mock import MagicMock

import scripts.agent as agent_module
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


def make_agent(monkeypatch, docs_presents: bool = True, chunks=None, historique=None):
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
# Intake — obligatoires manquants
# ---------------------------------------------------------------------------

class TestIntake:
    def test_statut_intake_quand_kpi_manquant(self, monkeypatch):
        agent = make_agent(monkeypatch, docs_presents=False)
        agent._call_llm = MagicMock(return_value={})
        response = agent.run(make_request(with_inputs=False))
        assert response.statut == "intake"
        assert response.deliverable is None
        assert response.sources == []

    def test_requete_documents_contient_obligatoires(self, monkeypatch):
        agent = make_agent(monkeypatch, docs_presents=False)
        agent._call_llm = MagicMock(return_value={})
        response = agent.run(make_request(with_inputs=False))
        obligatoires = [m for m in response.requete_documents if m.obligatoire]
        assert len(obligatoires) >= 2

    def test_peut_produire_partiel_false_en_intake(self, monkeypatch):
        agent = make_agent(monkeypatch, docs_presents=False)
        agent._call_llm = MagicMock(return_value={})
        response = agent.run(make_request(with_inputs=False))
        assert response.peut_produire_partiel is False


# ---------------------------------------------------------------------------
# Mode partiel — obligatoires OK, documents recommandés absents
# ---------------------------------------------------------------------------

class TestModePartiel:
    def test_livrable_produit_en_mode_partiel(self, monkeypatch):
        agent = make_agent(monkeypatch, docs_presents=False)
        agent._call_llm = MagicMock(
            return_value={"synthese": "Analyse partielle.", "root_causes": [], "recommandations": []}
        )
        response = agent.run(make_request())
        assert response.statut == "complet"
        assert response.deliverable is not None
        assert response.peut_produire_partiel is True

    def test_recommandes_manquants_dans_requete_documents(self, monkeypatch):
        agent = make_agent(monkeypatch, docs_presents=False)
        agent._call_llm = MagicMock(
            return_value={"synthese": "OK", "root_causes": [], "recommandations": []}
        )
        response = agent.run(make_request())
        recommandes = [m for m in response.requete_documents if not m.obligatoire]
        assert len(recommandes) >= 1


# ---------------------------------------------------------------------------
# Flow complet
# ---------------------------------------------------------------------------

class TestFlowComplet:
    _CHUNK = {
        "content": "Les retards sont liés à la migration infrastructure.",
        "file": "rapport_q1.pdf",
        "page": 3,
        "title": "Rapport Q1",
        "url": None,
        "score": 0.92,
    }

    def test_deliverable_structure(self, monkeypatch):
        llm_json = {
            "synthese": "Le projet Alpha présente un écart de réalisation de -16%.",
            "root_causes": ["Retard de migration", "Sous-adoption"],
            "recommandations": [
                {"qui": "PMO", "quoi": "Accélérer la migration", "quand": "T3 2026", "impact_attendu": "+5% productivité"}
            ],
        }
        agent = make_agent(monkeypatch, docs_presents=True, chunks=[self._CHUNK])
        agent._call_llm = MagicMock(return_value=llm_json)

        response = agent.run(make_request())

        assert response.deliverable is not None
        assert len(response.deliverable.kpi_table) == 2
        assert len(response.deliverable.ecarts) >= 1
        assert len(response.deliverable.root_causes) == 2
        assert len(response.deliverable.recommandations) == 1

    def test_chiffres_viennent_de_compute(self, monkeypatch):
        from scripts.compute import analyse_8020, compute_variance

        llm_json = {"synthese": "Synthèse test.", "root_causes": ["Cause A"], "recommandations": []}
        agent = make_agent(monkeypatch, docs_presents=True, chunks=[self._CHUNK])
        agent._call_llm = MagicMock(return_value=llm_json)

        request = make_request()
        response = agent.run(request)

        kpi_dicts = [k.model_dump() for k in request.inputs.kpi_valeur]
        expected_table = compute_variance(kpi_dicts)
        expected_ecarts = analyse_8020(expected_table)

        assert response.deliverable.kpi_table == expected_table
        assert response.deliverable.ecarts == expected_ecarts

    def test_sources_toujours_presentes(self, monkeypatch):
        agent = make_agent(monkeypatch, docs_presents=True)
        agent._call_llm = MagicMock(return_value={"synthese": "OK", "root_causes": [], "recommandations": []})
        response = agent.run(make_request())
        assert isinstance(response.sources, list)

    def test_writer_appele(self, monkeypatch):
        agent = make_agent(monkeypatch, docs_presents=True)
        agent._call_llm = MagicMock(return_value={"synthese": "OK", "root_causes": [], "recommandations": []})
        agent.run(make_request())
        agent.writer.upsert_analyse.assert_called_once()

    def test_sources_dedupliquees(self, monkeypatch):
        chunks = [
            {"content": "A", "file": "doc.pdf", "page": 1, "title": "Doc", "url": None, "score": 0.9},
            {"content": "B", "file": "doc.pdf", "page": 1, "title": "Doc", "url": None, "score": 0.8},
        ]
        agent = make_agent(monkeypatch, docs_presents=True, chunks=chunks)
        agent._call_llm = MagicMock(return_value={"synthese": "OK", "root_causes": [], "recommandations": []})
        response = agent.run(make_request())
        assert len(response.sources) == 1


# ---------------------------------------------------------------------------
# donnees_op (cumulés à la coupe) branchés dans le contexte LLM
# ---------------------------------------------------------------------------

class TestDonneesOpDansContexteLLM:
    """_build_llm_context doit exploiter donnees_op (cut-dépendant) sans casser
    la rétro-compatibilité (donnees_op=None/{})."""

    def _agent(self):
        return agent_module.ValueTrackingAgent.__new__(agent_module.ValueTrackingAgent)

    def test_deux_coupes_produisent_des_contextes_distincts(self):
        agent = self._agent()
        kpi_table = [{"libelle": "CA", "prevu": 1, "realise": 1, "ecart_pct": 0}]

        donnees_op_2024 = {
            "cut": "2024.12", "poc": 0.93, "couts_cumul": -10621.26,
            "marge_cumul": 2510.06, "rev_cumul": 13131.32, "wbs_pareto": [],
        }
        donnees_op_2025 = {
            "cut": "2025.12", "poc": 0.97, "couts_cumul": -20999.99,
            "marge_cumul": 4200.5, "rev_cumul": 25200.49, "wbs_pareto": [],
        }

        ctx_2024 = agent._build_llm_context(kpi_table, [], [], [], donnees_op=donnees_op_2024)
        ctx_2025 = agent._build_llm_context(kpi_table, [], [], [], donnees_op=donnees_op_2025)

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
        agent = self._agent()
        donnees_op = {
            "cut": "2024.12",
            "wbs_pareto": [
                {"libelle": "Lot Infra", "marge": 120.5},
                {"libelle": "Lot Data", "marge": -30.2},
            ],
        }
        ctx = agent._build_llm_context([], [], [], [], donnees_op=donnees_op)
        assert "PHOTO" in ctx
        assert "NON cut-dépendante" in ctx
        assert "Lot Infra" in ctx
        assert "Lot Data" in ctx

    def test_donnees_op_none_ne_leve_pas_et_najoute_pas_section(self):
        agent = self._agent()
        ctx = agent._build_llm_context([], [], [], [], donnees_op=None)
        assert "SITUATION CUMULÉE" not in ctx
        assert "PHOTO" not in ctx

    def test_donnees_op_vide_ne_leve_pas_et_najoute_pas_section(self):
        agent = self._agent()
        ctx = agent._build_llm_context([], [], [], [], donnees_op={})
        assert "SITUATION CUMULÉE" not in ctx
        assert "PHOTO" not in ctx

    def test_build_llm_context_sans_donnees_op_reste_compatible(self):
        agent = self._agent()
        ctx = agent._build_llm_context([], [], [], [])
        assert "SITUATION CUMULÉE" not in ctx

    def test_run_passe_donnees_op_au_call_llm(self, monkeypatch):
        agent = make_agent(monkeypatch, docs_presents=True)
        agent._call_llm = MagicMock(return_value={"synthese": "OK", "root_causes": [], "recommandations": []})

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

        agent.run(request)

        call_kwargs = agent._call_llm.call_args.kwargs
        assert call_kwargs.get("donnees_op") == donnees_op
