"""Legislação municipal de Belo Horizonte/MG a partir do módulo "Pesquisar legislação" da Câmara Municipal.

Por que esta fonte: a Câmara Municipal (cmbh.mg.gov.br) mantém o SIL (Sistema de Informação
Legislativa) com leis, decretos legislativos, resoluções, emendas à Lei Orgânica e outros atos
desde o início do acervo, cada norma com o documento de texto integral anexado. A prefeitura
(pbh.gov.br) responde 403 a clientes automatizados e não é usada.

Como funciona (tudo sob o host oficial, respeitando robots.txt e o Crawl-delay: 10 dele):
  1. GET da página do módulo: lê os tipos de norma oferecidos (confere nomes e ids);
  2. POST em /sites/all/modules/consulta_normas/pesquisar.php (o mesmo que o formulário faz),
     um tipo por vez (e opcionalmente um ano), página a página (paginaRequerida=N, 7 itens por
     página; o resumo "itens de X a Y de um total de N" confere a contagem);
  3. POST em .../proxy.php com urlNorma=<id no SIL>: página da norma com data de promulgação,
     ementa, autoria e o link do anexo (.doc OLE2, às vezes .docx/.pdf);
  4. GET do anexo; scripts/comum.documento_para_md converte (soffice quando não há textutil).
Uma requisição a cada 10 s no total (listas, detalhes e anexos dividem o mesmo limitador).
Nunca contorna 403/429: a coleta para e relata. O cache torna a execução retomável.

Cache (fora do git): municipais/_indice/belo-horizonte/ (listas, detalhes, falhas.jsonl, totais)
e municipais/_fonte/belo-horizonte/ (anexos originais).
Saída: municipais/MG/belo-horizonte/<categoria>/<ano>/<categoria>-<numero>-<ano>-sil-<id>.md

Uso:
  python3 scripts/baixar_belo_horizonte.py --contar                       # totais por tipo (1 requisição/tipo)
  python3 scripts/baixar_belo_horizonte.py --contar --ano 2020            # totais do ano
  python3 scripts/baixar_belo_horizonte.py --tipos lei --ano 2020         # um tipo, um ano
  python3 scripts/baixar_belo_horizonte.py --tipos lei,decreto-legislativo --max-documentos 20
  python3 scripts/baixar_belo_horizonte.py                                # coleta completa (demorada)

Entra: lei, decreto, decreto-legislativo, decreto-lei, emenda-lo (emenda à Lei Orgânica),
lei-organica, resolucao e portaria (--tipos escolhe). Ficam de fora: "Ato do Presidente",
"Deliberação" e "Instrução de Serviço" (atos administrativos internos da Câmara) e normas sem
anexo. Em vez de gravar texto duvidoso, o coletor RETÉM em falhas.jsonl: conversão que falha,
corpo sem artigo inicial reconhecível, anexo com epígrafe de outra norma ou aviso de outra
publicação, anexo que não bate com a identidade da norma. Textos idênticos (hash) são gravados uma vez.
"""
import argparse
import datetime as dt
import gzip
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from comum import Rede, UA, documento_para_md, normalizar_chave, yaml_str
from granularidade_sapl import alertas_multiplos_atos, alertas_outras_publicacoes
from baixar_organicas_sapl import corpo_integral_reconhecivel

RAIZ = Path(__file__).resolve().parent.parent / "municipais"
BASE = "https://www.cmbh.mg.gov.br"
PAGINA = BASE + "/atividade-legislativa/pesquisar-legislacao"
PESQUISAR = BASE + "/sites/all/modules/consulta_normas/pesquisar.php"
PROXY = BASE + "/sites/all/modules/consulta_normas/proxy.php"
SIL = "https://cmbhsilint.cmbh.mg.gov.br/silinternet/servico/norma?id="
MUNICIPIO, UF, IBGE = "Belo Horizonte", "MG", "3106200"
PASTA = RAIZ / UF / "belo-horizonte"
INDICE = RAIZ / "_indice/belo-horizonte"
FONTE = RAIZ / "_fonte/belo-horizonte"

# chave -> (nome do tipo na fonte, categoria, rótulo canônico)
TIPOS = {
    "lei": ("Lei", "leis-ordinarias", "Lei ordinária"),
    "decreto": ("Decreto", "decretos", "Decreto"),
    "decreto-legislativo": ("Decreto Legislativo", "decretos-legislativos", "Decreto legislativo"),
    "decreto-lei": ("Decreto-Lei", "decretos-lei", "Decreto-lei"),
    "emenda-lo": ("Emenda à Lei Orgânica", "emendas-lei-organica", "Emenda à Lei Orgânica"),
    "lei-organica": ("Lei Orgânica", "lei-organica", "Lei Orgânica"),
    "resolucao": ("Resolução", "resolucoes", "Resolução"),
    "portaria": ("Portaria", "portarias", "Portaria"),
}
EXTENSOES = (".doc", ".docx", ".pdf", ".rtf", ".odt")


class Bloqueio(RuntimeError):
    """403/429 da fonte: a coleta para; não há contorno."""


def atraso_robots(texto):
    """Crawl-delay do grupo `User-agent: *` (segundos) ou None."""
    grupo = False
    for linha in texto.splitlines():
        linha = linha.split("#", 1)[0].strip()
        if not linha or ":" not in linha:
            continue
        k, v = (x.strip() for x in linha.split(":", 1))
        if k.lower() == "user-agent":
            grupo = v == "*"
        elif grupo and k.lower() == "crawl-delay":
            try:
                return float(v)
            except ValueError:
                return None
    return None


def proibidos_robots(texto):
    """Prefixos `Disallow` do grupo `User-agent: *`."""
    grupo, regras = False, []
    for linha in texto.splitlines():
        linha = linha.split("#", 1)[0].strip()
        if ":" not in linha:
            continue
        k, v = (x.strip() for x in linha.split(":", 1))
        if k.lower() == "user-agent":
            grupo = v == "*"
        elif grupo and k.lower() == "disallow" and v:
            regras.append(v)
    return regras


def permitido(caminho, regras):
    return not any(caminho.startswith(r) for r in regras if "*" not in r and "$" not in r)


class Fonte:
    """Rede com um único limitador (Crawl-delay) para o site e o host de download."""

    def __init__(self, intervalo):
        self.intervalo = intervalo
        self.rede = Rede(rps=1.0 / intervalo)

    def _checar(self, status, url):
        if status in (403, 429):
            raise Bloqueio(f"HTTP {status} em {url}; coleta interrompida (sem contornar)")

    def get(self, url):
        st, b, _ = self.rede.get(url, tentativas=3)
        self._checar(st, url)
        return st, b

    def post(self, url, campos, tentativas=3):
        corpo = urllib.parse.urlencode(campos).encode()
        ultimo = None
        for n in range(tentativas):
            self.rede.limite.esperar()
            req = urllib.request.Request(url, data=corpo, headers={
                "User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest", "Referer": PAGINA})
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    return r.status, r.read()
            except urllib.error.HTTPError as e:
                self._checar(e.code, url)
                if e.code in (400, 404, 410):
                    return e.code, b""
                ultimo = e
            except Exception as e:
                ultimo = e
            time.sleep(2 * (n + 1) ** 2)
        raise RuntimeError(f"POST falhou após {tentativas} tentativas: {url}: {ultimo}")


def decodificar(b):
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("cp1252", "replace")


def limpar(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def escrever(p, b):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
    tmp.write_bytes(b)
    tmp.replace(p)


# ---------------------------------------------------------------- análise das páginas

def parse_tipos(pagina):
    """{nome: id} do <select id="tipoDeNorma">."""
    m = re.search(r'<select name="tipoDeNorma".*?</select>', pagina, re.S)
    if not m:
        raise RuntimeError("seletor de tipos não encontrado na página do módulo")
    return {html.unescape(n).strip(): v for v, n in re.findall(r'<option[^>]*value="([^"]+)"[^>]*>([^<]*)</option>', m[0])}


def parse_lista(pagina):
    """-> (total, [{'sil_id','titulo','ementa'}]); total 0 quando não há resultado."""
    if "Nenhum resultado encontrado" in pagina:
        return 0, []
    r = re.search(r"itens de (\d+) a (\d+) de um total de (\d+)", pagina)
    if not r:
        raise RuntimeError("resumo de paginação ausente na lista")
    itens = []
    for bloco in re.findall(r"<li><h3>.*?</li>", pagina, re.S):
        c = re.search(r"data-caminho='[^']*?[?&]id=([0-9a-f]+)'[^>]*>([^<]+)</span>", bloco)
        if not c:
            raise RuntimeError("item de lista sem id do SIL")
        e = re.search(r"<strong>Ementa:\s*</strong>(.*?)</p>", bloco, re.S)
        itens.append({"sil_id": c[1], "titulo": limpar(c[2]), "ementa": limpar(e[1]) if e else ""})
    return int(r[3]), itens


def parse_titulo(titulo):
    m = re.fullmatch(r"(.+?)\s+-\s+(\S+?)/(\d{4})", titulo)
    return (m[1], m[2], m[3]) if m else None


def data_br(s):
    m = re.search(r"(\d{2})/(\d{2})/(\d{4})", s or "")
    return f"{m[3]}-{m[2]}-{m[1]}" if m else ""


def parse_detalhe(pagina):
    d = {"documentos": [], "fonte": "", "promulgacao": "", "publicacao": "", "ementa": "", "autoria": "", "promulgador": ""}
    u = re.search(r"id='novaUrl'[^>]*>([^<]+)<", pagina)
    d["fonte"] = u[1].strip() if u else ""
    t = re.search(r"<h2>([^<]+)</h2>", pagina)
    d["titulo"] = limpar(t[1]) if t else ""
    for nome, href in re.findall(r"<li><span>([^<]+)</span><a href=\"([^\"]+)\"", pagina):
        d["documentos"].append({"nome": html.unescape(nome).strip(), "url": html.unescape(href)})
    for chave, rotulo in (("promulgacao", "Data da promulgação"), ("publicacao", "Data da publicação")):
        m = re.search(rotulo + r":\s*</strong>\s*([^<]*)", pagina)
        d[chave] = data_br(m[1]) if m else ""
    m = re.search(r"<strong>Ementa:</strong>(.*?)</p>", pagina, re.S)
    d["ementa"] = limpar(m[1]) if m else ""
    m = re.search(r"<strong>Autoria:</strong>(.*?)</p>", pagina, re.S)
    d["autoria"] = limpar(m[1]) if m else ""
    m = re.search(r"Responsável pela Promulgação/Publicação:\s*</strong>([^<]*)", pagina)
    d["promulgador"] = limpar(m[1]) if m else ""
    return d


MESES = {m: i for i, m in enumerate(
    "janeiro fevereiro marco abril maio junho julho agosto setembro outubro novembro dezembro".split(), 1)}


def data_da_epigrafe(corpo):
    """Data 'DE 31 DE DEZEMBRO DE 2020' na epígrafe (primeiras linhas), quando a fonte não traz a promulgação."""
    for linha in corpo.splitlines()[:6]:
        m = re.search(r"(\d{1,2})\s*(?:o|º|°)?\s+de\s+([a-zç]+)\s+de\s+(\d{4})", normalizar_chave(linha))
        if m and m[2] in MESES:
            return f"{m[3]}-{MESES[m[2]]:02d}-{int(m[1]):02d}"
    return ""


def epigrafe_confere(corpo, tipo_fonte, numero):
    """O anexo deve citar o número da norma (com ou sem ponto de milhar) nas primeiras linhas."""
    alvo = re.sub(r"\D", "", numero)
    if not alvo:
        return True
    cabeca = corpo[:1500]
    return any(re.sub(r"\D", "", n) == alvo for n in re.findall(r"\d[\d.]*", cabeca))


# ---------------------------------------------------------------- coleta

class Coletor:
    def __init__(self, fonte, atualizar_listas=False, hoje=None):
        self.f = fonte
        self.atualizar = atualizar_listas
        self.hoje = hoje or dt.date.today().isoformat()
        self.ids_tipos = {}
        self.stats = {"gravados": 0, "ja_existentes": 0, "equivalentes": 0, "retidos": 0}
        self.motivos = {}
        self.ids_feitos = set()
        self.hashes = {}
        self.lista_indice = INDICE

    # --- cache
    def _cache(self, caminho, buscar):
        if caminho.exists() and not (self.atualizar and "listas" in caminho.parts):
            return caminho.read_bytes()
        st, b = buscar()
        if st != 200:
            raise RuntimeError(f"HTTP {st}")
        escrever(caminho, b)
        return b

    def abrir(self):
        regras = []
        st, robots = self.f.rede.get(BASE + "/robots.txt", tentativas=2)[:2]
        if st == 200:
            texto = robots.decode("utf-8", "replace")
            regras = proibidos_robots(texto)
            atraso = atraso_robots(texto)
            if atraso and atraso > self.f.intervalo:
                raise RuntimeError(f"intervalo ({self.f.intervalo}s) abaixo do Crawl-delay de {atraso}s")
        for url in (PESQUISAR, PROXY, PAGINA):
            if not permitido(urllib.parse.urlsplit(url).path, regras):
                raise RuntimeError(f"robots.txt proíbe {url}")
        pagina = decodificar(self._cache(self.lista_indice / "pagina-modulo.html", lambda: self.f.get(PAGINA)))
        reais = parse_tipos(pagina)
        for chave, (nome, _, _) in TIPOS.items():
            if nome not in reais:
                raise RuntimeError(f"tipo '{nome}' não consta mais no módulo de pesquisa")
            self.ids_tipos[chave] = reais[nome]
        PASTA.mkdir(parents=True, exist_ok=True)
        for arq in PASTA.glob("**/*.md"):
            texto = arq.read_text()
            mi = re.search(r'^id_sil: "([0-9a-f]+)"$', texto, re.M)
            mh = re.search(r'^sha256_texto: "([0-9a-f]{64})"$', texto, re.M)
            if mi:
                self.ids_feitos.add(mi[1])
            if mh:
                self.hashes.setdefault(mh[1], arq)

    def campos(self, chave, ano, pagina):
        return {"metodo": "", "paginaRequerida": pagina, "urlNorma": "", "idNorma": "", "buscarEmendas": "",
                "buscaViaUrl": "", "mobile": "0", "tipoDeNorma": self.ids_tipos[chave], "numero": "[número]",
                "ano": ano or "[ano]", "assunto": "[assunto]", "assunto2": "[assunto2]", "autoria": "[autoria]", "tema": ""}

    def lista(self, chave, ano, pagina):
        arq = self.lista_indice / "listas" / f"{chave}-{ano or 'todos'}-p{pagina}.html"
        campos = self.campos(chave, ano, pagina)
        return parse_lista(decodificar(self._cache(arq, lambda: self.f.post(PESQUISAR, campos))))

    def detalhe(self, sil_id):
        campos = self.campos("lei", None, 1)
        campos["urlNorma"] = SIL + sil_id
        arq = self.lista_indice / "detalhes" / f"{sil_id}.html"
        return decodificar(self._cache(arq, lambda: self.f.post(PROXY, campos)))

    def anexo(self, sil_id, doc):
        m = re.search(r"idDocDaNorma=([0-9a-f]+)", doc["url"])
        if not m:
            raise RuntimeError("link do anexo sem idDocDaNorma")
        host = urllib.parse.urlsplit(doc["url"]).hostname or ""
        if not host.endswith(".cmbh.mg.gov.br"):
            raise RuntimeError("anexo fora do domínio oficial da Câmara")
        arq = FONTE / f"{sil_id}-{m[1]}{Path(doc['nome']).suffix.lower()}"
        return self._cache(arq, lambda: self.f.get(doc["url"]))

    def contar(self, chaves, ano):
        saida = {}
        for chave in chaves:
            total, _ = self.lista(chave, ano, 1)
            saida[chave] = total
            print(f"{chave:20s} {ano or 'todos os anos'}: {total}", flush=True)
        return saida

    def reter(self, item, motivo, extra=None):
        self.stats["retidos"] += 1
        categoria = motivo.split(":")[0].split(";")[0][:70]
        self.motivos[categoria] = self.motivos.get(categoria, 0) + 1
        INDICE.mkdir(parents=True, exist_ok=True)
        with (INDICE / "falhas.jsonl").open("a") as f:
            f.write(json.dumps({"sil_id": item["sil_id"], "titulo": item["titulo"], "erro": motivo, **(extra or {}),
                                "quando": dt.datetime.now().isoformat(timespec="seconds")}, ensure_ascii=False) + "\n")
        print("RETIDO", item["titulo"], "-", motivo[:160], flush=True)

    def processar(self, chave, item):
        """Grava o .md de uma norma ou retém. Retorna 'gravado'|'existente'|'equivalente'|'retido'."""
        if item["sil_id"] in self.ids_feitos:
            self.stats["ja_existentes"] += 1
            return "existente"
        nome_fonte, categoria, rotulo = TIPOS[chave]
        t = parse_titulo(item["titulo"])
        if not t and chave == "lei-organica" and normalizar_chave(item["titulo"]) == normalizar_chave(nome_fonte):
            t = (nome_fonte, "", "")  # a Lei Orgânica não tem número; o ano vem da promulgação
        if not t:
            self.reter(item, "título fora do padrão 'Tipo - número/ano'")
            return "retido"
        tipo_lista, numero, ano = t
        if normalizar_chave(tipo_lista) != normalizar_chave(nome_fonte):
            self.reter(item, f"tipo do título ({tipo_lista}) diverge do tipo pesquisado ({nome_fonte})")
            return "retido"
        det = parse_detalhe(self.detalhe(item["sil_id"]))
        if normalizar_chave(det["titulo"]) != normalizar_chave(item["titulo"]):
            self.reter(item, f"página da norma ({det['titulo']}) diverge da lista")
            return "retido"
        if not ano:
            if not det["promulgacao"]:
                self.reter(item, "norma sem número/ano e sem data de promulgação")
                return "retido"
            ano = det["promulgacao"][:4]
        docs = [d for d in det["documentos"] if Path(d["nome"]).suffix.lower() in EXTENSOES]
        if not docs:
            self.reter(item, "norma sem anexo de texto integral")
            return "retido"
        if len(docs) > 1:
            self.reter(item, "mais de um anexo de texto; revisar qual é o ato integral",
                       {"anexos": [d["nome"] for d in docs]})
            return "retido"
        doc = docs[0]
        bruto = self.anexo(item["sil_id"], doc)
        corpo, motivo = documento_para_md(bruto, doc["nome"])
        if motivo:
            self.reter(item, motivo)
            return "retido"
        if not epigrafe_confere(corpo, nome_fonte, numero):
            self.reter(item, "anexo não cita o número da norma nas primeiras linhas; revisar identidade")
            return "retido"
        outras = alertas_outras_publicacoes(corpo)
        if outras:
            self.reter(item, "possível aviso/extrato de outra publicação no anexo; revisar",
                       {"alertas": outras[:2]})
            return "retido"
        multiplos = alertas_multiplos_atos(corpo, nome_fonte, numero)
        if multiplos:
            self.reter(item, "anexo contém epígrafe de outra norma; revisar granularidade", {"alertas": multiplos[:2]})
            return "retido"
        if not corpo_integral_reconhecivel(corpo, emenda=(chave == "emenda-lo")):
            self.reter(item, "texto sem artigo inicial reconhecível; revisar integralidade")
            return "retido"
        digest = hashlib.sha256(corpo.encode()).hexdigest()
        if digest in self.hashes:
            self.stats["equivalentes"] += 1
            INDICE.mkdir(parents=True, exist_ok=True)
            with (INDICE / "equivalencias.jsonl").open("a") as f:
                f.write(json.dumps({"sil_id": item["sil_id"], "titulo": item["titulo"],
                                    "igual_a": str(self.hashes[digest].relative_to(RAIZ)), "sha256_texto": digest}) + "\n")
            return "equivalente"
        data = det["promulgacao"] or data_da_epigrafe(corpo)
        origem = "data de promulgação do cadastro" if det["promulgacao"] else ("data da epígrafe do texto" if data else "sem data na fonte")
        versao = f"documento anexado à norma no SIL da Câmara Municipal de Belo Horizonte ({doc['nome']}); {origem}"
        meta = {"titulo": f"{rotulo} nº {numero}/{ano}" if numero else f"{rotulo} de {ano}", "tipo": rotulo, "tipo_na_fonte": nome_fonte, "numero": numero,
                "data": data, "municipio": MUNICIPIO, "uf": UF, "codigo_ibge": IBGE, "versao_texto": versao,
                "fonte": det["fonte"] or f"{PAGINA}/{chave}/{numero}/{ano}", "fonte_texto": doc["url"],
                "sha256_fonte": hashlib.sha256(bruto).hexdigest(), "sha256_texto": digest,
                "baixado_em": self.hoje, "id_sil": item["sil_id"]}
        if det["publicacao"]:
            meta["data_publicacao"] = det["publicacao"]
        ementa = det["ementa"] or item["ementa"]
        cab = ["---"] + [f"{k}: {yaml_str(v)}" for k, v in meta.items()] + ["---", "", f"# {meta['titulo']}", ""]
        if ementa and normalizar_chave(ementa)[:60] not in normalizar_chave(corpo[:3000]):
            cab += [f"> {ementa}", ""]
        arq = PASTA / categoria / ano / f"{'-'.join(filter(None, [categoria, normalizar_chave(numero).replace(' ', '-'), ano]))}-sil-{item['sil_id'][-8:]}.md"
        escrever(arq, "\n".join(cab + [corpo, ""]).encode())
        self.ids_feitos.add(item["sil_id"])
        self.hashes[digest] = arq
        self.stats["gravados"] += 1
        return "gravado"

    def coletar(self, chaves, ano, max_documentos):
        for chave in chaves:
            pagina, total, vistos = 1, None, set()
            while True:
                try:
                    total, itens = self.lista(chave, ano, pagina)
                except RuntimeError as e:
                    print(chave, "FALHA na lista", pagina, e, flush=True)
                    break
                for item in itens:
                    if max_documentos and self.stats["gravados"] >= max_documentos:
                        return
                    vistos.add(item["sil_id"])
                    try:
                        self.processar(chave, item)
                    except Bloqueio:
                        raise
                    except Exception as e:
                        self.reter(item, f"erro de coleta: {e}")
                if not itens or len(vistos) >= total:
                    break
                pagina += 1
            if total is not None and len(vistos) != total:
                print(chave, ano or "todos", f"AVISO: {len(vistos)} itens distintos de {total} oficiais", flush=True)
            escrever(INDICE / f"totais-{chave}-{ano or 'todos'}.json", json.dumps(
                {"tipo": chave, "ano": ano, "total_oficial": total, "itens_distintos": len(vistos),
                 "consultado_em": self.hoje}, ensure_ascii=False).encode())


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tipos", default=",".join(TIPOS), help="chaves separadas por vírgula: " + ", ".join(TIPOS))
    ap.add_argument("--ano", type=int, help="só as normas deste ano (coleta em frações)")
    ap.add_argument("--max-documentos", type=int, default=0, help="para após gravar N novos .md (0 = sem limite)")
    ap.add_argument("--contar", action="store_true", help="só imprime os totais oficiais por tipo")
    ap.add_argument("--intervalo", type=float, default=10.0, help="segundos entre requisições (mínimo: Crawl-delay do robots.txt)")
    ap.add_argument("--atualizar-listas", action="store_true", help="ignora o cache das listas (as demais etapas seguem em cache)")
    a = ap.parse_args(argv)
    chaves = [c for c in a.tipos.split(",") if c]
    if not chaves or any(c not in TIPOS for c in chaves):
        ap.error("tipo desconhecido; válidos: " + ", ".join(TIPOS))
    if a.intervalo < 10:
        ap.error("intervalo abaixo do Crawl-delay: 10 do robots.txt")
    if a.max_documentos < 0:
        ap.error("max-documentos deve ser não negativo")
    c = Coletor(Fonte(a.intervalo), a.atualizar_listas)
    try:
        c.abrir()
        if a.contar:
            c.contar(chaves, a.ano)
            return 0
        c.coletar(chaves, a.ano, a.max_documentos)
    except Bloqueio as e:
        print("BLOQUEIO:", e, flush=True)
        return 2
    print("resumo:", json.dumps(c.stats), "motivos de retenção:", json.dumps(c.motivos, ensure_ascii=False), flush=True)
    return 1 if c.stats["retidos"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
