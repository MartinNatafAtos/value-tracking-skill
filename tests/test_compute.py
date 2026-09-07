"""Tests unitaires pour compute.py — fonctions pures, aucun mock LLM."""

from scripts.compute import compute_variance, analyse_8020


# ---------------------------------------------------------------------------
# compute_variance
# ---------------------------------------------------------------------------

class TestComputeVariance:
    def test_ecart_positif(self):
        kpis = [{"libelle": "CA", "prevu": 100.0, "realise": 120.0}]
        result = compute_variance(kpis)
        assert len(result) == 1
        assert result[0]["ecart_abs"] == 20.0
        assert result[0]["ecart_pct"] == 20.0

    def test_ecart_negatif(self):
        kpis = [{"libelle": "Délai", "prevu": 10.0, "realise": 7.0}]
        result = compute_variance(kpis)
        assert result[0]["ecart_abs"] == -3.0
        assert result[0]["ecart_pct"] == -30.0

    def test_prevu_zero(self):
        """Division par zéro : ecart_pct doit être None."""
        kpis = [{"libelle": "X", "prevu": 0.0, "realise": 5.0}]
        result = compute_variance(kpis)
        assert result[0]["ecart_pct"] is None
        assert result[0]["ecart_abs"] == 5.0

    def test_liste_vide(self):
        assert compute_variance([]) == []

    def test_plusieurs_kpis(self):
        kpis = [
            {"libelle": "A", "prevu": 200.0, "realise": 180.0},
            {"libelle": "B", "prevu": 50.0, "realise": 60.0},
        ]
        result = compute_variance(kpis)
        assert len(result) == 2
        assert result[0]["libelle"] == "A"
        assert result[0]["ecart_abs"] == -20.0
        assert result[1]["ecart_abs"] == 10.0

    def test_preserve_libelle(self):
        kpis = [{"libelle": "Bénéfices nets", "prevu": 500.0, "realise": 500.0}]
        result = compute_variance(kpis)
        assert result[0]["libelle"] == "Bénéfices nets"
        assert result[0]["ecart_abs"] == 0.0
        assert result[0]["ecart_pct"] == 0.0

    def test_cout_depassement_pct_negatif(self):
        """Coût stocké négatif, dépassement (réalisé plus négatif) → % NÉGATIF (défavorable)."""
        kpis = [{"libelle": "COSTS", "prevu": -100.0, "realise": -110.0}]
        result = compute_variance(kpis)
        assert result[0]["ecart_abs"] == -10.0
        assert result[0]["ecart_pct"] == -10.0  # dépassement = défavorable = négatif

    def test_cout_economie_pct_positif(self):
        """Coût stocké négatif, économie (réalisé moins négatif) → % POSITIF (favorable)."""
        kpis = [{"libelle": "COSTS", "prevu": -100.0, "realise": -90.0}]
        result = compute_variance(kpis)
        assert result[0]["ecart_abs"] == 10.0
        assert result[0]["ecart_pct"] == 10.0  # économie = favorable = positif

    def test_realise_cumul_present(self):
        """realise_cumul fourni en entrée doit ressortir tel quel dans le dict."""
        kpis = [{"libelle": "CA", "prevu": 100.0, "realise": 120.0, "realise_cumul": 2320.64}]
        result = compute_variance(kpis)
        assert result[0]["realise_cumul"] == 2320.64

    def test_realise_cumul_absent(self):
        """Clé realise_cumul absente en entrée → None en sortie, pas d'exception."""
        kpis = [{"libelle": "CA", "prevu": 100.0, "realise": 120.0}]
        result = compute_variance(kpis)
        assert result[0]["realise_cumul"] is None

    def test_ecart_abs_base_sur_realise_fac_pas_cumul(self):
        """L'écart reste calculé sur realise (FAC) vs prevu, jamais sur realise_cumul."""
        kpis = [{"libelle": "CA", "prevu": 100.0, "realise": 120.0, "realise_cumul": 9999.0}]
        result = compute_variance(kpis)
        assert result[0]["ecart_abs"] == 20.0
        assert result[0]["ecart_pct"] == 20.0


# ---------------------------------------------------------------------------
# analyse_8020
# ---------------------------------------------------------------------------

class TestAnalyse8020:
    def test_selection_80pct(self):
        """Les leviers sélectionnés doivent couvrir ≥ 80 % de l'écart total absolu."""
        kpi_table = [
            {"libelle": "A", "ecart_abs": 60.0},
            {"libelle": "B", "ecart_abs": 20.0},
            {"libelle": "C", "ecart_abs": 10.0},
            {"libelle": "D", "ecart_abs": 10.0},
        ]
        leviers = analyse_8020(kpi_table)
        # A (60) + B (20) = 80/100 = 80% → doit s'arrêter après B
        assert leviers == ["A", "B"]

    def test_un_seul_levier_couvre_80(self):
        kpi_table = [
            {"libelle": "Majeur", "ecart_abs": 90.0},
            {"libelle": "Mineur", "ecart_abs": 10.0},
        ]
        leviers = analyse_8020(kpi_table)
        assert leviers == ["Majeur"]

    def test_ecart_total_nul(self):
        kpi_table = [
            {"libelle": "A", "ecart_abs": 0.0},
            {"libelle": "B", "ecart_abs": 0.0},
        ]
        assert analyse_8020(kpi_table) == []

    def test_liste_vide(self):
        assert analyse_8020([]) == []

    def test_trie_par_valeur_absolue(self):
        """Un écart négatif important doit être considéré en valeur absolue."""
        kpi_table = [
            {"libelle": "Petit positif", "ecart_abs": 5.0},
            {"libelle": "Grand negatif", "ecart_abs": -80.0},
            {"libelle": "Moyen", "ecart_abs": 15.0},
        ]
        leviers = analyse_8020(kpi_table)
        # Grand negatif (80) sur total 100 = 80% → seul levier
        assert leviers[0] == "Grand negatif"

    def test_tous_egaux(self):
        """Tous égaux : on prend le minimum nécessaire pour couvrir 80%."""
        kpi_table = [
            {"libelle": f"K{i}", "ecart_abs": 10.0} for i in range(10)
        ]
        leviers = analyse_8020(kpi_table)
        # Chaque levier = 10% du total → besoin de 8 pour couvrir 80%
        assert len(leviers) == 8
