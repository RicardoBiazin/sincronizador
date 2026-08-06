"""Carregamento e gravacao da configuracao (jobs, e-mail, log).

A configuracao fica num arquivo JSON. Por padrao, ao lado do executavel
(sincronizador.config.json). Cada "job" descreve uma tarefa de sincronizacao.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field, asdict, fields as dcfields
from typing import Dict, List, Optional

from . import segredos
from .endpoints import campos_secretos, endpoint_kinds


# ---------------------------------------------------------------------------
# Localizacao dos arquivos (config, logs) ao lado do .exe ou do script
# ---------------------------------------------------------------------------
def app_dir() -> str:
    """Pasta base da aplicacao (onde fica o .exe ou o app.py)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


DEFAULT_CONFIG_PATH = os.path.join(app_dir(), "sincronizador.config.json")
DEFAULT_LOG_DIR = os.path.join(app_dir(), "logs")


# ---------------------------------------------------------------------------
# Estruturas de dados
# ---------------------------------------------------------------------------
MODES = ("espelho", "incremental", "bidirecional")

# Como decidir se dois arquivos sao iguais:
#   auto     - usa a data quando os dois lados preservam a data original,
#              senao cai para tamanho (object storage nao aceita definir data)
#   data     - sempre tamanho + data (comportamento historico)
#   tamanho  - so o tamanho (rapido, menos seguro)
#   conteudo - tamanho e, se bater, o hash do conteudo (lento, mais seguro)
COMPARE_MODES = ("auto", "data", "tamanho", "conteudo")


def endpoint_types() -> tuple:
    """Tipos de endpoint disponiveis (vem do registro em endpoints.py)."""
    return tuple(endpoint_kinds())


#: mantido por compatibilidade; prefira endpoint_types()
ENDPOINT_TYPES = endpoint_types()


@dataclass
class Remote:
    """Dados de conexao de um endpoint remoto. Ignorado no tipo 'local'.

    Os campos fixos atendem FTP/SFTP. Tipos novos (S3, Azure, WebDAV, OAuth...)
    guardam o que precisarem em 'options', declarando os campos no EndpointSpec
    correspondente - assim nem esta classe nem a GUI mudam a cada backend novo.
    """
    host: str = ""
    port: int = 0            # 0 = padrao (21 FTP, 22 SFTP)
    user: str = ""
    password: str = ""
    key_file: str = ""       # caminho de chave privada (SFTP, opcional)
    passive: bool = True     # FTP modo passivo
    tls: bool = False        # FTP com TLS (FTPS)
    options: Dict[str, object] = field(default_factory=dict)

    def opt(self, key: str, default=None):
        """Valor de um campo livre (bucket, regiao, token, ...)."""
        v = self.options.get(key, default)
        return default if v in (None, "") else v

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Remote":
        r = cls()
        for f in dcfields(cls):
            if f.name == "options":
                continue
            if f.name in d:
                setattr(r, f.name, d[f.name])
        opts = d.get("options") or {}
        r.options = dict(opts) if isinstance(opts, dict) else {}
        return r


@dataclass
class Job:
    name: str = "Nova tarefa"
    mode: str = "espelho"                # espelho | incremental | bidirecional
    enabled: bool = True

    source: str = ""
    source_type: str = "local"          # local | ftp | sftp
    source_remote: Remote = field(default_factory=Remote)

    dest: str = ""
    dest_type: str = "local"
    dest_remote: Remote = field(default_factory=Remote)

    include: List[str] = field(default_factory=list)   # glob; vazio = tudo
    exclude: List[str] = field(default_factory=lambda: [
        "~$*", "*.tmp", "Thumbs.db", "desktop.ini", ".~lock.*", "*.crdownload",
        "__pycache__", ".git",
    ])

    versioning: bool = False
    backup_dir: str = ""                 # se vazio e versioning=on: usa <dest>/_backup_sincronizador
    keep_versions_days: int = 30         # limpa backups mais antigos que isso (0 = nunca)

    validate: bool = True                # confere os arquivos ao final da sincronizacao
    validate_hash: bool = False          # validacao forte por conteudo (hash) - mais lento

    compare: str = "auto"                # ver COMPARE_MODES

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Job":
        j = cls()
        for k in ("name", "mode", "enabled", "source", "source_type",
                  "dest", "dest_type", "include", "exclude",
                  "versioning", "backup_dir", "keep_versions_days",
                  "validate", "validate_hash", "compare"):
            if k in d:
                setattr(j, k, d[k])
        j.source_remote = Remote.from_dict(d.get("source_remote", {}))
        j.dest_remote = Remote.from_dict(d.get("dest_remote", {}))
        return j


@dataclass
class Email:
    enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    use_tls: bool = True
    from_addr: str = ""
    to_addrs: List[str] = field(default_factory=list)
    notify_on: str = "erros"             # sempre | erros

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Email":
        e = cls()
        for k in e.__dict__:
            if k in d:
                setattr(e, k, d[k])
        return e


@dataclass
class AppConfig:
    jobs: List[Job] = field(default_factory=list)
    email: Email = field(default_factory=Email)
    log_dir: str = DEFAULT_LOG_DIR
    log_keep_days: int = 60
    theme: str = "Claro"                  # nome de um tema em gui.THEMES
    accent: str = "#0a6cff"               # cor de destaque (hex)
    parallel: int = 4                     # quantos arquivos copiar ao mesmo tempo (local)

    def to_dict(self) -> dict:
        return {
            "jobs": [j.to_dict() for j in self.jobs],
            "email": self.email.to_dict(),
            "log_dir": self.log_dir,
            "log_keep_days": self.log_keep_days,
            "theme": self.theme,
            "accent": self.accent,
            "parallel": self.parallel,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AppConfig":
        c = cls()
        c.jobs = [Job.from_dict(j) for j in d.get("jobs", [])]
        c.email = Email.from_dict(d.get("email", {}))
        c.log_dir = d.get("log_dir", DEFAULT_LOG_DIR)
        c.log_keep_days = d.get("log_keep_days", 60)
        c.theme = d.get("theme", "Claro")
        c.accent = d.get("accent", "#0a6cff")
        c.parallel = int(d.get("parallel", 4)) or 1
        return c

    def get_job(self, name: str) -> Optional[Job]:
        for j in self.jobs:
            if j.name == name:
                return j
        return None


# ---------------------------------------------------------------------------
# Cifragem das credenciais no arquivo (ver segredos.py)
# ---------------------------------------------------------------------------
def _percorrer_segredos(data: dict, funcao) -> dict:
    """Aplica 'funcao' a cada credencial do dicionario serializado.

    Quais campos sao credencial vem da declaracao de cada tipo de endpoint
    (Field.kind = password/oauth), entao um backend novo protege as suas
    credenciais sem precisar mexer aqui.
    """
    for job in data.get("jobs", []):
        for lado in ("source", "dest"):
            remote = job.get(lado + "_remote")
            if not isinstance(remote, dict):
                continue
            chaves = campos_secretos(job.get(lado + "_type", ""))
            opts = remote.get("options")
            for chave in chaves:
                if chave in remote and isinstance(remote[chave], str):
                    remote[chave] = funcao(remote[chave])
                if isinstance(opts, dict) and isinstance(opts.get(chave), str):
                    opts[chave] = funcao(opts[chave])
    email = data.get("email")
    if isinstance(email, dict) and isinstance(email.get("smtp_password"), str):
        email["smtp_password"] = funcao(email["smtp_password"])
    return data


# ---------------------------------------------------------------------------
# Persistencia
# ---------------------------------------------------------------------------
def load_config(path: str = DEFAULT_CONFIG_PATH) -> AppConfig:
    if not os.path.exists(path):
        return AppConfig()
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    # configuracoes antigas tem as senhas em texto puro: revelar() as devolve
    # como estao, e elas passam a ser cifradas na proxima gravacao
    return AppConfig.from_dict(_percorrer_segredos(data, segredos.revelar))


def save_config(cfg: AppConfig, path: str = DEFAULT_CONFIG_PATH) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    data = _percorrer_segredos(cfg.to_dict(), segredos.proteger)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
