"""
Extraction de texte depuis des PDF, page par page (PyMuPDF).

Vendored tel quel depuis le pipeline d'ingestion d'origine : aucune
dépendance au reste du POC.
"""
from dataclasses import dataclass
from typing import BinaryIO
import fitz  # PyMuPDF


@dataclass
class PageContent:
    """Contenu extrait d'une page PDF."""
    page_number: int  # 1-based
    text: str


def extract_text_from_pdf(pdf_stream: BinaryIO, filename: str) -> list[PageContent]:
    """
    Extrait le texte d'un PDF page par page.

    Args:
        pdf_stream: Flux binaire du PDF
        filename: Nom du fichier (pour les logs)

    Returns:
        Liste de PageContent (numéros de page en base 1)
    """
    pages = []

    try:
        pdf_bytes = pdf_stream.read()
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")

        for page_num in range(len(doc)):
            page = doc[page_num]
            text = page.get_text()

            pages.append(PageContent(
                page_number=page_num + 1,
                text=text.strip()
            ))

        doc.close()
        print(f"[OK] Extrait {len(pages)} page(s) de {filename}")

    except Exception as e:
        print(f"[ERREUR] Extraction de {filename} : {e}")
        raise

    finally:
        if hasattr(pdf_stream, 'close'):
            try:
                pdf_stream.close()
            except Exception:
                pass

    return pages
