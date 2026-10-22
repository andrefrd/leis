"""Coletor da Câmara Municipal de Belo Horizonte: análise de páginas, robots e gates, sem rede."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import baixar_belo_horizonte as b

LISTA = (
    "<div id='resultadoPesquisa_normasInterno'><h1>Resultados da pesquisa:</h1>"
    "<span class='resumoResultados'>Estão sendo exibidos os itens de 1 a 7 de um total de 68 itens</span>"
    "<ul class='lista-pesquisas'>"
    "<li><h3><span data-caminho='https://cmbhsilint.cmbh.mg.gov.br/silinternet/servico/norma?id=2c907f76766722210176cdb550ae0183' "
    "class='detalhar_normas vinculavel'>Lei - 11279/2020</span></h3><div class='col-xs-12 espaco-abaixo'><p><strong>Ementa: </strong>"
    "Altera a Lei nº 10.362/11 &amp; dá outras providências.</p></div></li>"
    "<li><h3><span data-caminho='https://cmbhsilint.cmbh.mg.gov.br/silinternet/servico/norma?id=2c907f76766722210176b3cbd1c50102' "
    "class='detalhar_normas vinculavel'>Lei - 11278/2020</span></h3><div class='col-xs-12 espaco-abaixo'><p><strong>Ementa: </strong>"
    "Dá o nome de Aldacyr Dias Lopes ao Viaduto 3.681.</p></div></li></ul></div>")

DETALHE = (
    "<div id=\"resultadoPesquisa_normasInterno\"><h2>Lei - 11279/2020</h2>"
    "<span id='novaUrl' style='display:none'>https://www.cmbh.mg.gov.br/atividade-legislativa/pesquisar-legislacao/lei/11279/2020</span>"
    "<div class=\"col-xs-12\"><p><strong>Documentos da norma: </strong><ol id='documentos-norma'><li><span>lei11279.doc</span>"
    "<a href=\"https://cmbhsildownload.cmbh.mg.gov.br/silinternet/servico/download/documentoDaNorma?idDocDaNorma=2c907f76766722210176cdbf6ede0187\" "
    "class=\"btn\"></a></li></ol></p><li style='list-style:none'><strong>Responsável pela Promulgação/Publicação: </strong>Prefeito</li>"
    "<ul><li><strong>Data da promulgação: </strong>31/12/2020</li><li><strong>Data da publicação: </strong>01/01/2021</li></ul>"
    "<p><strong>Autoria:</strong> Executivo: Mensagem nº 4</p><p><strong>Ementa:</strong> Altera a Lei nº 10.362/11.</p></div>")

PAGINA = ('<select name="tipoDeNorma" id="tipoDeNorma" class="form-control"><option value="">[Todas]</option>'
          '<option value="aa1">Decreto</option><option selected="selected" value="bb2">Emenda à Lei Orgânica</option></select>')

ROBOTS = "User-agent: GPTBot\nDisallow: /printpdf/\n\nUser-agent: *\nCrawl-delay: 10\nDisallow: /modules/\nDisallow: /search/\n"

LEI = ("**LEI Nº 11.279, DE 31 DE DEZEMBRO DE 2020**\n\n**Altera a Lei nº 10.362/11.**\n\n"
       "O Povo do Município de Belo Horizonte decreta:\n\n**Art. 1º** - O art. 2º passa a vigorar assim.\n\n"
       "**Art. 2º** - Esta lei entra em vigor na data de sua publicação.")


class AnaliseTest(unittest.TestCase):
    def test_lista(self):
        total, itens = b.parse_lista(LISTA)
        self.assertEqual(total, 68)
        self.assertEqual([i["titulo"] for i in itens], ["Lei - 11279/2020", "Lei - 11278/2020"])
        self.assertEqual(itens[0]["sil_id"], "2c907f76766722210176cdb550ae0183")
        self.assertIn("& dá outras", itens[0]["ementa"])

    def test_lista_vazia_e_sem_resumo(self):
        self.assertEqual(b.parse_lista("<p>Nenhum resultado encontrado. Tente</p>"), (0, []))
        with self.assertRaises(RuntimeError):
            b.parse_lista("<div>erro</div>")

    def test_detalhe(self):
        d = b.parse_detalhe(DETALHE)
        self.assertEqual(d["promulgacao"], "2020-12-31")
        self.assertEqual(d["publicacao"], "2021-01-01")
        self.assertEqual(d["documentos"][0]["nome"], "lei11279.doc")
        self.assertTrue(d["fonte"].endswith("/lei/11279/2020"))
        self.assertEqual(d["promulgador"], "Prefeito")

    def test_titulo_e_tipos(self):
        self.assertEqual(b.parse_titulo("Decreto Legislativo - 12/1999"), ("Decreto Legislativo", "12", "1999"))
        self.assertIsNone(b.parse_titulo("Lei 12"))
        self.assertEqual(b.parse_tipos(PAGINA), {"Decreto": "aa1", "Emenda à Lei Orgânica": "bb2"})

    def test_robots(self):
        self.assertEqual(b.atraso_robots(ROBOTS), 10.0)
        regras = b.proibidos_robots(ROBOTS)
        self.assertEqual(regras, ["/modules/", "/search/"])
        self.assertTrue(b.permitido("/sites/all/modules/consulta_normas/pesquisar.php", regras))
        self.assertFalse(b.permitido("/search/x", regras))

    def test_data_da_epigrafe(self):
        self.assertEqual(b.data_da_epigrafe("**DECRETO Nº 5, DE 1º DE MARÇO DE 1931**\n\nTexto"), "1931-03-01")
        self.assertEqual(b.data_da_epigrafe("sem data aqui"), "")

    def test_epigrafe_confere(self):
        self.assertTrue(b.epigrafe_confere(LEI, "Lei", "11279"))
        self.assertFalse(b.epigrafe_confere(LEI, "Lei", "999"))


class ColetaTest(unittest.TestCase):
    def coletor(self, tmp, corpo, motivo=None, detalhe=DETALHE):
        tmp = Path(tmp)
        patches = [patch.object(b, "RAIZ", tmp), patch.object(b, "PASTA", tmp / "MG/belo-horizonte"),
                   patch.object(b, "INDICE", tmp / "_indice"), patch.object(b, "FONTE", tmp / "_fonte"),
                   patch.object(b, "documento_para_md", lambda raw, nome: (corpo, motivo))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        c = b.Coletor(fonte=None, hoje="2026-10-22")
        c.detalhe = lambda sil_id: detalhe
        c.anexo = lambda sil_id, doc: b"%DOC%"
        return c

    ITEM = {"sil_id": "2c907f76766722210176cdb550ae0183", "titulo": "Lei - 11279/2020", "ementa": "Altera a Lei."}

    def test_grava_com_cabecalho(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = self.coletor(tmp, LEI)
            self.assertEqual(c.processar("lei", self.ITEM), "gravado")
            arq = Path(tmp) / "MG/belo-horizonte/leis-ordinarias/2020/leis-ordinarias-11279-2020-sil-50ae0183.md"
            texto = arq.read_text()
            for campo in ("titulo", "tipo", "tipo_na_fonte", "numero", "data", "municipio", "uf", "codigo_ibge",
                          "versao_texto", "fonte", "fonte_texto", "sha256_fonte", "sha256_texto", "baixado_em"):
                self.assertIn(f"\n{campo}: ", "\n" + texto)
            self.assertIn('data: "2020-12-31"', texto)
            self.assertIn('codigo_ibge: "3106200"', texto)
            self.assertIn("**Art. 1º**", texto)
            self.assertEqual(c.processar("lei", self.ITEM), "existente")
            self.assertEqual(c.stats["gravados"], 1)

    def test_retem_sem_artigo_inicial(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = self.coletor(tmp, "LEI Nº 11.279\n\nTexto solto sem dispositivos numerados.")
            self.assertEqual(c.processar("lei", self.ITEM), "retido")
            self.assertFalse(list(Path(tmp).glob("MG/**/*.md")))
            self.assertIn("artigo inicial", (Path(tmp) / "_indice/falhas.jsonl").read_text())

    def test_retem_epigrafe_de_outra_norma(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = self.coletor(tmp, LEI + "\n\n**LEI Nº 11.280, DE 31 DE DEZEMBRO DE 2020**\n\nOutra norma.")
            self.assertEqual(c.processar("lei", self.ITEM), "retido")
            self.assertEqual(c.stats["gravados"], 0)

    def test_retem_identidade_divergente_conversao_e_sem_anexo(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.coletor(tmp, LEI.replace("11.279", "11.000")).processar("lei", self.ITEM), "retido")
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.coletor(tmp, None, "documento vazio").processar("lei", self.ITEM), "retido")
        with tempfile.TemporaryDirectory() as tmp:
            sem = DETALHE.replace("<li><span>lei11279.doc</span>", "<li><span>nota.jpg</span>")
            self.assertEqual(self.coletor(tmp, LEI, detalhe=sem).processar("lei", self.ITEM), "retido")
        with tempfile.TemporaryDirectory() as tmp:
            c = self.coletor(tmp, LEI)
            c.detalhe = lambda sil_id: DETALHE.replace("Lei - 11279/2020", "Lei - 1/2020")
            self.assertEqual(c.processar("lei", self.ITEM), "retido")

    def test_dedupe_por_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = self.coletor(tmp, LEI)
            c.processar("lei", self.ITEM)
            outro = dict(self.ITEM, sil_id="2c907f76766722210176cdb550ae9999")
            self.assertEqual(c.processar("lei", outro), "equivalente")
            self.assertEqual(len(list(Path(tmp).glob("MG/**/*.md"))), 1)

    def test_data_ausente_usa_epigrafe(self):
        with tempfile.TemporaryDirectory() as tmp:
            sem = DETALHE.replace("<li><strong>Data da promulgação: </strong>31/12/2020</li>", "")
            c = self.coletor(tmp, LEI, detalhe=sem)
            self.assertEqual(c.processar("lei", self.ITEM), "gravado")
            self.assertIn('data: "2020-12-31"', next(Path(tmp).glob("MG/**/*.md")).read_text())


if __name__ == "__main__":
    unittest.main()
