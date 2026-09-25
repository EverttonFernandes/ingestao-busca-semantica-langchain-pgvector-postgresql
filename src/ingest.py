"""Ingestao de PDF: configuracao -> chunks -> embeddings -> PostgreSQL.

Para acompanhar o fluxo, comece por main(), depois run() e ingest().
As demais funcoes detalham cada etapa, sem camadas adicionais.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv
from langchain_community.document_loaders import PyPDFLoader
from langchain_core.documents import Document
from langchain_openai import OpenAIEmbeddings
from langchain_postgres import PGVector
from langchain_text_splitters import RecursiveCharacterTextSplitter
from openai import APIConnectionError, APIError, AuthenticationError, RateLimitError
from sqlalchemy import URL, create_engine, text
from sqlalchemy.engine import Connection, Engine, RowMapping
from sqlalchemy.exc import SQLAlchemyError

ROOT = Path(__file__).resolve().parents[1]
CHUNK_SIZE = 1000
CHUNK_OVERLAP = 150
INGESTION_LOCK_ID = 724001
# Preco standard consultado em 22/09/2026; estimativa, nao controle de faturamento.
PRICE_PER_MILLION = {"text-embedding-3-small": 0.02}


class IngestError(Exception):
    """Diagnostico seguro para o operador, sem detalhes de credenciais ou SQL."""


@dataclass(frozen=True)
class Settings:
    """Agrupa a configuracao sem alteracoes posteriores (frozen=True).

    repr=False evita mostrar credenciais ao inspecionar este objeto.
    """

    model: str
    dimensions: int
    collection: str
    api_key: str = field(repr=False)
    database_url: URL = field(repr=False)

    @classmethod
    def from_env(cls) -> Settings:
        try:
            dimensions = int(os.getenv("OPENAI_EMBEDDING_DIMENSIONS", "1536"))
            port = int(os.getenv("POSTGRES_PORT", "5432"))
            if dimensions <= 0 or not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            raise IngestError(
                "Confira OPENAI_EMBEDDING_DIMENSIONS e POSTGRES_PORT: "
                "use inteiros positivos validos."
            ) from None
        model = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small").strip()
        collection = os.getenv("PGVECTOR_COLLECTION", "document_pdf").strip()
        if not model or not collection:
            raise IngestError("OPENAI_EMBEDDING_MODEL e PGVECTOR_COLLECTION nao podem estar vazios.")
        database_url = URL.create(
            "postgresql+psycopg",
            username=os.getenv("POSTGRES_USER", "postgres"),
            password=os.getenv("POSTGRES_PASSWORD", ""),
            host=os.getenv("POSTGRES_HOST", "127.0.0.1"),
            port=port,
            database=os.getenv("POSTGRES_DB", "rag"),
        )
        return cls(
            model=model,
            dimensions=dimensions,
            collection=collection,
            api_key=os.getenv("OPENAI_API_KEY", "").strip(),
            database_url=database_url,
        )


def prepare_pdf(path: Path) -> tuple[list[Document], str]:
    """Preserva pagina zero-based e origem; nao envia o documento a rede."""
    if not path.is_file():
        raise IngestError("PDF ausente. Coloque document.pdf na raiz ou use --pdf CAMINHO.")
    try:
        file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        pages = PyPDFLoader(str(path)).load()
    except Exception:
        raise IngestError(
            "Nao foi possivel ler o PDF. Confira se e valido, legivel "
            "e nao protegido por senha."
        ) from None
    pages = [page for page in pages if page.page_content.strip()]
    if not pages:
        raise IngestError("PDF vazio ou sem texto extraivel. Use um PDF textual; OCR nao esta incluido.")
    # Guarda somente metadata util; nao persiste caminhos absolutos da maquina.
    for page in pages:
        page.metadata = {
            "source": path.name,
            "page": page.metadata["page"],
            "pdf_sha256": file_hash,
        }
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP, add_start_index=True,
    )
    chunks = splitter.split_documents(pages)
    for index, chunk in enumerate(chunks):
        chunk.metadata["chunk_index"] = index
    return chunks, file_hash


def collection_metadata(
    settings: Settings, chunks: list[Document], file_hash: str,
) -> dict:
    """Identifica documento, modelo e particionamento para evitar misturar versoes."""
    # Detecta tambem mudancas no loader/splitter, mesmo com PDF identico.
    payload = [[chunk.page_content, chunk.metadata] for chunk in chunks]
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return {
        "ingestion_version": 1,
        "pdf_sha256": file_hash,
        "chunks_sha256": digest,
        "embedding_model": settings.model,
        "embedding_dimensions": settings.dimensions,
        "chunk_size": CHUNK_SIZE,
        "chunk_overlap": CHUNK_OVERLAP,
        "chunk_count": len(chunks),
    }


def chunk_ids(collection: str, metadata: dict, chunks: list[Document]) -> list[str]:
    """Os mesmos dados produzem os mesmos IDs, inclusive depois de reiniciar."""
    prefix = collection + json.dumps(metadata, sort_keys=True)
    return [
        hashlib.sha256(f"{prefix}:{index}".encode()).hexdigest()
        for index in range(len(chunks))
    ]


def estimate_cost(chunks: list[Document], model: str) -> float:
    """Estima o custo conservador em dolares, sem acessar a API."""
    if model not in PRICE_PER_MILLION:
        raise IngestError(
            "Preco deste modelo nao revisado. Use text-embedding-3-small "
            "ou revise a estimativa antes de gerar embeddings."
        )
    # Um token BPE ocupa ao menos um byte. Contar bytes UTF-8 superestima o custo
    # e dispensa download de tokenizer; inclui texto repetido pelo overlap.
    byte_count = sum(len(chunk.page_content.encode("utf-8")) for chunk in chunks)
    return byte_count * PRICE_PER_MILLION[model] / 1_000_000


def check_database(connection: Connection, settings: Settings) -> None:
    """Recusa extensao ausente ou coluna incompatível antes de gerar embeddings."""
    extension_query = text(
        "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname='vector')"
    )
    extension_installed = connection.execute(extension_query).scalar_one()
    if not extension_installed:
        raise IngestError("Extensao vector ausente. Execute docker compose up -d e confira o bootstrap.")
    vector_type = connection.execute(text("""
        SELECT format_type(atttypid, atttypmod)
        FROM pg_attribute
        WHERE attrelid=to_regclass('public.langchain_pg_embedding')
          AND attname='embedding' AND NOT attisdropped
    """)).scalar_one_or_none()
    if vector_type not in (None, "vector", f"vector({settings.dimensions})"):
        raise IngestError(
            "Dimensao da coluna vector incompativel. Use um banco/schema compativel; "
            "mudar so a collection nao altera a coluna. Nenhum dado foi apagado."
        )


def stored_collection(connection: Connection, name: str) -> RowMapping | None:
    """Consulta a collection; None significa que ela ainda nao foi criada."""
    # Consultar o catalogo evita reutilizar a ausencia de uma relacao resolvida
    # antes de outra conexao (PGVector) criar e confirmar as tabelas.
    exists = connection.execute(text("""
        SELECT EXISTS (
            SELECT 1 FROM pg_catalog.pg_tables
            WHERE schemaname='public' AND tablename='langchain_pg_collection'
        )
    """)).scalar_one()
    if not exists:
        return None
    return connection.execute(text("""
        SELECT uuid, cmetadata FROM public.langchain_pg_collection WHERE name=:name
    """), {"name": name}).mappings().one_or_none()


def verify_rows(
    connection: Connection, collection_id, ids: list[str],
    chunks: list[Document], dimensions: int,
) -> bool:
    """Retorna False se vazia; dados parciais ou diferentes geram erro."""
    rows = connection.execute(text("""
        SELECT id, document, cmetadata, vector_dims(embedding) AS dimensions
        FROM public.langchain_pg_embedding WHERE collection_id=:collection_id
    """), {"collection_id": collection_id}).mappings().all()
    if not rows:
        return False
    expected_chunks = dict(zip(ids, chunks))
    inconsistent_message = (
        "Collection parcial ou inconsistente. Revise os registros no pgAdmin; "
        "nenhuma reingestao ou exclusao automatica foi feita."
    )
    if len(rows) != len(expected_chunks):
        raise IngestError(inconsistent_message)

    for row in rows:
        chunk = expected_chunks.get(row["id"])
        if chunk is None:
            raise IngestError(inconsistent_message)
        if (
            row["document"] != chunk.page_content
            or row["cmetadata"] != chunk.metadata
            or row["dimensions"] != dimensions
        ):
            raise IngestError(inconsistent_message)
    return True


def validate_vectors(vectors: list[list[float]], count: int, dimensions: int) -> None:
    """Confere quantidade, tamanho e numeros antes de enviar o lote ao banco."""
    invalid_message = (
        "Provedor retornou vetores invalidos ou dimensao inesperada. Confira o modelo "
        "e OPENAI_EMBEDDING_DIMENSIONS; nada foi persistido."
    )
    if len(vectors) != count:
        raise IngestError(invalid_message)
    for vector in vectors:
        if len(vector) != dimensions:
            raise IngestError(invalid_message)
        if not all(math.isfinite(value) for value in vector):
            raise IngestError(invalid_message)


def acquire_ingestion_lock(connection: Connection) -> None:
    """Impede ingestao concorrente; o PostgreSQL libera o lock ao fim da transacao."""
    lock_query = text("SELECT pg_try_advisory_xact_lock(:lock_id)")
    acquired = connection.execute(lock_query, {"lock_id": INGESTION_LOCK_ID}).scalar_one()
    if not acquired:
        raise IngestError("Outra ingestao esta em andamento neste banco. Aguarde antes de tentar novamente.")


def is_already_ingested(
    connection: Connection, settings: Settings, metadata: dict,
    ids: list[str], chunks: list[Document],
) -> bool:
    """Reaproveita somente uma collection compativel com todos os chunks intactos."""
    existing = stored_collection(connection, settings.collection)
    if existing is None:
        return False
    if existing["cmetadata"] != metadata:
        raise IngestError(
            "Collection incompativel com PDF, modelo, dimensao ou chunks atuais. "
            "Use outra PGVECTOR_COLLECTION; dimensao diferente exige banco/schema "
            "compativel. Nenhum dado foi apagado."
        )
    return verify_rows(connection, existing["uuid"], ids, chunks, settings.dimensions)


def validate_api_budget(settings: Settings, chunks: list[Document], max_cost: float) -> None:
    """Valida chave e estimativa apenas quando novos embeddings sao necessarios."""
    if not settings.api_key:
        raise IngestError("Defina OPENAI_API_KEY no .env local antes da primeira ingestao.")
    estimate = estimate_cost(chunks, settings.model)
    if not math.isfinite(max_cost) or max_cost <= 0 or estimate > max_cost:
        raise IngestError(
            "Estimativa excede --max-cost-usd ou limite invalido. Use --dry-run "
            "e revise o volume antes de autorizar um limite maior."
        )


def generate_and_store_embeddings(
    engine: Engine, settings: Settings, metadata: dict,
    ids: list[str], chunks: list[Document],
) -> None:
    """Unica etapa com chamada paga; grava o lote depois de validar os vetores."""
    embeddings = OpenAIEmbeddings(
        api_key=settings.api_key,
        model=settings.model,
        dimensions=settings.dimensions,
        max_retries=0,
        request_timeout=60,
    )
    store = PGVector(
        embeddings=embeddings,
        connection=engine,
        embedding_length=settings.dimensions,
        collection_name=settings.collection,
        collection_metadata=metadata,
        use_jsonb=True,
        create_extension=False,
        pre_delete_collection=False,
    )
    texts = [chunk.page_content for chunk in chunks]
    vectors = embeddings.embed_documents(texts)
    validate_vectors(vectors, len(chunks), settings.dimensions)
    # PGVector confirma o lote em uma transacao; nao um commit por chunk.
    store.add_embeddings(
        texts=texts,
        embeddings=vectors,
        ids=ids,
        metadatas=[chunk.metadata for chunk in chunks],
    )


def ingest(
    settings: Settings, chunks: list[Document], file_hash: str, max_cost: float = 0.01,
) -> bool:
    """Retorna True se gravou dados novos; False se os dados ja estavam completos."""
    if not settings.database_url.password:
        raise IngestError("Defina POSTGRES_PASSWORD no .env local.")
    metadata = collection_metadata(settings, chunks, file_hash)
    ids = chunk_ids(settings.collection, metadata, chunks)
    engine = create_engine(
        settings.database_url,
        hide_parameters=True,
        connect_args={
            "connect_timeout": 5,
            "options": "-c search_path=public -c statement_timeout=30000",
        },
    )
    try:
        with engine.begin() as connection:
            acquire_ingestion_lock(connection)
            check_database(connection, settings)
            if is_already_ingested(connection, settings, metadata, ids, chunks):
                return False

            validate_api_budget(settings, chunks, max_cost)
            generate_and_store_embeddings(engine, settings, metadata, ids, chunks)

            # A escrita usa outra conexao. Reler aqui comprova o commit do lote.
            saved = stored_collection(connection, settings.collection)
            if saved is None or not verify_rows(
                connection, saved["uuid"], ids, chunks, settings.dimensions,
            ):
                raise IngestError("Persistencia nao confirmada. Confira o banco antes de repetir a ingestao.")
            return True
    finally:
        engine.dispose()


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", type=Path, default=ROOT / "document.pdf")
    parser.add_argument("--dry-run", action="store_true", help="Valida PDF e estima custo sem banco ou API.")
    parser.add_argument(
        "--max-cost-usd", type=float, default=0.01,
        help="Limite da estimativa por execucao (padrao: 0.01 USD).",
    )
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> None:
    """Executa o fluxo da CLI; main() transforma erros em mensagens seguras."""
    settings = Settings.from_env()
    chunks, file_hash = prepare_pdf(args.pdf)
    print(f"PDF preparado: {len(chunks)} chunks; tamanho/overlap {CHUNK_SIZE}/{CHUNK_OVERLAP}.")
    if args.dry_run:
        estimated_cost = estimate_cost(chunks, settings.model)
        print(f"Estimativa conservadora de embeddings: US$ {estimated_cost:.6f}.")
        print("Nenhuma chamada de API ou conexao ao banco foi realizada.")
        return

    created = ingest(settings, chunks, file_hash, args.max_cost_usd)
    if created:
        print("Ingestao concluida: textos, metadata e vetores confirmados no PostgreSQL.")
    else:
        print("PDF ja ingerido: dados conferidos; nenhuma chamada de API realizada.")


def main(argv: list[str] | None = None) -> int:
    args = parse_arguments(argv)
    load_dotenv(ROOT / ".env", override=False)
    try:
        run(args)
        return 0
    except IngestError as exc:
        message = str(exc)
    except AuthenticationError:
        message = "Autenticacao OpenAI falhou. Confira OPENAI_API_KEY no .env."
    except RateLimitError:
        message = (
            "OpenAI recusou por cota ou limite. Confira Usage/Billing e aguarde "
            "se necessario; nao houve repeticao automatica."
        )
    except APIConnectionError:
        message = "Falha de conexao com OpenAI. Confira a rede; a API pode ter processado a solicitacao."
    except APIError:
        message = "OpenAI recusou a solicitacao. Confira modelo, dimensao e acesso da conta."
    except SQLAlchemyError:
        message = (
            "Falha no PostgreSQL. Confira Docker, variaveis POSTGRES_*, permissoes "
            "e schema no pgAdmin. Persistencia nao confirmada; nao apague volumes."
        )
    except Exception:
        # Nunca imprimir excecoes de SDK/banco que possam conter SQL, texto ou chaves.
        message = (
            "Falha inesperada na ingestao. Confira arquivo, dependencias "
            "e configuracao; persistencia nao confirmada."
        )
    print(f"Erro: {message}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
