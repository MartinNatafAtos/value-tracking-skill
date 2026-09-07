"""Tests pour LocalHistoryStore — fichier JSON, idempotence par coupe."""

from scripts.history_store import LocalHistoryStore


def test_upsert_puis_lecture(tmp_path):
    store = LocalHistoryStore(path=str(tmp_path / "history.json"))
    doc_id = store.upsert_analyse(
        project="alpha", synthese="Synthèse test", ecarts=["CA"],
        root_causes=["Cause A"], recommandations=[{"qui": "PMO", "quoi": "X", "quand": "Q1", "impact_attendu": "+5%"}],
        sources=[], cut="2025.12",
    )
    records = store.get_historique("alpha", top_k=5)
    assert len(records) == 1
    assert records[0]["id"] == doc_id
    assert records[0]["synthese"] == "Synthèse test"
    assert records[0]["ecarts"] == ["CA"]


def test_idempotence_meme_coupe(tmp_path):
    """Deux upserts avec la même coupe → même id, remplace le précédent (pas de doublon)."""
    store = LocalHistoryStore(path=str(tmp_path / "history.json"))
    id1 = store.upsert_analyse(
        project="alpha", synthese="Run 1", ecarts=[], root_causes=[], recommandations=[], sources=[], cut="2025.12",
    )
    id2 = store.upsert_analyse(
        project="alpha", synthese="Run 2", ecarts=[], root_causes=[], recommandations=[], sources=[], cut="2025.12",
    )
    assert id1 == id2
    records = store.get_historique("alpha")
    assert len(records) == 1
    assert records[0]["synthese"] == "Run 2"


def test_coupes_differentes_deux_entrees(tmp_path):
    store = LocalHistoryStore(path=str(tmp_path / "history.json"))
    store.upsert_analyse(project="alpha", synthese="S1", ecarts=[], root_causes=[], recommandations=[], sources=[], cut="2024.12")
    store.upsert_analyse(project="alpha", synthese="S2", ecarts=[], root_causes=[], recommandations=[], sources=[], cut="2025.12")
    records = store.get_historique("alpha", top_k=5)
    assert len(records) == 2


def test_isolation_par_projet(tmp_path):
    store = LocalHistoryStore(path=str(tmp_path / "history.json"))
    store.upsert_analyse(project="alpha", synthese="A", ecarts=[], root_causes=[], recommandations=[], sources=[], cut="2025.12")
    store.upsert_analyse(project="beta", synthese="B", ecarts=[], root_causes=[], recommandations=[], sources=[], cut="2025.12")
    assert len(store.get_historique("alpha")) == 1
    assert len(store.get_historique("beta")) == 1


def test_update_reco_statut(tmp_path):
    store = LocalHistoryStore(path=str(tmp_path / "history.json"))
    doc_id = store.upsert_analyse(
        project="alpha", synthese="S", ecarts=[], root_causes=[], recommandations=[], sources=[], cut="2025.12",
    )
    store.update_reco_statut(doc_id, "fait")
    records = store.get_historique("alpha")
    assert records[0]["statut_reco"] == "fait"


def test_fichier_absent_retourne_liste_vide(tmp_path):
    store = LocalHistoryStore(path=str(tmp_path / "inexistant.json"))
    assert store.get_historique("alpha") == []
