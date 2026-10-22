"""Coletor de São Paulo/SP: índice, extração do corpo, identidade e gates, sem rede."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import baixar_municipal_sao_paulo as s

BUSCA = '''<strong id="number-results"> 1 - 2 de 2 </strong>
<div id="resultados">
<div class="bx-resultado">
  <a href="/leis/lei-17543-de-23-de-dezembro-de-2020">
    <h4>LEI Nº 17.543 de 23 de Dezembro de 2020</h4>
    <p>Dispõe sobre o subsídio.</p>
  </a>
  <div class="bx-tbHistorico"><table><tbody><tr><td class="nameMeta">Situação</td>
  <td><p>SEM REVOGAÇÃO EXPRESSA</p></td></tr></tbody></table></div>
  <ul><li><a href="/leis/decreto-60052-de-14-de-janeiro-de-2021">DECRETO Nº 60.052 DE 14 DE JANEIRO DE 2021</a></li></ul>
</div>
<div class="bx-resultado">
  <a href="/leis/lei-86-de-29-de-dezembro-de-1893">
    <h4>LEI Nº 86 de 29 de Dezembro de 1893</h4>
    <p>PROIBE ESTABULOS</p>
  </a>
  <div class="bx-tbHistorico"><table><tbody><tr><td class="nameMeta">Situação</td>
  <td><p>REVOGADO</p></td></tr></tbody></table></div>
</div>
</div>'''

PAGINA = '''<div class="col s12 push-s0 bx-ementa customStyle"><p>Dispõe sobre o subsídio.</p></div>
<div class="col s12 customStyle">
<p>LEI Nº 17.543, DE 23 DE DEZEMBRO DE 2020</p><p>(<a href="http://x">Projeto de Lei nº 173/18</a>, da Mesa)</p>
<p>Dispõe sobre o subsídio.</p>
<p>BRUNO COVAS, Prefeito do Município de São Paulo, faz saber que a Câmara decretou e eu promulgo a seguinte lei:</p>
<p>Art. 1º O subsídio do Prefeito fica fixado em R$ 35.462,00.</p>
<p>Art. 2º Esta Lei entra em vigor na data de sua publicação.</p>
<div class="bx-mensagem "><p>Este texto não substitui o original publicado no Diário Oficial da Cidade de São Paulo</p></div>
</div>
<div class="bx-leis historyBox"><div id="historico"><div><p>L 14106/05-REVOGA A LEI</p></div></div></div>'''

VAZIA = '''<div class="col s12 push-s0 bx-ementa customStyle">PROIBE ESTABULOS</div>
<div class="col s12 customStyle">

</div>'''


ANEXOS = '''<div id="anexos" class="row"><ul>
<li><a href="/leis/lei-17543-de-23-de-dezembro-de-2020/anexo/abc123/Tabela.pdf" target="_blank">Tabela (1 KB)</a></li>
<li><a href="/leis/lei-17543-de-23-de-dezembro-de-2020/anexo/abc123/Tabela.pdf">dup</a></li>
</ul></div>'''


class SaoPauloTest(unittest.TestCase):
    def test_anexos_sinalizados(self):
        meta, corpo, _, _ = s.converter_ato(self.ato(), (PAGINA + ANEXOS).encode(), "LEI")
        url = s.BASE + "/leis/lei-17543-de-23-de-dezembro-de-2020/anexo/abc123/Tabela.pdf"
        self.assertEqual(meta["anexos_na_fonte"], [url])
        self.assertIn("NÃO incluído", meta["versao_texto"])
        self.assertIn(url, s.montar_md(meta, corpo))
        meta, _, _, _ = s.converter_ato(self.ato(), PAGINA.encode(), "LEI")
        self.assertNotIn("anexos_na_fonte", meta)

    def ato(self, slug="lei-17543-de-23-de-dezembro-de-2020", titulo="LEI Nº 17.543 de 23 de Dezembro de 2020"):
        return {"slug": slug, "titulo_indice": titulo, "ementa": "Dispõe sobre o subsídio.", "situacao": "SEM REVOGAÇÃO EXPRESSA"}

    def test_parse_resultados_usa_so_o_primeiro_link_e_a_situacao(self):
        total, atos = s.parse_resultados(BUSCA)
        self.assertEqual(total, 2)
        self.assertEqual([a["slug"] for a in atos], ["lei-17543-de-23-de-dezembro-de-2020", "lei-86-de-29-de-dezembro-de-1893"])
        self.assertEqual(atos[0]["situacao"], "SEM REVOGAÇÃO EXPRESSA")
        self.assertEqual(atos[1]["situacao"], "REVOGADO")
        self.assertEqual(atos[0]["ementa"], "Dispõe sobre o subsídio.")

    def test_numero_com_zero_a_esquerda_e_orgao(self):
        self.assertTrue(s.epigrafe_confere("Portaria nº 02/SUB-IT/GAB/2021\n\ntexto", "2"))
        self.assertFalse(s.epigrafe_confere("Portaria nº 12/SUB-IT/GAB/2021", "2"))
        self.assertEqual(s.orgao_do_titulo("PORTARIA SUBPREFEITURA DO ITAIM PAULISTA - SUB-IT Nº 2 de 15 de Fevereiro de 2021", "PORTARIA"),
                         "SUBPREFEITURA DO ITAIM PAULISTA - SUB-IT")
        pag = PAGINA.replace("LEI Nº 17.543, DE 23 DE DEZEMBRO DE 2020", "PORTARIA 02/SMS/2020, DE 23 DE DEZEMBRO DE 2020")
        ato = {"slug": "portaria-smul-sms-2-de-23-de-dezembro-de-2020",
               "titulo_indice": "PORTARIA SECRETARIA DA SAÚDE - SMS Nº 2 de 23 de Dezembro de 2020", "ementa": "", "situacao": ""}
        meta, _, categoria, _ = s.converter_ato(ato, pag.encode(), "PORTARIA")
        self.assertEqual(categoria, "portarias")
        self.assertEqual(meta["orgao_na_fonte"], "SECRETARIA DA SAÚDE - SMS")
        self.assertEqual(meta["titulo"], "Portaria SECRETARIA DA SAÚDE - SMS nº 2, de 23 de dezembro de 2020")
        self.assertNotIn("situacao_na_fonte", meta)

    def test_total_em_pagina_unica(self):
        self.assertEqual(s.parse_resultados('<strong id="number-results">\n   92\n </strong>')[0], 92)

    def test_identidade(self):
        self.assertEqual(s.identidade("LEI Nº 17.543 de 23 de Dezembro de 2020", "lei-17543-de-23-de-dezembro-de-2020"),
                         ("17.543", "2020", "2020-12-23"))
        self.assertEqual(s.identidade("DECRETO Nº 65.599 de 1 de Outubro de 2026", "decreto-65599-de-1-de-outubro-de-2026")[2],
                         "2026-10-01")
        self.assertEqual(s.identidade("LEI Nº 9.000-A de 3 de Março de 1980", "lei-9000-a-de-3-de-marco-de-1980")[2], "1980-03-03")
        # data do slug (publicação) diferente da do título é tolerada se o corpo citar a data do título
        self.assertEqual(s.identidade("DECRETO LEI PREFEITO - PREF Nº 431 de 8 de Julho de 1947",
                                      "decreto-lei-gabinete-do-prefeito-431-de-9-de-julho-de-1947")[2], "1947-07-08")
        self.assertFalse(s.slug_confere_data("decreto-lei-x-431-de-9-de-julho-de-1947", "1947-07-08"))
        self.assertTrue(s.corpo_cita_data("Decreto-lei nº 431, de 8 de julho de 1947", "1947-07-08"))
        self.assertTrue(s.corpo_cita_data("de 1º de março de 1980", "1980-03-01"))
        self.assertFalse(s.corpo_cita_data("de 9 de julho de 1947", "1947-07-08"))
        with self.assertRaises(RuntimeError):
            s.identidade("LEI Nº 1 de 23 de Dezembro de 2020", "lei-2-de-23-de-dezembro-de-2020")
        with self.assertRaises(RuntimeError):
            s.identidade("Sem número", "x")

    def test_extrai_corpo_aviso_e_historico(self):
        corpo, avisos, historico = s.extrair_pagina(PAGINA)
        self.assertIn("Art. 1º", corpo)
        self.assertNotIn("bx-mensagem", corpo)
        self.assertNotIn("Este texto não substitui", corpo)
        self.assertEqual(avisos, ["Este texto não substitui o original publicado no Diário Oficial da Cidade de São Paulo"])
        self.assertEqual(historico, ["L 14106/05-REVOGA A LEI"])

    def test_converte_ato_com_metadados(self):
        meta, corpo, categoria, ano = s.converter_ato(self.ato(), PAGINA.encode(), "LEI")
        self.assertEqual((categoria, ano), ("leis-ordinarias", "2020"))
        self.assertEqual(meta["data"], "2020-12-23")
        self.assertEqual(meta["numero"], "17543")
        self.assertEqual(meta["titulo"], "Lei nº 17.543, de 23 de dezembro de 2020 — Dispõe sobre o subsídio.")
        self.assertEqual(meta["fonte"], "https://legislacao.prefeitura.sp.gov.br/lei-17543-de-23-de-dezembro-de-2020")
        self.assertEqual(meta["situacao_na_fonte"], "SEM REVOGAÇÃO EXPRESSA")
        self.assertIn("não substitui o original", meta["versao_texto"])
        self.assertIn("**Art. 1º**", corpo)
        md = s.montar_md(meta, corpo)
        self.assertTrue(md.startswith('---\ntitulo: "Lei nº 17.543'))
        self.assertIn('historico_na_fonte: ["L 14106/05-REVOGA A LEI"]', md)

    def test_tachado_e_hifen_condicional(self):
        pag = PAGINA.replace("Art. 2º Esta Lei", "Art. 2º <del>Esta</del> (Declarado inconstitucional) Lei").replace("subsídio do", "subsí\u00addio do")
        meta, corpo, *_ = s.converter_ato(self.ato(), pag.encode(), "LEI")
        self.assertIn("~~Esta~~", corpo)
        self.assertNotIn("\u00ad", corpo)
        self.assertIn("tachados", meta["versao_texto"])

    def test_gate_sem_texto_integral(self):
        with self.assertRaisesRegex(RuntimeError, "sem texto integral"):
            s.converter_ato(self.ato("lei-86-de-29-de-dezembro-de-1893", "LEI Nº 86 de 29 de Dezembro de 1893"), VAZIA.encode(), "LEI")

    def test_gate_epigrafe_de_outro_numero(self):
        outro = PAGINA.replace("LEI Nº 17.543", "LEI Nº 17.999")
        with self.assertRaisesRegex(RuntimeError, "epígrafe"):
            s.converter_ato(self.ato(), outro.encode(), "LEI")

    def test_gate_data_do_titulo_ausente_no_corpo(self):
        ato = self.ato("lei-17543-de-24-de-dezembro-de-2020")
        with self.assertRaisesRegex(RuntimeError, "difere do slug"):
            s.converter_ato(ato, PAGINA.replace("23 DE DEZEMBRO DE 2020", "25 DE DEZEMBRO DE 2020").replace("em sessão", "em").encode(), "LEI")
        meta, *_ = s.converter_ato(ato, PAGINA.encode(), "LEI")
        self.assertEqual(meta["data"], "2020-12-23")

    def test_gate_sem_artigo_inicial(self):
        sem = PAGINA.replace("Art. 1º", "Parágrafo").replace("Art. 2º", "Parágrafo")
        with self.assertRaisesRegex(RuntimeError, "artigo inicial"):
            s.converter_ato(self.ato(), sem.encode(), "LEI")

    def test_gate_anexo_com_outra_epigrafe(self):
        extra = PAGINA.replace("</div>\n<div class=\"bx-leis", "<p>DECRETO Nº 60.052, DE 14 DE JANEIRO DE 2021</p></div>\n<div class=\"bx-leis")
        with self.assertRaisesRegex(RuntimeError, "outra norma"):
            s.converter_ato(self.ato(), extra.encode(), "LEI")

    def test_lei_complementar_pelo_titulo(self):
        self.assertEqual(s.categoria_do_ato("LEI", "LEI COMPLEMENTAR Nº 5 de 1 de Janeiro de 2000")[0], "leis-complementares")
        self.assertEqual(s.categoria_do_ato("DECRETO", "DECRETO Nº 5 de 1 de Janeiro de 2000")[0], "decretos")

    def test_nome_arquivo_estavel(self):
        meta, *_ = s.converter_ato(self.ato(), PAGINA.encode(), "LEI")
        a = s.nome_arquivo("leis-ordinarias", meta, "2020", "lei-17543-de-23-de-dezembro-de-2020")
        self.assertRegex(a.name, r"^leis-ordinarias-17543-2020-sp-[0-9a-f]{6}\.md$")
        self.assertEqual(a, s.nome_arquivo("leis-ordinarias", meta, "2020", "lei-17543-de-23-de-dezembro-de-2020"))

    def test_indice_confere_total_e_usa_cache(self):
        paginas = [BUSCA.replace("de 2 </strong>", "de 3 </strong>")]
        class Rede:
            def post(self, url, dados, tentativas=3):
                return 200, paginas[0].encode(), ""
        with tempfile.TemporaryDirectory() as tmp, patch.object(s, "CACHE", Path(tmp)):
            with self.assertRaisesRegex(RuntimeError, "diverge"):
                s.indice_tipo_ano(Rede(), "LEI", 2020)
            paginas[0] = BUSCA
            atos = s.indice_tipo_ano(Rede(), "LEI", 2020)
            self.assertEqual(len(atos), 2)
            self.assertTrue((Path(tmp) / "indice-LEI-2020.json").exists())
            class SemRede:
                def post(self, *a, **k):
                    raise AssertionError("cache deveria bastar")
            self.assertEqual(s.indice_tipo_ano(SemRede(), "LEI", 2020), atos)


if __name__ == "__main__":
    unittest.main()
