# Ingestao E Busca Semantica Com LangChain E PostgreSQL

Desafio Python para ingerir um PDF em PostgreSQL/pgVector e responder perguntas via CLI
usando somente o conteudo do documento.

## Estado Atual

Planejamento preparado; aplicacao ainda nao implementada. Nenhuma historia de produto
esta concluida e nao ha banco, credencial, pipeline ou modelo provisionados por este setup.

## Estrutura Da Entrega

```text
docker-compose.yml
requirements.txt
.env.example
src/
  ingest.py
  search.py
  chat.py
document.pdf
README.md
```

Esta e a estrutura-alvo, ainda nao implementada. O .gitignore tambem e versionado
como controle do repositorio. Documentacao interna de planejamento fica apenas local.

## Execucao Planejada

Python em venv, banco em Docker Compose e scripts src/ingest.py, src/search.py e
src/chat.py, conforme o enunciado. Os comandos reais de instalacao, configuracao e
uso serao validados nas historias e documentados aqui antes da entrega.

Escolha inicial de um provedor (OpenAI ou Gemini) e modelos ainda sera realizada; nao ha
promessa de gratuidade. Chaves, .env, documentos privados e logs sensiveis nao devem
ser publicados. Nenhum segredo e necessario para ler ou revisar o planejamento.
