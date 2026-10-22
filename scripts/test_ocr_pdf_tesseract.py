import io
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import comum

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    Image = None

FONTES = ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/dejavu/DejaVuSans.ttf",
          "/Library/Fonts/Arial.ttf"]
FONTE = next((f for f in FONTES if os.path.exists(f)), None)
TEM_TUDO = bool(Image and FONTE and shutil.which("tesseract") and shutil.which("pdftoppm")
                and comum._tesseract_modelo())


def pdf_escaneado(paginas):
    imgs = []
    fonte = ImageFont.truetype(FONTE, 44)
    for linhas in paginas:
        im = Image.new("L", (1700, 2200), 255)
        d = ImageDraw.Draw(im)
        for i, ln in enumerate(linhas):
            d.text((150, 200 + i * 90), ln, font=fonte, fill=0)
        imgs.append(im)
    buf = io.BytesIO()
    imgs[0].save(buf, format="PDF", resolution=200, save_all=True, append_images=imgs[1:])
    return buf.getvalue()


@unittest.skipUnless(TEM_TUDO, "requer PIL, fonte DejaVu, tesseract (por) e pdftoppm")
class OcrPdfTesseractTest(unittest.TestCase):
    PAGINAS = [["LEI N 1234, DE 5 DE MAIO DE 2020", "Art. 1 Fica instituido o programa municipal.",
                "Art. 2 Esta Lei entra em vigor na data de sua publicacao."],
               ["Art. 3 Revogam-se as disposicoes em contrario."]]

    def setUp(self):
        self.pdf = pdf_escaneado(self.PAGINAS)
        self.sem_vision = mock.patch.object(comum, "OCR_BIN", "/inexistente/ocr")
        self.sem_vision.start()
        self.addCleanup(self.sem_vision.stop)

    def test_texto_paginas_e_motor(self):
        texto, motor = comum.ocr_pdf_motor(self.pdf)
        self.assertTrue(motor.startswith("Tesseract 5 (por,"), motor)
        paginas = [p for p in texto.split("\f") if p.strip()]
        self.assertEqual(len(paginas), 2)
        self.assertIn("1234", paginas[0])
        self.assertIn("Art. 2", paginas[0])
        self.assertIn("Art. 3", paginas[1])
        self.assertEqual(comum.ocr_pdf(self.pdf), texto)

    def test_documento_para_md_grava_motor_tesseract(self):
        corpo, erro = comum.documento_para_md(self.pdf)
        self.assertIsNone(erro)
        self.assertIn("PDF escaneado (Tesseract 5 (por,", corpo.split("\n")[0])
        self.assertIn("1234", corpo)


class OcrPdfSemMotorTest(unittest.TestCase):
    def test_sem_vision_nem_tesseract_devolve_vazio(self):
        with mock.patch.object(comum, "OCR_BIN", "/inexistente/ocr"), \
                mock.patch.object(comum, "_tesseract_modelo", return_value=None):
            self.assertEqual(comum.ocr_pdf_motor(b"%PDF-1.4"), ("", ""))

    def test_vision_preservado_quando_existe(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = os.path.join(tmp, "ocr")
            with open(fake, "w") as f:
                f.write("#!/bin/sh\nprintf 'Art. 1 texto\\f'\n")
            os.chmod(fake, 0o755)
            with mock.patch.object(comum, "OCR_BIN", fake):
                self.assertEqual(comum.ocr_pdf_motor(b"%PDF-1.4"), ("Art. 1 texto\f", "Vision do macOS"))


if __name__ == "__main__":
    unittest.main()
