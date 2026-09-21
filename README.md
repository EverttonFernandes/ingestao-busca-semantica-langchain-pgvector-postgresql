# Ingestão e busca semântica com LangChain e PostgreSQL

Desafio do MBA: ingerir um PDF em PostgreSQL/pgVector e responder via terminal somente
com base no documento. Fonte: [repositório oficial](https://github.com/devfullcycle/mba-ia-desafio-ingestao-busca).

## Estado atual

Setup de ambiente (RAGT-001). Os scripts `src/ingest.py`, `src/search.py` e `src/chat.py`
são placeholders: exibem a funcionalidade pendente e encerram com código 1.
Ingestão, busca e chat serão implementados nas próximas histórias. O PDF existente
não foi validado como referência; a escolha do documento, provedor e modelos pertence
à RAGT-002. Nenhuma chave de modelo é necessária para este setup.

O GitHub informa que o repositório atual não é um fork do exemplo. Regularizar a
origem conforme o enunciado é uma pendência manual que bloqueia a entrega final.

## Pré-requisitos e Python

Python 3.10+ (validação local com 3.14.4), Docker e Docker Compose com daemon ativo.
Na raiz do projeto:

```sh
python3 -m venv venv
source venv/bin/activate
python -m pip install -r requirements.txt
python -m pip check
```

Se a distribuição não incluir `ensurepip`, instale o pacote de venv correspondente
(por exemplo `python3.14-venv` no Debian/Ubuntu). Alternativamente, o método
[oficial do pip](https://pip.pypa.io/en/stable/installation/) funciona dentro da venv:

```sh
python3 -m venv --without-pip venv
curl -fsSLo /tmp/rag-get-pip.py https://bootstrap.pypa.io/get-pip.py
venv/bin/python /tmp/rag-get-pip.py
source venv/bin/activate
python -m pip install -r requirements.txt
python -m pip check
```

As dependências diretas estão fixadas em `requirements.txt`. Integrações específicas
de OpenAI ou Gemini serão adicionadas após a escolha do provedor.

## Configurar e subir o banco

Na primeira execução, copie o template (preserve seu `.env` se já existir):

```sh
cp -n .env.example .env
```

Preencha `POSTGRES_PASSWORD` no `.env` com uma senha local. Usuário, banco e porta
padrão são `postgres`, `rag` e `5432`. `POSTGRES_HOST=127.0.0.1` é o endereço para o
Python executado na máquina; o bootstrap usa o hostname interno `postgres`.
O Compose rejeita senha ausente ou vazia com um diagnóstico. Não versione `.env`.

```sh
docker compose config --quiet
docker compose up -d
docker compose ps -a
docker compose logs bootstrap_vector_ext
```

O serviço `postgres` deve ficar saudável e `bootstrap_vector_ext` deve terminar com
código 0. O bootstrap executa `CREATE EXTENSION IF NOT EXISTS vector` e pode ser
repetido. Aguarde sua conclusão antes de executar a aplicação.

```sh
docker compose exec postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT extname, extversion FROM pg_extension WHERE extname = '\''vector'\'';"'
```

Para verificar a conexão TCP do Python e uma operação vetorial, com a venv ativa:

```sh
python - <<'PY'
from dotenv import dotenv_values
import psycopg

config = dotenv_values('.env')
with psycopg.connect(
    host=config['POSTGRES_HOST'], port=config['POSTGRES_PORT'],
    user=config['POSTGRES_USER'], password=config['POSTGRES_PASSWORD'],
    dbname=config['POSTGRES_DB'], connect_timeout=5,
) as conn:
    print(conn.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'").fetchone())
    print(conn.execute("SELECT '[1,2,3]'::vector <-> '[1,2,4]'::vector").fetchone())
PY
```

Espera-se a versão da extensão e distância `1.0`.

## Adaptações do Compose oficial

Mantidos PostgreSQL 17 com pgVector, volume persistente e serviço de bootstrap.
Credenciais saíram do YAML e são compartilhadas pelos serviços via `.env`;
o healthcheck acompanha usuário/banco configurados. A porta fica vinculada a
`127.0.0.1` e pode mudar com `POSTGRES_PORT` se 5432 estiver ocupada. O nome fixo de
container foi removido para evitar colisões entre projetos Compose.

O volume `postgres_data` preserva os dados. Alterar usuário, senha ou banco no `.env`
não altera as credenciais de um volume já inicializado. Para parar sem apagar dados:

```sh
docker compose down
```

## Próximas etapas

A implementação seguirá chunks de 1.000 caracteres, overlap de 150 e busca `k=10`.
Após implementar ingestão e chat, a ordem será:

```sh
python src/ingest.py
python src/chat.py
```

A resposta obrigatória para informação ausente será:
“Não tenho informações necessárias para responder sua pergunta.”
