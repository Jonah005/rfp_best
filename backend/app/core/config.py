"""Application configuration.

Every value comes from the environment. Secrets are never defaulted to a usable
value and never read from anywhere but the environment.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The pgvector column dimension is fixed at DDL time and is therefore a schema
# constant, not configuration. Changing the embedding model to one with a
# different width is a migration (see docs/05 section 14.4), not an env edit.
EMBEDDING_DIM: int = 768


# `.env` lives beside the backend package, and it has to be found from any
# working directory. A bare ".env" is resolved against the process cwd, so
# launching from the repo root — which `run_local.py` does — silently loaded no
# file at all, and every value in it was ignored without a word of warning.
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BIDREADY_",
        env_file=str(ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: Literal["development", "test", "staging", "production"] = "development"
    secret_key: str = "insecure-development-key-change-me"  # noqa: S105 - dev default,
    # rejected outside development by the validator below
    access_token_ttl_minutes: int = 30
    refresh_token_ttl_days: int = 14
    jwt_algorithm: str = "HS256"

    database_url: str = "postgresql+psycopg://bidready_app:bidready@localhost:5432/bidready"
    # Migrations create and alter tables, which the application role deliberately
    # cannot do: it is NOSUPERUSER, holds no CREATE on schema public, and has no
    # BYPASSRLS, which is exactly what makes FORCE ROW LEVEL SECURITY bind it.
    # Alembic therefore connects as the schema owner instead. Leaving this unset
    # runs migrations as `database_url`, which fails with "permission denied for
    # schema public" -- loudly, and before any data exists.
    migration_database_url: str | None = None
    db_echo: bool = False
    db_pool_size: int = 10
    db_max_overflow: int = 20

    redis_url: str = "redis://localhost:6379/0"
    # Where streamed-answer job state lives. "redis" is required for more than
    # one API replica: the POST that starts a job and the GET that streams it
    # are separate requests and will land on different processes. "memory" is
    # for single-process runs; "auto" uses Redis when it is reachable.
    job_store: Literal["auto", "redis", "memory"] = "auto"
    # Run queued work inline instead of dispatching to a broker. Only for the
    # local single-process runner: it makes an upload block until indexing
    # finishes, which is wrong for production and convenient for a walkthrough.
    celery_eager: bool = False
    cors_origins: str = "http://localhost:3000"

    # "s3" for MinIO or any S3-compatible target; "local" writes to a directory
    # on disk, which is what the single-process local runner uses so a
    # walkthrough needs no object-store container.
    storage_backend: Literal["s3", "local"] = "s3"
    storage_local_path: str = ".local/objects"
    storage_endpoint_url: str | None = "http://localhost:9000"
    storage_access_key: str = "minioadmin"
    storage_secret_key: str = "minioadmin"  # noqa: S105 - local MinIO default
    storage_bucket: str = "bidready"
    storage_region: str = "us-east-1"

    llm_provider: Literal["deepseek", "null"] = "deepseek"
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-chat"
    llm_temperature: float = 0.2
    llm_timeout_seconds: float = 60.0
    llm_max_retries: int = 3
    llm_max_output_tokens: int = 4096

    # How embeddings are stored and searched.
    #
    #   pgvector  - a vector(768) column with an HNSW index. Indexed, fast at
    #               scale, and requires the pgvector extension.
    #   portable  - a real[] column ranked by cosine similarity in Python. Same
    #               embeddings, same ranking, no extension; unindexed, so it is
    #               a scan and only suits modest corpora.
    #
    # Both keep vectors inside PostgreSQL, so row level security, the
    # permission predicate and atomic writes apply identically.
    vector_backend: Literal["pgvector", "portable"] = "pgvector"

    embedding_provider: Literal["local", "null"] = "local"
    embedding_model: str = "intfloat/multilingual-e5-base"
    # "cpu", "cuda", "cuda:0", "mps". Named rather than auto-detected: an
    # unusable GPU kills the process in native code with no traceback, and this
    # workload does not need one.
    embedding_device: str = "cpu"

    # Local OCR for pages with no text layer. "auto" uses whichever engine is
    # installed and does nothing when neither is; "none" disables it outright.
    # Both engines run in-process on CPU: a scanned tender page is confidential
    # bid material, and sending it to a hosted OCR service would defeat the
    # point of keeping inference inside the boundary.
    ocr_provider: Literal["auto", "rapidocr", "tesseract", "none"] = "auto"

    # Read from the bare environment variable, not the BIDREADY_ prefix, because
    # the vendor's own name is the contract the deployment team expects.
    deepseek_api_key: str | None = Field(default=None, alias="DEEPSEEK_API_KEY")

    # Reading a picture needs a model that can see; the text model cannot. It is
    # off unless someone configures it, it is used only when a person ticks the
    # box for one specific image, and its key comes from VISION_API_KEY in the
    # environment -- never from source or the database.
    vision_provider: Literal["none", "openai_compatible"] = "none"
    vision_base_url: str = ""
    vision_model: str = ""
    vision_timeout_seconds: float = 90.0
    vision_max_image_bytes: int = 5 * 1024 * 1024
    vision_api_key: str | None = Field(default=None, alias="VISION_API_KEY")

    # Have the model check what retrieval returned before anything is attached.
    #
    # On, retrieval widens — the similarity floor drops to the recall value in
    # app.services.assessment — and the model prunes the shortlist. Off, the
    # strict similarity floor is the only filter. The cost is one model call per
    # assessed requirement, so a bid of sixty requirements is sixty calls: real
    # latency and real spend, which is why this is a setting and not a constant.
    # It degrades to the strict-threshold behaviour whenever the call fails or
    # no provider is configured — verification can never be the reason a bid
    # cannot be assessed.
    assessment_llm_verification: bool = True

    @field_validator("secret_key")
    @classmethod
    def _reject_default_secret_outside_dev(cls, v: str, info) -> str:
        env = (info.data or {}).get("env", "development")
        if env in ("staging", "production") and "insecure" in v:
            raise ValueError("BIDREADY_SECRET_KEY must be set outside development")
        return v

    @property
    def is_production(self) -> bool:
        return self.env == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
