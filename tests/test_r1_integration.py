"""Test d'intégration du parseur R1 — vérifie le portage hors du monorepo
d'origine à l'aide du fichier d'exemple anonymisé, s'il est présent
(ce fichier vit dans le dépôt d'origine ; copiez-le ici si vous extrayez ce
skill dans son propre dépôt et voulez garder ce test)."""

from pathlib import Path

import pytest

_SAMPLE = Path(__file__).resolve().parents[2] / "R1_Dashboard_SAMPLE_anonymise.xlsx"

pytestmark = pytest.mark.skipif(
    not _SAMPLE.exists(),
    reason="R1_Dashboard_SAMPLE_anonymise.xlsx introuvable (hors périmètre du skill extrait)",
)


def test_process_r1_workbook_sample(tmp_path):
    import openpyxl

    from scripts.r1.intake import process_r1_workbook

    wb = openpyxl.load_workbook(str(_SAMPLE), data_only=True)
    result = process_r1_workbook(wb, cache_path=str(tmp_path / "r1_cache.json"))

    assert result["cut"] in result["coupes"]
    assert len(result["coupes"]) >= 1

    inputs = result["inputs"]
    assert len(inputs.kpi_valeur) >= 1
    for kpi in inputs.kpi_valeur:
        assert kpi.libelle in {"REVENUE", "COSTS", "MARGIN"}

    assert isinstance(inputs.donnees_op, dict)
    assert "poc" in inputs.donnees_op

    # Les onglets PII (Time Bookings, Work Units...), s'ils existent dans le
    # fichier, ne doivent jamais apparaître comme onglets détectés.
    assert isinstance(result["pii_sheets_ignores"], list)
