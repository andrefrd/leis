# Coleta municipal em lote (SAPL + capitais)

Jobs que alimentam `municipais/` com a legislação dos municípios de **SP, MG e SC**. São três peças:

| Script | O que faz |
| --- | --- |
| `coletar_sapl.sh` | roda `scripts/baixar_organicas_sapl.py` para cada host da lista, N em paralelo |
| `commitar_continuo.sh` | a cada 10 min, audita e commita cada host concluído (1 commit por município) e faz push |
| `commitar_host.sh` | o commit de um host: stage → `auditar.py --staged` → `auditar_municipal.py --staged` → commit |
| `status.sh` | resumo: hosts por estado, `.md` pendentes, motivos de retenção, disco |

A lista `hosts.txt` (223 hosts, cerca de 711 mil registros na API) vem do catálogo SIGI do Interlegis,
filtrado para SP, MG e SC, sondando `/api/norma/normajuridica/` e ordenando por população. `hosts.json` traz
`total_entries` e a população de cada host.

## Pré-requisitos (Linux)

```sh
sudo apt install python3 poppler-utils tesseract-ocr tesseract-ocr-por libreoffice-writer-nogui git
```

- `pdftotext`/`pdftoppm` (poppler) extraem o texto dos PDFs, Tesseract faz o OCR dos PDFs escaneados e o LibreOffice
  converte DOC/DOCX/ODT/RTF. Tudo isso exige a branch `feat/ocr-pdf-tesseract` (PR #2), porque sem ela, no Linux,
  PDF escaneado e DOC viram falha.
- Opcional, mas melhora o OCR: modelo `tessdata_best` verificado por SHA-256 (o coletor confere o hash).
  ```sh
  mkdir -p scripts/bin/tessdata
  curl -L -o scripts/bin/tessdata/por.traineddata \
    https://raw.githubusercontent.com/tesseract-ocr/tessdata_best/main/por.traineddata
  sha256sum scripts/bin/tessdata/por.traineddata   # 711de9dbb8052067bd42f16b9119967f30bada80d57e2ef24f65d09f531adb04
  ```
- Disco: os originais vão para `municipais/_fonte/` (fora do Git), cerca de 0,85 MB por norma. A coleta pausa
  sozinha quando o disco fica abaixo de `MIN_GB` (60 GB por padrão).
- Clone completo do repositório (cerca de 18 GB) e `gh auth setup-git` para o push.

## Rodar em outro PC

```sh
git clone https://github.com/andrefrd/leis.git && cd leis
git remote add upstream https://github.com/ctresb/leis.git
git config core.hooksPath .githooks            # o pre-commit atualiza PROGRESSO.md
git config core.untrackedCache true            # git status/add bem mais rápidos com 1,1 milhão de arquivos
git config feature.manyFiles true
git fetch origin feat/jobs-coleta-municipal
git checkout -b feat/municipais-sapl-pc2 origin/feat/jobs-coleta-municipal

# terminal 1: coleta (use tmux/screen ou systemd --user: são horas ou dias)
HOSTS=scripts/coleta_municipal/hosts-pc2.txt PAR=8 scripts/coleta_municipal/coletar_sapl.sh

# terminal 2: commits + push contínuos
BRANCH=feat/municipais-sapl-pc2 scripts/coleta_municipal/commitar_continuo.sh

# a qualquer momento
HOSTS=scripts/coleta_municipal/hosts-pc2.txt scripts/coleta_municipal/status.sh
```

Logs e marcadores ficam em `../leis-coleta-logs/`, fora do repositório (ajuste com `LOGS=`). Variáveis
aceitas: `REPO`, `LOGS`, `HOSTS`, `PAR`, `MIN_GB`, `BRANCH`, `INTERVALO`.

### Divisão entre os PCs

| Lista | Hosts | Registros na API | Onde roda |
| --- | ---: | ---: | --- |
| `hosts-pc1.txt` | 104 | cerca de 356 mil | PC original (inclui os hosts já iniciados lá, Campinas entre eles) |
| `hosts-pc2.txt` | 119 | cerca de 356 mil | segundo PC, nenhum host iniciado |

**Nunca rode o mesmo host em dois PCs.** Os arquivos saem com o mesmo caminho e `baixado_em` diferente, o que gera
conflito no merge, e a câmara recebe o dobro de requisições. Cada PC usa a sua branch (`feat/municipais-sapl` e
`feat/municipais-sapl-pc2`) e abre o seu próprio PR. Para levar tudo para um PC só, use `hosts.txt` inteiro e
pare a coleta no outro.

## Como funciona

1. **Coleta** (`coletar_sapl.sh`): `xargs -P` chama `baixar_organicas_sapl.py --descobrir <host> --modo legislacao --rps 1`.
   - O coletor valida a identidade IBGE da câmara e pagina o índice de normas.
   - Para cada norma, baixa o texto integral (PDF/DOC) e grava o `.md` com cabeçalho YAML.
   - Gates de integridade **retêm** em vez de gravar texto duvidoso: sem texto integral, sem artigo inicial reconhecível,
     anexo com epígrafe de outra norma (granularidade), aviso de outra publicação. As retenções vão para
     `municipais/_indice/organicas-sapl/<host>/falhas.jsonl` e para o log do host.
   - A coleta é retomável: o `.md` que já existe é pulado (dedupe pela `fonte`) e o hash igual é registrado como
     equivalência. Uma trava `flock` por host impede duas instâncias do mesmo host.
2. **Estado por host** (em `LOGS/`): `<host>.log`, `.done` (concluído), `.erro` (abortou sem resumo),
   `.commitado`. **`rc=1` não é erro**: o coletor sai com 1 quando houve normas retidas. O que conta é a linha de
   resumo `IBGE <código> novos N já existentes M`.
3. **Commit** (`commitar_host.sh`):
   - Pega os `.md` não rastreados que citam o host e os põe no índice com **um único** `git add --pathspec-from-file`.
     Cada `git add` regrava o índice inteiro, e com xargs isso levava minutos.
   - Roda as duas auditorias sobre o lote staged. Se alguma falhar, desfaz o stage e tenta de novo no próximo ciclo.
   - Commita com a contagem por categoria e o número de retenções. O hook `pre-commit` regrava `PROGRESSO.md` e
     `progresso.svg`.
   - Recusa rodar se o índice já tiver algo staged, para não misturar municípios num commit.

## Armadilhas já vistas

- **Processo órfão**: matar o `coletar_sapl.sh` não mata o `xargs` nem os Python filhos. Antes de reiniciar,
  confira com `pgrep -af baixar_organicas_sapl` e encerre pelos PIDs. Dois executores sobre os mesmos hosts
  duplicam o tráfego.
- **`index.lock` órfão**: se um `git add` ou commit for interrompido, sobra `.git/index.lock` vazio. Confirme que
  nenhum git está rodando, apague o arquivo e rode `git reset -q` antes de voltar a commitar.
- **Não edite um `.sh` enquanto ele roda**: o bash lê o script aos poucos. Para trocar, grave um arquivo novo e
  faça `mv`.
- **`pkill -f <padrão>`** casa com o próprio shell que contém o padrão. Prefira matar por PID.
- **Hosts lentos ou grandes** (Campinas, cerca de 58 mil registros) levam mais de um dia a `--rps 1`. Não aumente o rps.

## Capitais (coletores próprios, fora desta lista)

| Cidade | Branch / PR | Comando | Ritmo |
| --- | --- | --- | --- |
| São Paulo | `feat/municipal-sao-paulo` (#4) | `python3 -I scripts/baixar_municipal_sao_paulo.py --ano 2000-2026 --tipos 'LEI,DECRETO,DECRETO LEGISLATIVO,EMENDA,DECRETO LEI,RESOLUÇÃO,PORTARIA' --rps 1`, depois `--ano 1892-1999` | cerca de 2 dias |
| Belo Horizonte | `feat/municipal-belo-horizonte` (#5) | `python3 -I scripts/baixar_belo_horizonte.py` | crawl-delay de 10 s, cerca de 9 dias |

As duas já estão rodando no PC original, cada uma na sua worktree. **Não as rode também no segundo PC**: o BH,
em especial, violaria o crawl-delay da Câmara.

Ficam fora pelas regras do repositório (não contornar bloqueio): Florianópolis (reCAPTCHA), Joinville
(`robots.txt` com `Disallow: /`) e Uberlândia (captcha no texto integral).
