"""Résolveur de mapping R1 Dashboard — heuristique → LLM fallback → cache.

``openai_client`` accepte indifféremment un client ``openai.OpenAI`` ou
``openai.AzureOpenAI`` : les deux exposent la même interface
``.chat.completions.create(...)``, ce qui rend ce résolveur agnostique du
provider LLM utilisé pour le fallback.
"""

import hashlib
import json
import logging
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def detect_pii_sheets(sheetnames, config: dict) -> list:
    """Retourne les onglets contenant de la PII (selon ``pii_blocklist``).

    Ces onglets (Time Bookings, Work Units, Employee name, HR…) NE SONT JAMAIS
    LUS : ils sont exclus du traitement. On ne refuse plus le fichier entier —
    on saute l'onglet et on traite les onglets R1 financiers normalement.
    """
    blocklist = config.get("pii_blocklist", [])
    detected = []
    for sheet_name in sheetnames:
        name_lower = sheet_name.lower()
        if any(blocked.lower() in name_lower for blocked in blocklist):
            detected.append(sheet_name)
    return detected


class R1ConfigError(Exception):
    """Champ obligatoire introuvable — cite le champ canonique."""


class R1Resolver:
    """
    Résolveur de mapping pour le format R1 Dashboard.

    Pipeline :
    1. Garde PII : détecter les onglets PII et les EXCLURE (jamais lus)
    2. Détecter les onglets R1 (vérifier signature d'un fichier R1 valide)
    3. Calculer la signature de format = sha256(sorted_sheet_names + header_labels)
    4. Si signature en cache → retourner le mapping en cache
    5. Ancrer les entêtes (marqueurs)
    6. Mapping heuristique (synonymes)
    7. LLM fallback sur les lacunes (si openai_client fourni)
    8. Valider que les champs obligatoires sont résolus (sinon R1ConfigError)
    9. Persister en cache
    10. Retourner le mapping
    """

    def __init__(
        self,
        config: dict,
        openai_client=None,
        cache_path: str = ".r1_mapping_cache.json"
    ):
        """
        Args:
            config: R1_STATIC_CONFIG ou équivalent
            openai_client: Client OpenAI-compatible (optionnel)
            cache_path: Chemin du fichier de cache JSON
        """
        self.config = config
        self.openai_client = openai_client
        self.cache_path = Path(cache_path)
        self._cache = self._load_cache()
        self.pii_sheets_ignores = []  # onglets PII détectés + exclus au dernier resolve()

    def _load_cache(self) -> dict:
        """Charge le cache depuis le disque."""
        if self.cache_path.exists():
            try:
                with open(self.cache_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Erreur lecture cache {self.cache_path}: {e}")
        return {}

    def _save_cache(self) -> None:
        """Persiste le cache sur disque."""
        try:
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump(self._cache, f, indent=2, ensure_ascii=False)
            logger.info(f"Cache persisté dans {self.cache_path}")
        except Exception as e:
            logger.error(f"Erreur écriture cache {self.cache_path}: {e}")

    def resolve(self, wb) -> dict:
        """
        Retourne le mapping résolu : {champ_canonique: {"sheet": str, "col_index": int}}.

        Args:
            wb: openpyxl.Workbook (chargé avec data_only=True)

        Returns:
            Mapping résolu, ex: {"to_date_poc": {"sheet": "to_date", "col_index": 2}}

        Note : les onglets PII sont exclus (jamais lus), pas un motif de refus.

        Raises:
            R1ConfigError: Si champ obligatoire non résolu ou onglets R1 absents
        """
        # 1. Garde PII : détecte + EXCLUT les onglets PII (ne refuse plus le fichier)
        self.pii_sheets_ignores = self._check_pii(wb)

        # 2. Détecter les onglets (les onglets PII sont exclus du scan)
        sheet_mapping = self._detect_sheets(wb)
        logger.info(f"Onglets détectés: {sheet_mapping}")

        # 3. Ancrer les entêtes
        header_rows = self._anchor_headers(wb, sheet_mapping)
        logger.info(f"Entêtes ancrées: {header_rows}")

        # 4. Calculer la signature
        signature = self._format_signature(wb, header_rows)
        logger.info(f"Signature format: {signature[:16]}...")

        # 5. Cache hit ?
        if signature in self._cache:
            logger.info("Mapping trouvé en cache")
            mapping = self._cache[signature]
            # Valider que les champs obligatoires sont présents
            self._validate_required(mapping)
            return mapping

        # 6. Mapping heuristique
        mapping = {}
        missing = {}
        for sheet_key, ws_name in sheet_mapping.items():
            if ws_name is None:
                continue
            # Ignorer les onglets sans entête ancrée (pas de marqueurs)
            if sheet_key not in header_rows:
                continue
            ws = wb[ws_name]
            header_row = header_rows[sheet_key]
            result = self._heuristic_map(ws, header_row, sheet_key)
            mapping.update(result["resolved"])
            missing.update(result["missing"])

        logger.info(f"Mapping heuristique: {len(mapping)} résolus, {len(missing)} lacunes")

        # 7. LLM fallback sur les lacunes
        if missing and self.openai_client:
            logger.info(f"Appel LLM pour {len(missing)} champs manquants")
            llm_result = self._llm_fallback(wb, sheet_mapping, header_rows, missing)
            mapping.update(llm_result)
        elif missing and not self.openai_client:
            logger.warning(f"{len(missing)} champs non résolus et pas de LLM disponible, utilisation des fallbacks de position")
            # Utiliser les fallbacks de position
            for field_name in missing:
                if field_name in self.config["position_fallback"]:
                    fb = self.config["position_fallback"][field_name]
                    mapping[field_name] = {"sheet": fb["sheet"], "col_index": fb["index"]}

        # 8. Valider les champs obligatoires
        self._validate_required(mapping)

        # 9. Persister en cache
        self._cache[signature] = mapping
        self._save_cache()

        # 10. Retourner
        return mapping

    def _check_pii(self, wb) -> list:
        """Détecte les onglets PII et les EXCLUT du traitement (ne lève plus).

        Les onglets PII (Time Bookings, Work Units…) ne sont jamais lus : ils
        sont recensés ici, loggés, puis exclus de la détection/lecture. Le reste
        du fichier (onglets R1 financiers) est traité normalement.

        Returns:
            La liste des noms d'onglets PII ignorés.
        """
        pii = detect_pii_sheets(wb.sheetnames, self.config)
        if pii:
            logger.warning(
                "Onglet(s) PII détecté(s) et IGNORÉ(s) (jamais lus) : %s", pii
            )
        return pii

    def _detect_sheets(self, wb) -> dict:
        """
        Détecte les onglets R1 présents dans le workbook.

        Returns:
            Mapping {sheet_key: sheet_name_réel ou None}
        """
        result = {}
        # Exclure les onglets PII : ils ne doivent jamais être détectés ni lus.
        pii = set(detect_pii_sheets(wb.sheetnames, self.config))
        available = [s for s in wb.sheetnames if s not in pii]

        for key, candidates in self.config["sheets"].items():
            found = None
            for candidate in candidates:
                # Match insensible casse/espaces
                for sheet_name in available:
                    if self._normalize(sheet_name) == self._normalize(candidate):
                        found = sheet_name
                        break
                if found:
                    break
            result[key] = found

        # Vérifier qu'au moins performance, to_date, wbs sont présents
        required_sheets = ["performance", "to_date", "wbs"]
        missing_sheets = [k for k in required_sheets if result.get(k) is None]
        if missing_sheets:
            raise R1ConfigError(
                f"Onglets R1 manquants: {missing_sheets}. "
                f"Format R1 Dashboard invalide."
            )

        return result

    def _anchor_headers(self, wb, sheet_mapping: dict) -> dict:
        """
        Ancre les lignes d'entête pour chaque onglet détecté.

        Returns:
            {sheet_key: row_index_0based}
        """
        result = {}
        max_scan = self.config.get("header_scan_rows", 20)

        for sheet_key, ws_name in sheet_mapping.items():
            if ws_name is None:
                continue
            markers = self.config["header_markers"].get(sheet_key, [])
            if not markers:
                logger.warning(f"Aucun marqueur défini pour {sheet_key}")
                continue

            ws = wb[ws_name]
            row_idx = self._find_header_row(ws, markers, max_scan)
            if row_idx is None:
                # Identifier les champs manquants liés à ce sheet
                affected_fields = [f for f in self.config.get("synonyms", {}).keys()
                                 if self._get_sheet_for_field(f) == sheet_key]
                raise R1ConfigError(
                    f"Entête introuvable dans '{ws_name}' (marqueurs: {markers}). "
                    f"Champs affectés: {affected_fields}. "
                    f"Scanné {max_scan} premières lignes."
                )
            result[sheet_key] = row_idx

        return result

    def _find_header_row(self, ws, markers: List[str], max_rows: int = 20) -> Optional[int]:
        """
        Retourne l'index (0-based) de la ligne contenant TOUS les marqueurs.

        Args:
            ws: openpyxl.worksheet.worksheet.Worksheet
            markers: Liste de marqueurs à trouver (tous doivent être présents)
            max_rows: Nombre max de lignes à scanner

        Returns:
            Index 0-based de la ligne d'entête, ou None si non trouvée
        """
        for row_idx in range(max_rows):
            row = list(ws.iter_rows(min_row=row_idx+1, max_row=row_idx+1, values_only=True))[0]
            row_str = [str(c).strip() if c else "" for c in row]

            # Tous les marqueurs doivent être présents
            if all(any(self._normalize(marker) in self._normalize(cell) for cell in row_str) for marker in markers):
                return row_idx

        return None

    def _heuristic_map(self, ws, header_row: int, sheet_key: str) -> dict:
        """
        Matching heuristique synonymes → {champ: col_index}.

        Returns:
            {"resolved": {champ: {"sheet": sheet_key, "col_index": idx}},
             "missing": {champ: raison}}
        """
        resolved = {}
        missing = {}

        # Lire la ligne d'entête
        header_cells = list(ws.iter_rows(min_row=header_row+1, max_row=header_row+1, values_only=True))[0]
        headers = [self._normalize(str(c)) if c else "" for c in header_cells]

        # Pour chaque champ canonique, vérifier s'il appartient à ce sheet
        synonyms = self.config["synonyms"]
        for field_name, field_synonyms in synonyms.items():
            # Déterminer si ce champ appartient à ce sheet
            sheet_for_field = self._get_sheet_for_field(field_name)
            if sheet_for_field != sheet_key:
                continue

            # Si pas de synonymes → position uniquement
            if not field_synonyms:
                # Utiliser position fallback
                if field_name in self.config["position_fallback"]:
                    fb = self.config["position_fallback"][field_name]
                    if fb["sheet"] == sheet_key:
                        resolved[field_name] = {"sheet": sheet_key, "col_index": fb["index"]}
                else:
                    missing[field_name] = "Pas de synonymes et pas de fallback"
                continue

            # Matcher les synonymes
            matches = []
            for col_idx, header in enumerate(headers):
                for syn in field_synonyms:
                    syn_norm = self._normalize(syn)
                    # Matching exact ou inclusion bidirectionnelle
                    if header == syn_norm:
                        # Match exact prioritaire
                        matches.append(col_idx)
                        break
                    elif len(syn_norm) <= 5 and syn_norm in header:
                        # Labels courts (CBC, IBC, etc.) → inclusion simple
                        matches.append(col_idx)
                        break
                    elif syn_norm in header or header in syn_norm:
                        # Matching général par inclusion
                        matches.append(col_idx)
                        break

            if len(matches) == 1:
                # Un seul match → résolu
                resolved[field_name] = {"sheet": sheet_key, "col_index": matches[0]}
            elif len(matches) == 0:
                # Aucun match → lacune
                missing[field_name] = "Aucun synonyme trouvé"
            else:
                # Plusieurs matches → ambiguë, utiliser position fallback
                if field_name in self.config["position_fallback"]:
                    fb = self.config["position_fallback"][field_name]
                    if fb["sheet"] == sheet_key:
                        resolved[field_name] = {"sheet": sheet_key, "col_index": fb["index"]}
                else:
                    missing[field_name] = f"Plusieurs matches ({len(matches)}), pas de fallback"

        return {"resolved": resolved, "missing": missing}

    def _get_sheet_for_field(self, field_name: str) -> Optional[str]:
        """Détermine à quel sheet appartient un champ canonique."""
        if field_name.startswith("to_date_"):
            return "to_date"
        elif field_name.startswith("perf_"):
            return "performance"
        elif field_name.startswith("wbs_"):
            return "wbs"
        return None

    def _llm_fallback(
        self,
        wb,
        sheet_mapping: dict,
        header_rows: dict,
        missing_fields: dict
    ) -> dict:
        """
        Appelle le LLM (petit modèle) sur les champs non résolus.

        Args:
            wb: openpyxl.Workbook
            sheet_mapping: {sheet_key: sheet_name}
            header_rows: {sheet_key: row_index}
            missing_fields: {field_name: raison}

        Returns:
            {field_name: {"sheet": sheet_key, "col_index": idx}}
        """
        if not self.openai_client:
            return {}

        resolved = {}

        # Grouper les champs manquants par sheet
        by_sheet = {}
        for field_name in missing_fields:
            sheet_key = self._get_sheet_for_field(field_name)
            if sheet_key not in by_sheet:
                by_sheet[sheet_key] = []
            by_sheet[sheet_key].append(field_name)

        # Appeler le LLM pour chaque sheet
        for sheet_key, fields in by_sheet.items():
            ws_name = sheet_mapping.get(sheet_key)
            if not ws_name:
                continue

            ws = wb[ws_name]
            header_row = header_rows[sheet_key]

            # Construire le prompt
            header_cells = list(ws.iter_rows(min_row=header_row+1, max_row=header_row+1, values_only=True))[0]
            headers = [str(c) if c else "" for c in header_cells]

            # Prendre 2-3 lignes d'exemple (JAMAIS les valeurs numériques complètes)
            sample_rows = []
            for i in range(1, min(4, ws.max_row - header_row)):
                row = list(ws.iter_rows(min_row=header_row+i+1, max_row=header_row+i+1, values_only=True))[0]
                # Masquer les valeurs numériques
                masked = []
                for cell in row:
                    if isinstance(cell, (int, float)):
                        masked.append("###")
                    else:
                        masked.append(str(cell) if cell else "")
                sample_rows.append(masked)

            prompt = self._build_llm_prompt(headers, sample_rows, fields)

            try:
                response = self.openai_client.chat.completions.create(
                    model=self.config.get("llm_model", "gpt-4o-mini"),
                    messages=[
                        {"role": "system", "content": "Tu es un expert en analyse de tableaux financiers. Réponds uniquement en JSON strict."},
                        {"role": "user", "content": prompt}
                    ],
                    temperature=0,
                    response_format={"type": "json_object"}
                )

                result = json.loads(response.choices[0].message.content)

                # Parser le résultat
                for field_name in fields:
                    if field_name in result:
                        col_ref = result[field_name]
                        if isinstance(col_ref, int):
                            col_idx = col_ref
                        else:
                            # Trouver l'index du libellé
                            col_idx = None
                            for i, h in enumerate(headers):
                                if self._normalize(h) == self._normalize(col_ref):
                                    col_idx = i
                                    break
                            if col_idx is None:
                                logger.warning(f"LLM a retourné '{col_ref}' pour {field_name}, introuvable dans les entêtes")
                                continue

                        resolved[field_name] = {"sheet": sheet_key, "col_index": col_idx}
                        logger.info(f"LLM a résolu {field_name} → col {col_idx}")

            except Exception as e:
                logger.error(f"Erreur appel LLM pour {sheet_key}: {e}")

        return resolved

    def _build_llm_prompt(self, headers: list, samples: list, fields: list) -> str:
        """Construit le prompt pour le LLM."""
        prompt = f"""Voici les entêtes d'un tableau financier et quelques lignes d'exemple (valeurs numériques masquées) :

Entêtes (index 0-based) :
{json.dumps(headers, ensure_ascii=False, indent=2)}

Lignes d'exemple :
{json.dumps(samples, ensure_ascii=False, indent=2)}

Je cherche à identifier les colonnes pour ces champs canoniques :
{json.dumps(fields, ensure_ascii=False, indent=2)}

Pour chaque champ, retourne soit l'index de la colonne (0-based), soit le libellé exact de l'entête.

Réponds en JSON strict : {{"field_name": index_ou_libelle, ...}}
"""
        return prompt

    def _normalize(self, s: str) -> str:
        """Normalise une chaîne : minuscule, sans accents, sans espaces multiples."""
        s = s.lower().strip()
        s = unicodedata.normalize('NFKD', s).encode('ascii', 'ignore').decode('ascii')
        s = ' '.join(s.split())
        return s

    def _format_signature(self, wb, header_rows: dict) -> str:
        """
        Hash stable : sha256(noms_onglets triés + libellés_entête de chaque onglet).

        Args:
            wb: openpyxl.Workbook
            header_rows: {sheet_key: row_index}

        Returns:
            Signature sha256 (hex)
        """
        parts = []

        # Noms d'onglets triés
        parts.append("::".join(sorted(wb.sheetnames)))

        # Retrouver le mapping des onglets
        sheet_mapping = self._detect_sheets(wb)

        # Libellés d'entête de chaque onglet détecté
        for sheet_key in sorted(header_rows.keys()):
            ws_name = sheet_mapping.get(sheet_key)
            if not ws_name:
                continue
            ws = wb[ws_name]
            row_idx = header_rows[sheet_key]
            header_cells = list(ws.iter_rows(min_row=row_idx+1, max_row=row_idx+1, values_only=True))[0]
            headers = [str(c) if c else "" for c in header_cells]
            parts.append(f"{sheet_key}::{' | '.join(headers)}")

        signature_input = "||".join(parts)
        return hashlib.sha256(signature_input.encode('utf-8')).hexdigest()

    def _validate_required(self, mapping: dict) -> None:
        """
        Valide que tous les champs obligatoires sont résolus.

        Raises:
            R1ConfigError: Si un champ obligatoire manque
        """
        from .config import REQUIRED_FIELDS

        missing = [f for f in REQUIRED_FIELDS if f not in mapping]
        if missing:
            raise R1ConfigError(
                f"Champs obligatoires non résolus : {missing}. "
                f"Impossible de continuer le traitement."
            )

    def get_sample_values(self, wb, mapping: dict, n: int = 3, cut: Optional[str] = None) -> dict:
        """
        Retourne des valeurs d'exemple par champ résolu (pour une confirmation avant analyse).

        Les champs de l'onglet 'To Date' varient par coupe : si `cut` est fourni,
        on retourne la valeur RÉELLE lue sur la ligne To Date où la période
        correspond à `cut` (liste à 1 élément, pour rester compatible avec le
        format front qui fait `samples[field].slice(0,3)`). C'est la même logique
        de scan que `_read_poc` (r1.parser) : on matche la colonne période
        exacte, en ignorant la ligne de totaux 'To Date'.

        Si `cut` est None ou que la période est introuvable dans l'onglet To Date,
        on retombe sur le comportement historique (3 premières lignes après
        l'entête) et on logue un warning — ces valeurs ne représentent alors pas
        forcément la coupe qui sera effectivement extraite.

        Les champs des autres onglets (performance, wbs) sont des snapshots figés
        (FAC) par design : l'échantillon reste les n premières lignes après
        l'entête, quelle que soit la coupe.

        Args:
            wb: openpyxl.Workbook
            mapping: Mapping résolu
            n: Nombre de valeurs à échantillonner (fallback / onglets non to_date)
            cut: Coupe cible ('YYYY.MM') pour échantillonner les champs to_date
                 à la ligne réelle de la coupe. None → fallback historique.

        Returns:
            {field_name: [val1, val2, val3]}
        """
        samples = {}

        # Retrouver les noms d'onglets réels
        sheet_names = {}
        for sheet_key, ws_name in self._detect_sheets(wb).items():
            sheet_names[sheet_key] = ws_name

        # Retrouver les lignes d'entête
        header_rows = self._anchor_headers(wb, sheet_names)

        # Pré-calculer la ligne To Date correspondant à `cut` (une seule fois)
        to_date_cut_row = None
        if cut is not None:
            to_date_cut_row = self._find_to_date_row(wb, cut, sheet_names, header_rows, mapping)
            if to_date_cut_row is None:
                logger.warning(
                    "get_sample_values: coupe '%s' introuvable dans l'onglet To Date, "
                    "fallback sur les %d premières lignes pour les champs to_date "
                    "(échantillon non représentatif de la coupe sélectionnée).",
                    cut, n,
                )
        elif cut is None:
            logger.warning(
                "get_sample_values: aucune coupe fournie, fallback sur les %d "
                "premières lignes pour les champs to_date (échantillon statique, "
                "non représentatif de la coupe qui sera extraite).",
                n,
            )

        for field_name, loc in mapping.items():
            sheet_key = loc["sheet"]
            col_idx = loc["col_index"]

            ws_name = sheet_names.get(sheet_key)
            if not ws_name:
                continue

            ws = wb[ws_name]
            header_row = header_rows.get(sheet_key, 0)

            if sheet_key == "to_date" and to_date_cut_row is not None:
                # Valeur RÉELLE lue à la ligne To Date de la coupe sélectionnée.
                values = [to_date_cut_row[col_idx]] if col_idx < len(to_date_cut_row) else []
            else:
                values = []
                for i in range(1, min(n+1, ws.max_row - header_row)):
                    row = list(ws.iter_rows(min_row=header_row+i+1, max_row=header_row+i+1, values_only=True))[0]
                    if col_idx < len(row):
                        values.append(row[col_idx])

            samples[field_name] = values

        return samples

    def _find_to_date_row(
        self, wb, cut: str, sheet_names: dict, header_rows: dict, mapping: dict
    ) -> Optional[tuple]:
        """
        Retrouve la ligne (tuple de valeurs) de l'onglet To Date correspondant à `cut`.

        Même motif de scan que `_read_poc` (r1.parser) : on ignore la
        ligne de totaux 'To Date' et on matche la colonne période exacte.

        Args:
            wb: openpyxl.Workbook
            cut: Période cible ('YYYY.MM')
            sheet_names: {sheet_key: nom réel de l'onglet}
            header_rows: {sheet_key: index 0-based de la ligne d'entête}
            mapping: Mapping résolu (doit contenir 'to_date_period')

        Returns:
            Le tuple de valeurs de la ligne matchée, ou None si introuvable.
        """
        ws_name = sheet_names.get("to_date")
        if not ws_name or "to_date_period" not in mapping:
            return None

        ws = wb[ws_name]
        header_row = header_rows.get("to_date", 0)
        col_period = mapping["to_date_period"]["col_index"]

        for row_idx in range(header_row + 1, ws.max_row):
            row = list(ws.iter_rows(min_row=row_idx+1, max_row=row_idx+1, values_only=True))[0]
            if not row or col_period >= len(row):
                continue

            period_val = row[col_period]
            if not period_val:
                continue

            period_str = str(period_val).strip()

            # Ignorer 'To Date' (ligne de totaux)
            if period_str.lower() == "to date":
                continue

            if period_str == cut:
                return row

        return None

    def get_available_columns(self, wb, n: int = 3) -> Dict[str, List[Dict[str, Any]]]:
        """
        Retourne TOUTES les colonnes disponibles par onglet R1 détecté (pour
        permettre de corriger le mapping avant analyse, en complément du
        mapping résolu automatiquement).

        Réutilise `_detect_sheets` / `_anchor_headers` (même logique que
        `resolve()`) : seuls les onglets non-PII, détectés, avec une entête
        ancrée apparaissent dans le résultat. Les onglets PII n'apparaissent
        JAMAIS (garde déjà appliquée en amont par `_detect_sheets`).

        Args:
            wb: openpyxl.Workbook (chargé avec data_only=True)
            n: Nombre max de valeurs d'exemple par colonne

        Returns:
            {sheet_key: [{"col_index", "header", "samples"}, ...]}
            - col_index : 0-based, cohérent avec mapping[...].col_index
            - header : libellé de l'entête, "(sans titre)" si la cellule est vide
            - samples : jusqu'à `n` valeurs non nulles sous l'entête, en str
        """
        sheet_mapping = self._detect_sheets(wb)
        header_rows = self._anchor_headers(wb, sheet_mapping)

        result: Dict[str, List[Dict[str, Any]]] = {}

        for sheet_key, ws_name in sheet_mapping.items():
            if ws_name is None or sheet_key not in header_rows:
                continue

            ws = wb[ws_name]
            header_row = header_rows[sheet_key]

            header_cells = list(
                ws.iter_rows(min_row=header_row + 1, max_row=header_row + 1, values_only=True)
            )[0]
            n_cols = len(header_cells)

            columns: List[Dict[str, Any]] = []
            for col_idx in range(n_cols):
                header_val = header_cells[col_idx]
                header_str = str(header_val).strip() if header_val not in (None, "") else ""
                if not header_str:
                    header_str = "(sans titre)"

                samples: List[str] = []
                for row_idx in range(header_row + 1, ws.max_row):
                    row = list(
                        ws.iter_rows(min_row=row_idx + 1, max_row=row_idx + 1, values_only=True)
                    )[0]
                    if col_idx >= len(row):
                        continue
                    val = row[col_idx]
                    if val is None or str(val).strip() == "":
                        continue
                    samples.append(str(val))
                    if len(samples) >= n:
                        break

                columns.append({"col_index": col_idx, "header": header_str, "samples": samples})

            result[sheet_key] = columns

        return result

    def confirm(self, mapping: dict, samples: dict, interactive: bool = False) -> bool:
        """
        Affiche le mapping + échantillons et retourne True (auto-confirm) ou attend validation.

        Args:
            mapping: Mapping résolu
            samples: Échantillons de valeurs
            interactive: Si True, attend une validation utilisateur

        Returns:
            True si confirmé, False sinon
        """
        print("\n" + "="*60)
        print("MAPPING RÉSOLU")
        print("="*60)
        for field, loc in sorted(mapping.items()):
            sample_vals = samples.get(field, [])
            print(f"{field:30} → {loc['sheet']:15} col {loc['col_index']:2}  |  ex: {sample_vals}")
        print("="*60 + "\n")

        if not interactive:
            logger.info("Mode non-interactif : mapping auto-confirmé")
            return True

        # Mode interactif
        response = input("Confirmer ce mapping ? (o/n) : ").strip().lower()
        return response in ["o", "oui", "y", "yes"]
