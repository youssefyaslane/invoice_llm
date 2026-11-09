"""Étape 1 : PDF -> Markdown avec Docling (tableaux reconstruits, colonnes dans l'ordre, en-têtes et pieds de page
gardés, sans images)."""
import logging

from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling_core.types.doc import ContentLayer

logging.getLogger("docling").setLevel(logging.WARNING)


def convertisseur():
    options = PdfPipelineOptions()
    options.do_ocr = False                   # PDF numériques : le texte est déjà dans le fichier
    options.do_table_structure = True        # vrais tableaux (cellules sur plusieurs lignes, colonnes nommées)
    options.generate_picture_images = False  # pas de logos en base64 dans le texte
    return DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)})


def pdf_en_markdown(conv, pdf):
    document = conv.convert(str(pdf)).document
    # tableaux compacts (sans espaces d'alignement) : beaucoup moins de tokens à lire pour le LLM ;
    # en-têtes et pieds de page gardés : le nom du fournisseur n'est parfois écrit que là (OVH, mentions légales)
    return document.export_to_markdown(compact_tables=True, escape_html=False, escape_underscores=False,
                                       included_content_layers={ContentLayer.BODY, ContentLayer.FURNITURE})
