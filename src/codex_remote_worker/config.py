from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal
import re
import tomllib

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from .common import JobError


def read_codex_config(home: Path) -> dict:
    path = home / 'config.toml'
    try:
        return tomllib.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}
    except (OSError, ValueError):
        raise JobError('CODEX_CONFIG_INVALID', '无法读取 Codex config.toml，请在服务运行账户下检查配置。', 503) from None


def provider_env_names(config: dict) -> set[str]:
    names = {'OPENAI_API_KEY', 'OPENROUTER_API_KEY'}
    for provider in config.get('model_providers', {}).values():
        if isinstance(provider, dict):
            names.add(provider.get('env_key', ''))
            names.update(provider.get('env_http_headers', {}).values())
    reserved = {'CODEX_WORKER_SERVICE_TOKEN', 'CODEX_HOME', 'PATH', 'HOME', 'USERPROFILE', 'TEMP', 'TMP',
                'TMPDIR', 'SYSTEMROOT', 'WINDIR', 'APPDATA', 'LOCALAPPDATA', 'COMSPEC', 'PYTHONPATH', 'PYTHONHOME'}
    return {name for name in names if isinstance(name, str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name)
            and name.upper() not in reserved}


def credential_values(value, key='') -> list[str]:
    if isinstance(value, dict):
        return [secret for name, item in value.items() for secret in credential_values(item, name)]
    if isinstance(value, str) and value and key not in {'env_key', 'bearer_token_env_var'}:
        if re.search(r'(?i)(token|api_key|secret|password|authorization)', key):
            return [value]
    return []


class Options(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    timeout_seconds: int = Field(default=1800, ge=60, le=7200)


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    data_root: Path
    codex_executable: Path
    codex_home: Path = Field(default_factory=lambda: Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex').resolve())
    expected_codex_version: str = "0.166.0-alpha.5.1"
    tool_python: Path
    doc_converter: Path
    tool_read_roots: list[Path] = Field(default_factory=list)
    service_token: SecretStr
    provider_env: dict[str, SecretStr] = Field(default_factory=dict, exclude=True)
    authentication_mode: Literal["server_codex", "chatgpt"] = "server_codex"
    protected_read_roots: list[Path] = Field(default_factory=list)
    windows_sandbox: Literal["elevated", "mxc"] = "elevated"
    bind_host: str = "127.0.0.1"
    port: int = Field(default=8787, ge=1, le=65535)
    queue_capacity: int = Field(default=10, ge=1, le=100)
    max_concurrent_jobs: int = Field(default=1, ge=1, le=16, strict=True)
    task_batch_concurrency: int = Field(default=1, ge=1, le=16, strict=True)
    max_file_bytes: int = Field(default=32 * 1024**2, gt=0)
    max_prompt_bytes: int = Field(default=1024**2, gt=0)
    max_upload_bytes: int = Field(default=64 * 1024**2, gt=0)
    max_output_bytes: int = Field(default=32 * 1024**2, gt=0)
    max_log_bytes: int = Field(default=64 * 1024**2, ge=1024)
    min_free_bytes: int = Field(default=1024**3, ge=0)

    @model_validator(mode='after')
    def bounded_task_concurrency(self):
        if self.task_batch_concurrency > self.max_concurrent_jobs:
            raise ValueError('task_batch_concurrency must not exceed max_concurrent_jobs')
        return self

    @field_validator("data_root", "codex_executable", "codex_home", "tool_python", "doc_converter")
    @classmethod
    def absolute(cls, value: Path):
        if not value.is_absolute():
            raise ValueError("deployment paths must be absolute")
        return value.resolve()

    @field_validator("service_token")
    @classmethod
    def strong_token(cls, value: SecretStr):
        if len(value.get_secret_value()) < 32:
            raise ValueError("SERVICE_TOKEN must contain at least 32 characters")
        return value

    @field_validator('tool_read_roots', 'protected_read_roots')
    @classmethod
    def narrow_roots(cls, values):
        if any(not p.is_absolute() or p == Path(p.anchor) for p in values):
            raise ValueError('tool read roots must be absolute, non-root directories')
        return [p.resolve() for p in values]

    def secret_values(self):
        secrets = [self.service_token.get_secret_value(), *(v.get_secret_value() for v in self.provider_env.values())]
        secrets.extend(credential_values(read_codex_config(self.codex_home)))
        try:
            secrets.extend(credential_values(json.loads((self.codex_home / 'auth.json').read_text(encoding='utf-8-sig'))))
        except (OSError, ValueError):
            pass  # Codex also supports an OS credential store; an auth.json file is not required.
        return [secret for secret in secrets if secret]


def load_settings(path: Path, credentials_file: Path | None = None) -> Settings:
    values = json.loads(path.read_text(encoding="utf-8-sig"))
    if {"service_token", "openrouter_api_key", "provider_env"} & values.keys():
        raise ValueError("Secrets must be provided through environment variables, not config JSON")
    values.pop('models', None)  # A 0.1 deployment may retain this obsolete field; Codex now owns model selection.
    home = Path(values.get('codex_home') or os.environ.get('CODEX_HOME') or Path.home() / '.codex')
    values['codex_home'] = home
    env_names = provider_env_names(read_codex_config(home))
    credentials = os.environ
    if credentials_file is not None:
        from .common import contained_file
        private_root = (Path(values['data_root']) / 'private').resolve(strict=True)
        credentials_file = contained_file(private_root, credentials_file.absolute())
        credentials = json.loads(credentials_file.read_text(encoding='utf-8-sig'))
        if set(credentials) - {'CODEX_WORKER_SERVICE_TOKEN', *env_names}:
            raise ValueError('credentials file contains names not used by the configured Codex provider')
    values["service_token"] = credentials.get("CODEX_WORKER_SERVICE_TOKEN", "")
    values['provider_env'] = {name: os.environ.get(name, credentials.get(name)) for name in env_names
                              if os.environ.get(name, credentials.get(name))}
    return Settings.model_validate(values)
