"""
Anonymisation de texte avec Microsoft Presidio (moteur NLP français).

Vendored tel quel depuis le pipeline d'ingestion d'origine : ce wrapper n'a
aucune dépendance au reste du POC, il ne dépend que de Presidio et de spaCy.
"""
import os
from presidio_analyzer import AnalyzerEngine, RecognizerRegistry, PatternRecognizer
from presidio_analyzer.nlp_engine import NlpEngineProvider
from presidio_anonymizer import AnonymizerEngine
from presidio_anonymizer.entities import OperatorConfig


class TextAnonymizer:
    """
    Wrapper pour l'anonymisation de texte en français via Presidio.
    """

    def __init__(
        self,
        pii_entities: list[str] | None = None,
        client_names: list[str] | None = None,
    ):
        """
        Initialise l'anonymiseur.

        Args:
            pii_entities: Liste des types PII à masquer.
                          Par défaut : PERSON, EMAIL_ADDRESS, PHONE_NUMBER, ID
            client_names: Noms de clients à masquer de façon déterministe
                          (liste de refus). Chaque occurrence est remplacée par
                          ``<CLIENT>``. Utile pour retirer le nom du client mis en
                          avant dans un document, indépendamment de la détection NER.
        """
        if pii_entities is None:
            pii_entities_str = os.getenv("PII_ENTITIES", "PERSON,EMAIL_ADDRESS,PHONE_NUMBER,ID")
            pii_entities = [e.strip() for e in pii_entities_str.split(",")]

        self.pii_entities = pii_entities
        self.client_names = [c.strip() for c in (client_names or []) if c and c.strip()]

        # Configuration du moteur NLP pour le français
        nlp_configuration = {
            "nlp_engine_name": "spacy",
            "models": [{"lang_code": "fr", "model_name": "fr_core_news_md"}]
        }

        provider = NlpEngineProvider(nlp_configuration=nlp_configuration)
        nlp_engine = provider.create_engine()

        # Initialiser l'analyseur avec le moteur NLP français
        self.analyzer = AnalyzerEngine(
            nlp_engine=nlp_engine,
            supported_languages=["fr"]
        )

        # Liste de refus déterministe pour le(s) nom(s) de client.
        # Garantit le masquage exact du nom, sans dépendre de la détection NER
        # (qui peut rater un nom propre ou en sur-masquer d'autres).
        if self.client_names:
            client_recognizer = PatternRecognizer(
                supported_entity="CLIENT",
                deny_list=self.client_names,
                supported_language="fr",
            )
            self.analyzer.registry.add_recognizer(client_recognizer)
            if "CLIENT" not in self.pii_entities:
                self.pii_entities.append("CLIENT")

        self.anonymizer = AnonymizerEngine()

        scope = ', '.join(self.pii_entities)
        clients = f" | clients: {', '.join(self.client_names)}" if self.client_names else ""
        # ASCII only : évite un UnicodeEncodeError sur console Windows (cp1252).
        print(f"[OK] Anonymiseur initialise (scope PII : {scope}{clients})")

    def anonymize(self, text: str) -> str:
        """
        Anonymise le texte en masquant les entités PII.

        Args:
            text: Texte à anonymiser

        Returns:
            Texte anonymisé (PII remplacées par des marqueurs <PERSON>, <EMAIL_ADDRESS>, etc.)
        """
        if not text or not text.strip():
            return text

        # Analyser le texte pour détecter les PII
        results = self.analyzer.analyze(
            text=text,
            language="fr",
            entities=self.pii_entities
        )

        # Anonymiser en remplaçant par le type d'entité
        anonymized = self.anonymizer.anonymize(
            text=text,
            analyzer_results=results,
            operators={
                entity: OperatorConfig("replace", {"new_value": f"<{entity}>"})
                for entity in self.pii_entities
            }
        )

        return anonymized.text
