"""Legislação municipal de São Paulo/SP (IBGE 3550308) pelo Catálogo de Legislação Municipal da Prefeitura.

Por que esta fonte: https://legislacao.prefeitura.sp.gov.br é o portal oficial da Prefeitura (Casa Civil / PRODAM)
com o texto de leis, decretos, emendas à Lei Orgânica e centenas de milhares de atos infralegais desde 1892.
O texto vem em HTML nativo (div "customStyle"), sem captcha, sem 403 e sem robots.txt efetivo (a rota devolve a
página inicial). Respeita-se rps <= 1 mesmo assim.

Como funciona:
  1. Índice: POST /busca (tipo[], ano-inicial, ano-final, limite=100) e POST /busca/pg/N com os mesmos campos.
     A busca limita-se a 10 000 resultados por consulta e exige um filtro de data; por isso o índice é montado
     por (tipo, ano). Cada resultado traz título, ementa e a "Situação" oficial (SEM REVOGAÇÃO EXPRESSA, REVOGADO,
     ALTERADO, ...). O total anunciado pelo portal é conferido contra os atos lidos; divergência aborta o par.
  2. Texto: GET /<slug> (a rota /leis/<slug> redireciona). O corpo é o <div class="col s12 customStyle"> que não
     é a ementa; o aviso "não substitui o original publicado no Diário Oficial" do portal vai para `versao_texto`.
  3. Gates: o ato é RETIDO em _indice/sao-paulo-catalogo/falhas.jsonl, e não gravado, quando o corpo está vazio
     (atos antigos só têm ementa), não tem artigo inicial reconhecível, traz epígrafe de outra norma ou aviso de
     outra publicação, ou a epígrafe do corpo diverge do título do índice. Textos idênticos já gravados são
     dedupados por hash (equivalencias.json).
  4. Retomável: índice por (tipo, ano) e HTML de cada ato ficam em cache (_indice/ e _fonte/, no .gitignore);
     atos cuja fonte já está num .md do município são pulados.

Saída: municipais/SP/sao-paulo/<categoria>/<ano>/<categoria>-<numero>-<ano>-sp-<id>.md, com `situacao_na_fonte`
(valor literal do portal) e `historico_na_fonte` (linhas da seção "Alterações" do portal, quando existirem).

Uso (a partir da raiz do repositório):
  python3 scripts/baixar_municipal_sao_paulo.py --ano 2020 --tipos LEI,DECRETO --max-documentos 20
  python3 scripts/baixar_municipal_sao_paulo.py --ano 1990-2026          # coleta completa dos tipos padrão
  python3 scripts/baixar_municipal_sao_paulo.py --ano 2020 --so-indice   # só contagens, sem baixar textos
  python3 scripts/baixar_municipal_sao_paulo.py --listar-tipos           # tipos que o catálogo oferece

O que entra (padrão): LEI (leis ordinárias e, se o título disser, leis complementares), DECRETO,
DECRETO LEGISLATIVO, EMENDA (à Lei Orgânica; o catálogo não tem nenhum ato EMENDA de 1892 a 2026).
Observado: a partir de 2000 todos os atos de LEI/DECRETO amostrados têm texto integral; antes disso a maioria só tem ementa e
é retida. A busca por ano pode devolver atos de data vizinha; o ano da pasta vem da data do título. Opcionais via --tipos: DECRETO LEI, RESOLUÇÃO, PORTARIA.
O que fica de fora: demais tipos do catálogo (instrução normativa, ordem interna, parecer, ofício, circular,
comunicado, despacho, projeto de lei, razões de veto etc., não são normas gerais ou não têm categoria canônica),
anexos em PDF/XLS dos atos (tabelas orçamentárias), o "Texto Consolidado" separado e atos sem texto integral na
fonte (só ementa), que ficam registrados como retidos.
"""
import argparse
import datetime as dt
import hashlib
import html as htmllib
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent))
from comum import Rede, yaml_str, slugify
from html2md import convert
from granularidade_sapl import alertas_multiplos_atos, alertas_outras_publicacoes
from baixar_organicas_sapl import corpo_integral_reconhecivel

RAIZ = Path(__file__).resolve().parent.parent / "municipais"
BASE = "https://legislacao.prefeitura.sp.gov.br"
MUNICIPIO, UF, IBGE = "São Paulo", "SP", "3550308"
PASTA = RAIZ / UF / "sao-paulo"
CACHE = RAIZ / "_indice/sao-paulo-catalogo"
FONTE = RAIZ / "_fonte/sao-paulo-catalogo"
LIMITE_CONSULTA = 10000

# tipo do catálogo -> (categoria canônica, rótulo do tipo)
TIPOS = {
    "LEI": ("leis-ordinarias", "Lei ordinária"),
    "DECRETO": ("decretos", "Decreto"),
    "DECRETO LEGISLATIVO": ("decretos-legislativos", "Decreto legislativo"),
    "DECRETO LEI": ("decretos-leis", "Decreto-lei"),
    "EMENDA": ("emendas-lei-organica", "Emenda à Lei Orgânica"),
    "RESOLUÇÃO": ("resolucoes", "Resolução"),
    "PORTARIA": ("portarias", "Portaria"),
}
PADRAO = ("LEI", "DECRETO", "DECRETO LEGISLATIVO", "EMENDA")
MESES = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro",
         "outubro", "novembro", "dezembro"]
MES_NUM = {m: i + 1 for i, m in enumerate(MESES)}
MES_NUM["marco"] = 3


class RedeSP(Rede):
    def post(self, url, dados, tentativas=3, timeout=120):
        """POST de formulário com o mesmo limitador e política de erros de Rede.get (4xx definitivos não repetem)."""
        req = urllib.request.Request(url, data=dados, headers={**self.headers, "Accept": "text/html",
                                     "Content-Type": "application/x-www-form-urlencoded"})
        ultimo = None
        for n in range(tentativas):
            self.limite.esperar()
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return r.status, r.read(), r.headers.get("Content-Type") or ""
            except urllib.error.HTTPError as e:
                if e.code in (400, 403, 404, 410):
                    return e.code, b"", ""
                ultimo = e
            except Exception as e:
                ultimo = e
            time.sleep(2 * (n + 1) ** 2)
        raise RuntimeError(f"falhou após {tentativas} tentativas: {url}: {ultimo}")


def escrever(p, b):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_bytes(b)
    tmp.replace(p)


def limpar(s):
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def categoria_do_ato(tipo, titulo):
    categoria, rotulo = TIPOS[tipo]
    if tipo == "LEI" and re.match(r"\s*LEI\s+COMPLEMENTAR", titulo, re.I):
        return "leis-complementares", "Lei complementar"
    return categoria, rotulo


def parse_resultados(pagina):
    """Retorna (total_anunciado, [ato]) de uma página de resultados da busca."""
    m = re.search(r'id="number-results">\s*(?:\d+\s*-\s*\d+\s+de\s+)?(\d+)', pagina)
    total = int(m[1]) if m else 0
    atos = []
    for bloco in re.split(r'<div class="bx-resultado">', pagina)[1:]:
        a = re.search(r'<a href="/leis/([^"/]+)">\s*<h4>(.*?)</h4>\s*(?:<p>(.*?)</p>)?', bloco, re.S)
        if not a:
            continue
        sit = re.search(r'nameMeta">Situa[çc][ãa]o</td>\s*<td>\s*(?:<p>)?(.*?)(?:</p>)?\s*</td>', bloco, re.S)
        atos.append({"slug": a[1], "titulo_indice": limpar(a[2]), "ementa": limpar(a[3] or ""),
                     "situacao": limpar(sit[1]) if sit else ""})
    return total, atos


def data_br(d):
    return d.strftime("%d/%m/%Y")


def faixas(ano, tipo, rede, particionar=False):
    if not particionar:
        yield f"{ano}", f"{ano}", f"{ano}"
        return
    for mes in range(1, 13):
        ini = dt.date(ano, mes, 1)
        fim = (dt.date(ano + (mes == 12), mes % 12 + 1, 1) - dt.timedelta(days=1))
        yield f"{ano}-{mes:02d}", data_br(ini), data_br(fim)


def indice_tipo_ano(rede, tipo, ano):
    """Atos do catálogo de um tipo em um ano, com cache. Confere o total anunciado pelo portal."""
    nome = re.sub(r"\W+", "_", tipo)
    cache = CACHE / f"indice-{nome}-{ano}.json"
    if cache.exists():
        return json.loads(cache.read_text())["atos"]
    atos, vistos = [], set()
    particionar = False
    while True:
        atos, vistos, estourou = [], set(), False
        for rotulo, ini, fim in faixas(ano, tipo, rede, particionar):
            campos = [("tipo[]", tipo), ("ano-inicial", ini), ("ano-final", fim), ("limite", "100")]
            pagina, total_faixa, lidos = 1, None, 0
            while True:
                url = f"{BASE}/busca" + ("" if pagina == 1 else f"/pg/{pagina}")
                st, b, _ = rede.post(url, urlencode(campos).encode(), tentativas=3)
                if st != 200:
                    raise RuntimeError(f"HTTP {st}: {url} {tipo} {rotulo}")
                total, itens = parse_resultados(b.decode("utf-8", errors="replace"))
                if total_faixa is None:
                    total_faixa = total
                    if total_faixa >= LIMITE_CONSULTA:
                        if particionar:
                            raise RuntimeError(f"{tipo} {rotulo}: consulta no limite de {LIMITE_CONSULTA}; dividir mais")
                        estourou = True
                        break
                lidos += len(itens)
                for it in itens:
                    if it["slug"] not in vistos:
                        vistos.add(it["slug"])
                        atos.append(it)
                if not itens or lidos >= total_faixa:
                    break
                pagina += 1
            if estourou:
                break
            if lidos != total_faixa:
                raise RuntimeError(f"{tipo} {rotulo}: lidos {lidos} diverge do total anunciado {total_faixa}")
        if estourou:
            particionar = True
            continue
        break
    escrever(cache, json.dumps({"tipo": tipo, "ano": ano, "total": len(atos), "atos": atos,
                                "consultado_em": dt.date.today().isoformat()}, ensure_ascii=False).encode())
    return atos


def div_balanceado(html, inicio):
    """Conteúdo interno do <div> que abre em `inicio` (posição de '<div'), com contagem de aninhamento."""
    fim_tag = html.index(">", inicio) + 1
    prof = 1
    for m in re.finditer(r"<div\b|</div\s*>", html[fim_tag:]):
        prof += 1 if m[0].startswith("<div") else -1
        if prof == 0:
            return html[fim_tag:fim_tag + m.start()]
    return html[fim_tag:]


def extrair_pagina(html):
    """Retorna (corpo_html, avisos, historico) da página de um ato. corpo_html vazio se a fonte não traz texto."""
    corpo, avisos = "", []
    for m in re.finditer(r'<div class="col s12 customStyle">', html):
        corpo = div_balanceado(html, m.start())
        break
    for m in re.finditer(r'<div class="bx-mensagem[^"]*">', corpo):
        avisos.append(limpar(div_balanceado(corpo, m.start())))
    corpo = re.sub(r'<div class="bx-mensagem[^"]*">.*?</div>', "", corpo, flags=re.S)
    historico = []
    h = re.search(r'<div id="historico">', html)
    if h:
        for p in re.findall(r"<p>(.*?)</p>", div_balanceado(html, h.start()), re.S):
            if limpar(p):
                historico.append(limpar(p))
    return corpo, [a for a in avisos if a], historico


def identidade(titulo_indice, slug):
    """(numero, ano, data iso) a partir do título do índice; o número é conferido contra o slug."""
    m = re.search(r"N[ºo°]?\s*([\d.]+(?:\s?-\s?[A-Za-z]+)?)\s+de\s+(\d{1,2})º?\s+de\s+(\w+)\s+de\s+(\d{4})", titulo_indice, re.I)
    if not m:
        raise RuntimeError("título do índice sem número e data reconhecíveis: " + titulo_indice)
    mes = MES_NUM.get(m[3].lower())
    if not mes:
        raise RuntimeError("mês não reconhecido: " + titulo_indice)
    data = dt.date(int(m[4]), mes, int(m[2]))
    num = re.sub(r"[^a-z0-9]+", "-", m[1].lower().replace(".", "")).strip("-")
    if f"-{num}-de-" not in slug:
        raise RuntimeError(f"número do título e do slug divergem: {titulo_indice} / {slug}")
    return m[1].replace(" ", "").upper(), str(data.year), data.isoformat()


def slug_confere_data(slug, data_iso):
    d = dt.date.fromisoformat(data_iso)
    return slug.endswith(f"-de-{d.day}-de-{MESES[d.month - 1].replace('ç', 'c')}-de-{d.year}")


def corpo_cita_data(corpo, data_iso):
    """Quando o slug traz outra data (publicação), o corpo precisa citar a data do título (assinatura)."""
    d = dt.date.fromisoformat(data_iso)
    cabeca = corpo.lower()
    dia = "1[ºo°]?" if d.day == 1 else str(d.day)
    return bool(re.search(rf"\b0?{dia}\s*(?:º|°)?\s*de\s+{MESES[d.month - 1]}\s+de\s+{d.year}\b", cabeca) or
                re.search(rf"\b0?{d.day}/0?{d.month}/{d.year}\b", cabeca))


def numero_fmt(numero):
    digitos = re.match(r"\d+", numero)
    if not digitos:
        return numero
    d = digitos[0]
    return f"{int(d):,}".replace(",", ".") + numero[len(d):]


def titulo_md(categoria, numero, data_iso, ementa, orgao=""):
    d = dt.date.fromisoformat(data_iso)
    dia = "1º" if d.day == 1 else str(d.day)
    nome = {"leis-ordinarias": "Lei", "leis-complementares": "Lei complementar", "decretos": "Decreto",
            "decretos-legislativos": "Decreto legislativo", "decretos-leis": "Decreto-lei",
            "emendas-lei-organica": "Emenda à Lei Orgânica", "resolucoes": "Resolução",
            "portarias": "Portaria"}[categoria]
    base = f"{nome} {orgao + ' ' if orgao else ''}nº {numero_fmt(numero)}, de {dia} de {MESES[d.month - 1]} de {d.year}"
    return f"{base} — {ementa}" if ementa and len(ementa) <= 160 else base


def epigrafe_confere(corpo, numero):
    """O início do corpo precisa citar o número do índice (evita corpo de outro ato); zeros à esquerda não contam."""
    alvo = str(int(re.sub(r"\D", "", numero) or 0))
    cabeca = re.sub(r"(?<=\d)\.(?=\d)", "", re.sub(r"[\s*#]+", " ", corpo[:500]))
    return alvo in [str(int(x)) for x in re.findall(r"\d+", cabeca)]


def orgao_do_titulo(titulo_indice, tipo):
    """Resolução e portaria repetem o número entre órgãos: o órgão emissor vem entre o tipo e o 'Nº' do título."""
    m = re.match(r"\s*" + re.escape(tipo) + r"\s+(.+?)\s+N[ºo°]\s*\d", titulo_indice, re.I)
    return m[1].strip() if m else ""


def fontes_existentes():
    fontes, hashes = set(), {}
    for p in PASTA.glob("**/*.md"):
        t = p.read_text()
        mf = re.search(r'^fonte: (".*")$', t, re.M)
        mh = re.search(r'^sha256_texto: "([a-f0-9]{64})"$', t, re.M)
        if mf:
            fontes.add(json.loads(mf[1]))
        if mh:
            hashes.setdefault(mh[1], []).append(p)
    return fontes, hashes


def registrar_falha(ato, erro):
    CACHE.mkdir(parents=True, exist_ok=True)
    with (CACHE / "falhas.jsonl").open("a") as f:
        f.write(json.dumps({"slug": ato["slug"], "tipo": ato.get("tipo"), "erro": erro,
                            "quando": dt.datetime.now().isoformat()}, ensure_ascii=False) + "\n")


def converter_ato(ato, bruto, tipo):
    """HTML do ato -> (meta, corpo_md). Levanta RuntimeError com o motivo de retenção."""
    html = bruto.decode("utf-8", errors="replace")
    numero, ano, data = identidade(ato["titulo_indice"], ato["slug"])
    corpo_html, avisos, historico = extrair_pagina(html)
    if not limpar(corpo_html):
        raise RuntimeError("fonte sem texto integral (apenas ementa)")
    corpo = convert(corpo_html).replace("\u00ad", "").strip()
    if len(re.sub(r"\W", "", corpo)) < 40:
        raise RuntimeError("fonte sem texto integral (corpo vazio)")
    categoria, rotulo = categoria_do_ato(tipo, ato["titulo_indice"])
    if not epigrafe_confere(corpo, numero):
        raise RuntimeError("epígrafe do corpo não cita o número do índice; revisar identidade")
    if not slug_confere_data(ato["slug"], data) and not corpo_cita_data(corpo, data):
        raise RuntimeError(f"data do título ({data}) difere do slug e não consta no corpo; revisar")
    if alertas_outras_publicacoes(corpo):
        raise RuntimeError("possível aviso/extrato de outra publicação; revisar")
    gran = alertas_multiplos_atos(corpo, rotulo, numero)
    if gran:
        raise RuntimeError("corpo contém epígrafe de outra norma; revisar granularidade: " +
                           json.dumps(gran, ensure_ascii=False)[:400])
    if not corpo_integral_reconhecivel(corpo, tipo == "EMENDA"):
        raise RuntimeError("texto sem artigo inicial reconhecível; revisar integralidade")
    notas = ["texto do Catálogo de Legislação Municipal da Prefeitura de São Paulo"]
    notas += avisos
    if "~~" in corpo:
        notas.append("trechos tachados na fonte (revogados, suspensos ou declarados inconstitucionais) preservados entre ~~")
    orgao = orgao_do_titulo(ato["titulo_indice"], tipo) if categoria in ("resolucoes", "portarias") else ""
    meta = {"titulo": titulo_md(categoria, numero, data, ato["ementa"], orgao), "tipo": rotulo, "tipo_na_fonte": tipo,
            "numero": re.sub(r"[.]", "", numero), "data": data, "municipio": MUNICIPIO, "uf": UF,
            "codigo_ibge": IBGE, "versao_texto": "; ".join(notas),
            "fonte": f"{BASE}/{ato['slug']}", "fonte_texto": f"{BASE}/{ato['slug']}",
            "sha256_fonte": hashlib.sha256(bruto).hexdigest(),
            "sha256_texto": hashlib.sha256(corpo.encode()).hexdigest(),
            "baixado_em": dt.date.today().isoformat()}
    if orgao:
        meta["orgao_na_fonte"] = orgao
    if ato.get("situacao"):
        meta["situacao_na_fonte"] = ato["situacao"]
    if historico:
        meta["historico_na_fonte"] = historico
    return meta, corpo, categoria, ano


def montar_md(meta, corpo):
    linhas = ["---"] + [f"{k}: {yaml_str(v)}" for k, v in meta.items()] + ["---", "", f"# {meta['titulo']}", "", corpo, ""]
    return "\n".join(linhas)


def nome_arquivo(categoria, meta, ano, slug):
    numero = slugify(meta["numero"])
    return PASTA / categoria / ano / f"{categoria}-{numero}-{ano}-sp-{hashlib.sha1(slug.encode()).hexdigest()[:6]}.md"


def executar(anos, tipos, rps, max_documentos=0, so_indice=False):
    rede = RedeSP(rps=rps)
    fontes, hashes = fontes_existentes()
    eq_path = CACHE / "equivalencias.json"
    equivalencias = json.loads(eq_path.read_text()) if eq_path.exists() else {}
    resumo = {"gravados": 0, "pulados": 0, "equivalentes": 0, "retidos": 0, "indice": {}}
    processados = 0
    for ano in anos:
        for tipo in tipos:
            try:
                atos = indice_tipo_ano(rede, tipo, ano)
            except Exception as e:
                registrar_falha({"slug": f"indice:{tipo}:{ano}", "tipo": tipo}, str(e))
                print(tipo, ano, "FALHA índice", e, flush=True)
                continue
            resumo["indice"][f"{tipo}|{ano}"] = len(atos)
            if so_indice:
                print(tipo, ano, len(atos), flush=True)
                continue
            for ato in atos:
                ato["tipo"] = tipo
                fonte = f"{BASE}/{ato['slug']}"
                if fonte in fontes:
                    resumo["pulados"] += 1
                    continue
                if max_documentos and processados >= max_documentos:
                    continue
                try:
                    arq_fonte = FONTE / f"{ato['slug']}.html"
                    if arq_fonte.exists():
                        bruto = arq_fonte.read_bytes()
                    else:
                        st, bruto, _ = rede.get(f"{BASE}/leis/{ato['slug']}", tentativas=3)
                        processados += 1
                        if st != 200:
                            raise RuntimeError(f"HTTP {st}")
                        escrever(arq_fonte, bruto)
                    meta, corpo, categoria, ano_doc = converter_ato(ato, bruto, tipo)
                    iguais = hashes.get(meta["sha256_texto"], [])
                    if iguais:
                        equivalencias[ato["slug"]] = {"arquivo": str(iguais[0].relative_to(RAIZ)), "fonte": fonte,
                                                      "sha256_texto": meta["sha256_texto"]}
                        escrever(eq_path, json.dumps(equivalencias, ensure_ascii=False).encode())
                        resumo["equivalentes"] += 1
                        continue
                    arq = nome_arquivo(categoria, meta, ano_doc, ato["slug"])
                    escrever(arq, montar_md(meta, corpo).encode())
                    fontes.add(fonte)
                    hashes.setdefault(meta["sha256_texto"], []).append(arq)
                    resumo["gravados"] += 1
                except Exception as e:
                    resumo["retidos"] += 1
                    registrar_falha(ato, str(e))
                    print(tipo, ano, ato["slug"], "RETIDO", str(e)[:160], flush=True)
        print("ano", ano, {k: v for k, v in resumo.items() if k != "indice"}, flush=True)
    return resumo


def listar_tipos(rede):
    st, b, _ = rede.get(BASE + "/", tentativas=3)
    if st != 200:
        raise RuntimeError(f"HTTP {st}")
    for t in re.findall(r'name="tipo\[\]"[^>]*value="([^"]+)"', b.decode("utf-8", errors="replace")):
        print(htmllib.unescape(t), "->", TIPOS[t][0] if t in TIPOS else "(fora do escopo)")


def parse_anos(s):
    m = re.fullmatch(r"(\d{4})(?:-(\d{4}))?", s)
    if not m:
        raise argparse.ArgumentTypeError("use AAAA ou AAAA-AAAA")
    a, b = int(m[1]), int(m[2] or m[1])
    if not 1800 <= a <= b <= dt.date.today().year:
        raise argparse.ArgumentTypeError("intervalo de anos inválido")
    return list(range(b, a - 1, -1))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ano", type=parse_anos, help="AAAA ou AAAA-AAAA (obrigatório: a busca exige filtro de data)")
    ap.add_argument("--tipos", default=",".join(PADRAO), help="tipos do catálogo separados por vírgula; padrão: " + ",".join(PADRAO))
    ap.add_argument("--max-documentos", type=int, default=0, help="limita os atos baixados nesta execução (0 = sem limite)")
    ap.add_argument("--rps", type=float, default=1.0)
    ap.add_argument("--so-indice", action="store_true", help="monta/consulta só o índice (contagens)")
    ap.add_argument("--listar-tipos", action="store_true")
    a = ap.parse_args()
    if not 0 < a.rps <= 1:
        ap.error("rps deve ser maior que zero e no máximo 1")
    if a.max_documentos < 0:
        ap.error("max-documentos deve ser não negativo")
    if a.listar_tipos:
        listar_tipos(RedeSP(rps=a.rps))
        raise SystemExit(0)
    if not a.ano:
        ap.error("informe --ano")
    tipos = [t.strip().upper() for t in a.tipos.split(",") if t.strip()]
    invalidos = [t for t in tipos if t not in TIPOS]
    if invalidos:
        ap.error("tipos sem categoria canônica: " + ", ".join(invalidos) + "; válidos: " + ", ".join(TIPOS))
    r = executar(a.ano, tipos, a.rps, a.max_documentos, a.so_indice)
    print(json.dumps({k: v for k, v in r.items() if k != "indice"}, ensure_ascii=False))
    raise SystemExit(1 if r["retidos"] else 0)
