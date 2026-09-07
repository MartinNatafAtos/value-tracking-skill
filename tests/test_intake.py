"""Tests pour le manifeste d'intake — résolution déterministe, pas de LLM."""

from scripts.intake import MANIFESTE, resoudre_manifeste


def test_deux_obligatoires_dans_le_manifeste():
    obligatoires = [m for m in MANIFESTE if m.obligatoire]
    assert len(obligatoires) == 2


def test_kpi_et_couts_manquants_bloquent():
    manquants, obligatoires_manquants, peut_partiel = resoudre_manifeste(
        inputs_kpi_ok=False, inputs_couts_ok=False, docs_presents=False
    )
    assert obligatoires_manquants is True
    assert peut_partiel is False
    assert sum(1 for m in manquants if m.obligatoire) == 2


def test_obligatoires_ok_docs_absents_mode_partiel():
    manquants, obligatoires_manquants, peut_partiel = resoudre_manifeste(
        inputs_kpi_ok=True, inputs_couts_ok=True, docs_presents=False
    )
    assert obligatoires_manquants is False
    assert peut_partiel is True
    assert all(not m.obligatoire for m in manquants)
    assert len(manquants) == 3  # les 3 documents recommandés


def test_tout_present_aucun_manquant():
    manquants, obligatoires_manquants, peut_partiel = resoudre_manifeste(
        inputs_kpi_ok=True, inputs_couts_ok=True, docs_presents=True
    )
    assert manquants == []
    assert obligatoires_manquants is False
    assert peut_partiel is False


def test_seul_kpi_manquant():
    manquants, obligatoires_manquants, _ = resoudre_manifeste(
        inputs_kpi_ok=False, inputs_couts_ok=True, docs_presents=True
    )
    assert obligatoires_manquants is True
    assert len(manquants) == 1
    assert "KPI" in manquants[0].element
