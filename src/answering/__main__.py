"""Responde uma pergunta por execução; o loop de chat pertence à próxima história."""
import argparse
import sys

from dotenv import load_dotenv
from openai import APIError
from sqlalchemy.exc import SQLAlchemyError

from ingestion.config import ROOT, Settings
from ingestion.errors import IngestError
from retrieval.errors import SearchError
from retrieval.service import SemanticSearchService

from .model import AnswerError, ChatModel
from .service import AnswerService


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("question", nargs="+", help="Pergunta sobre o PDF.")
    arguments = parser.parse_args(argv)
    load_dotenv(ROOT / ".env", override=False)
    try:
        settings = Settings.from_env()
        model = ChatModel.from_env(settings.api_key)
        service = AnswerService(SemanticSearchService(settings), model)
        print(service.answer(" ".join(arguments.question)))
        return 0
    except (AnswerError, SearchError, IngestError) as error:
        message = str(error)
    except APIError:
        message = "Falha na OpenAI. Confira chave, saldo, modelo e conexão antes de repetir."
    except SQLAlchemyError:
        message = "Falha no PostgreSQL. Confira Docker e as variáveis POSTGRES_* do .env."
    except Exception:
        message = "Falha inesperada ao responder. Detalhes externos foram ocultados."
    print(f"Erro: {message}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
