"""Endpoints de nuvem: WebDAV, Amazon S3, Backblaze B2, Azure Blob,
Azure Files e Google Cloud Storage.

Todos os imports das bibliotecas sao feitos dentro do __init__ de cada classe:
quem nao usa um servico nao precisa ter o pacote instalado, e o executavel so
carrega o que a tarefa realmente usa.

Sobre a data de modificacao
---------------------------
Object storage (S3, B2, Azure Blob, GCS) nao tem "data do arquivo": a listagem
devolve a data do upload. Para nao reenviar o acervo inteiro a cada execucao,
estes endpoints gravam a data original num metadado (MTIME_META) e a leem de
volta quando o servico permite.

  - Azure Blob e GCS entregam os metadados na propria listagem -> a data volta
    de graca e preserves_mtime = True.
  - S3/B2 nao entregam: seria um HEAD por objeto. Fica desligado por padrao
    (campo "Ler data (1 requisicao por arquivo)") e a comparacao cai para
    tamanho, conforme a politica em engine.Comparer.
  - Azure Files tem data de verdade (SMB) e a define no upload.
  - WebDAV nao tem forma padronizada de definir a data: preserves_mtime = False.

Em compensacao esses servicos informam o MD5 do conteudo na listagem, entao
content_hash() sai sem custo e a comparacao por conteudo fica barata desse
lado (so o lado local precisa ler os arquivos).
"""
from __future__ import annotations

import base64
import io
import os
import posixpath
import shutil
from typing import Dict, Optional

from .endpoints import (Endpoint, EndpointSpec, Field, FileInfo, register)


#: nome do metadado onde guardamos a data original.
#: sem hifens nem pontos: o Azure exige nome de identificador valido.
MTIME_META = "sincmtime"


def _fmt_mtime(mtime: float) -> str:
    return "%.6f" % float(mtime)


def _parse_mtime(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _dt_to_epoch(dt) -> float:
    """datetime (com ou sem fuso) -> segundos epoch UTC."""
    if dt is None:
        return 0.0
    try:
        import datetime as _dt
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0.0


def _md5_from_etag(etag) -> str:
    """ETag do S3 eh o MD5 - menos quando o upload foi multipart ('...-3')."""
    if not etag:
        return ""
    e = str(etag).strip().strip('"')
    if "-" in e or len(e) != 32:
        return ""
    try:
        int(e, 16)
    except ValueError:
        return ""
    return e.lower()


def _md5_from_b64(v) -> str:
    """Azure/GCS devolvem o MD5 em base64."""
    if not v:
        return ""
    try:
        raw = v if isinstance(v, (bytes, bytearray)) else base64.b64decode(v)
        if isinstance(v, str):
            raw = base64.b64decode(v)
        return raw.hex() if len(raw) == 16 else ""
    except Exception:
        return ""


class _ChunkReader:
    """Adapta um iterador de blocos a um objeto com .read(n).

    Alguns SDKs so entregam o download como iterador; shutil.copyfileobj
    precisa de read(). Evita carregar o arquivo inteiro na memoria.
    """

    def __init__(self, chunks):
        self._it = iter(chunks)
        self._buf = b""

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            out = self._buf + b"".join(self._it)
            self._buf = b""
            return out
        while len(self._buf) < n:
            try:
                self._buf += next(self._it)
            except StopIteration:
                break
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Base comum de object storage
# ---------------------------------------------------------------------------
class ObjectStoreEndpoint(Endpoint):
    """Parte comum de S3/B2/Azure Blob/GCS: chaves com prefixo, sem
    diretorios, backup baixado para pasta local."""

    parallel_safe = True
    has_dirs = False
    preserves_mtime = False

    def __init__(self, prefix: str):
        self.prefix = prefix.strip("/")

    def _key(self, rel: str) -> str:
        return posixpath.join(self.prefix, rel) if self.prefix else rel

    def _rel(self, key: str) -> Optional[str]:
        if not self.prefix:
            return key
        head = self.prefix + "/"
        return key[len(head):] if key.startswith(head) else None

    def content_hash(self, rel: str, info: Optional[FileInfo] = None) -> str:
        return info.etag if info is not None else ""

    def move_to_backup(self, rel: str, backup_base: str) -> None:
        """Versionamento de arquivo remoto: baixa para a pasta local."""
        try:
            data = self.open_read(rel)
        except Exception:
            return
        dst = os.path.join(backup_base, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        try:
            with open(dst, "wb") as f:
                shutil.copyfileobj(data, f, length=1024 * 1024)
        finally:
            try:
                data.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# WebDAV (Nextcloud, ownCloud, IIS, Apache mod_dav, ...)
# ---------------------------------------------------------------------------
_PROPFIND = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<d:propfind xmlns:d="DAV:"><d:prop>'
    '<d:resourcetype/><d:getcontentlength/><d:getlastmodified/>'
    '<d:getcontentmd5/><d:getetag/>'
    '</d:prop></d:propfind>'
)


class WebDavEndpoint(Endpoint):
    # nao ha jeito padronizado de definir a data de um recurso via WebDAV
    preserves_mtime = False
    parallel_safe = False   # uma sessao requests por endpoint
    has_dirs = True

    def __init__(self, root: str, remote):
        import requests
        from urllib.parse import urlparse
        base = str(remote.opt("base_url", "")).rstrip("/")
        if not base:
            raise ValueError("Informe a URL base do WebDAV.")
        self.base = base
        self.root = root.strip("/")
        self.sess = requests.Session()
        if remote.user:
            self.sess.auth = (remote.user, remote.password)
        self.sess.verify = bool(remote.opt("verify_tls", True))
        self.timeout = 60
        # caminho da URL base, usado para transformar href em caminho relativo
        self._basepath = urlparse(base).path.rstrip("/")
        self._colecoes_ok = set()   # pastas que ja sabemos existir

    # -- URLs ---------------------------------------------------------------
    def _url(self, rel: str = "") -> str:
        from urllib.parse import quote
        parts = [p for p in (self.root + "/" + rel).split("/") if p]
        return self.base + "".join("/" + quote(p) for p in parts)

    def _rootpath(self) -> str:
        return (self._basepath + "/" + self.root).rstrip("/")

    def _req(self, method: str, url: str, **kw):
        r = self.sess.request(method, url, timeout=self.timeout, **kw)
        if r.status_code >= 400:
            raise IOError("WebDAV %s %s -> HTTP %d" % (method, url, r.status_code))
        return r

    # -- leitura ------------------------------------------------------------
    def scan(self) -> Dict[str, FileInfo]:
        out: Dict[str, FileInfo] = {}
        self._scan_dir("", out)
        return out

    def _scan_dir(self, rel: str, out: Dict[str, FileInfo]) -> None:
        import xml.etree.ElementTree as ET
        from urllib.parse import unquote, urlparse
        try:
            r = self._req("PROPFIND", self._url(rel),
                          headers={"Depth": "1", "Content-Type": "application/xml"},
                          data=_PROPFIND.encode("utf-8"))
        except IOError:
            return
        try:
            tree = ET.fromstring(r.content)
        except ET.ParseError:
            return

        ns = {"d": "DAV:"}
        aqui = (self._rootpath() + ("/" + rel if rel else "")).rstrip("/")
        for resp in tree.findall("d:response", ns):
            href = resp.findtext("d:href", default="", namespaces=ns)
            path = unquote(urlparse(href).path).rstrip("/")
            if not path or path == aqui:
                continue          # a propria pasta
            child = path[len(self._rootpath()):].strip("/")
            if not child:
                continue
            prop = resp.find("d:propstat/d:prop", ns)
            if prop is None:
                continue
            is_dir = prop.find("d:resourcetype/d:collection", ns) is not None
            if is_dir:
                self._scan_dir(child, out)
                continue
            size = prop.findtext("d:getcontentlength", default="", namespaces=ns)
            out[child] = FileInfo(
                size=int(size) if size.isdigit() else 0,
                mtime=self._http_date(prop.findtext("d:getlastmodified",
                                                    default="", namespaces=ns)),
                etag=_md5_from_b64(prop.findtext("d:getcontentmd5",
                                                 default="", namespaces=ns)),
            )

    @staticmethod
    def _http_date(s: str) -> float:
        if not s:
            return 0.0
        try:
            from email.utils import parsedate_to_datetime
            return _dt_to_epoch(parsedate_to_datetime(s))
        except Exception:
            return 0.0

    def open_read(self, rel: str):
        r = self.sess.get(self._url(rel), stream=True, timeout=self.timeout)
        if r.status_code >= 400:
            raise IOError("WebDAV GET %s -> HTTP %d" % (rel, r.status_code))
        r.raw.decode_content = True
        return r.raw

    # -- escrita ------------------------------------------------------------
    def _mkcols(self, rel: str) -> None:
        """Cria as colecoes que faltarem ate a pasta do arquivo.

        Inclui os segmentos da propria pasta raiz: se ela ainda nao existe no
        servidor, o PUT responderia 409 (Conflict).
        """
        from urllib.parse import quote
        parts = [p for p in (self.root + "/" + rel).split("/") if p][:-1]
        acc = []
        for p in parts:
            acc.append(p)
            caminho = "/".join(acc)
            if caminho in self._colecoes_ok:
                continue
            try:
                self._req("MKCOL", self.base + "".join("/" + quote(x) for x in acc))
            except IOError:
                pass   # ja existe (405) ou sem permissao; o PUT dira
            self._colecoes_ok.add(caminho)

    def write(self, rel: str, fobj, size: int, mtime: float) -> None:
        self._mkcols(rel)
        self._req("PUT", self._url(rel), data=fobj)

    def delete(self, rel: str) -> None:
        try:
            self._req("DELETE", self._url(rel))
        except IOError:
            pass

    def move_to_backup(self, rel: str, backup_base: str) -> None:
        ObjectStoreEndpoint.move_to_backup(self, rel, backup_base)

    def probe(self) -> None:
        cab = {"Depth": "0", "Content-Type": "application/xml"}
        corpo = _PROPFIND.encode("utf-8")
        try:
            self._req("PROPFIND", self._url(), headers=cab, data=corpo)
        except IOError:
            # a pasta pode ainda nao existir (sera criada no envio); o que
            # importa testar aqui eh a URL base e as credenciais
            self._req("PROPFIND", self.base, headers=cab, data=corpo)

    def close(self) -> None:
        try:
            self.sess.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Amazon S3 (e qualquer servico com API compativel: Backblaze B2, MinIO, ...)
# ---------------------------------------------------------------------------
class S3Endpoint(ObjectStoreEndpoint):
    def __init__(self, prefix: str, remote):
        import boto3
        from botocore.config import Config
        super().__init__(prefix)
        self.bucket = str(remote.opt("bucket", "")).strip()
        if not self.bucket:
            raise ValueError("Informe o bucket.")
        # ler a data original custa um HEAD por objeto: opcional
        self.head_mtime = bool(remote.opt("head_mtime", False))
        self.preserves_mtime = self.head_mtime
        self.cli = boto3.client(
            "s3",
            aws_access_key_id=remote.user or None,
            aws_secret_access_key=remote.password or None,
            region_name=str(remote.opt("region", "")) or None,
            endpoint_url=str(remote.opt("endpoint_url", "")) or None,
            config=Config(retries={"max_attempts": 5, "mode": "standard"}),
        )

    def scan(self) -> Dict[str, FileInfo]:
        out: Dict[str, FileInfo] = {}
        paginator = self.cli.get_paginator("list_objects_v2")
        kw = {"Bucket": self.bucket}
        if self.prefix:
            kw["Prefix"] = self.prefix + "/"
        for page in paginator.paginate(**kw):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.endswith("/"):
                    continue            # marcador de "pasta"
                rel = self._rel(key)
                if rel is None or not rel:
                    continue
                mtime = _dt_to_epoch(obj.get("LastModified"))
                if self.head_mtime:
                    mtime = self._meta_mtime(key, mtime)
                out[rel] = FileInfo(size=int(obj.get("Size", 0)), mtime=mtime,
                                    etag=_md5_from_etag(obj.get("ETag")))
        return out

    def _meta_mtime(self, key: str, default: float) -> float:
        try:
            head = self.cli.head_object(Bucket=self.bucket, Key=key)
        except Exception:
            return default
        return _parse_mtime(head.get("Metadata", {}).get(MTIME_META), default)

    def open_read(self, rel: str):
        return self.cli.get_object(Bucket=self.bucket, Key=self._key(rel))["Body"]

    def write(self, rel: str, fobj, size: int, mtime: float) -> None:
        self.cli.upload_fileobj(
            fobj, self.bucket, self._key(rel),
            ExtraArgs={"Metadata": {MTIME_META: _fmt_mtime(mtime)}})

    def delete(self, rel: str) -> None:
        try:
            self.cli.delete_object(Bucket=self.bucket, Key=self._key(rel))
        except Exception:
            pass

    def probe(self) -> None:
        self.cli.head_bucket(Bucket=self.bucket)


# ---------------------------------------------------------------------------
# Azure Blob Storage
# ---------------------------------------------------------------------------
class AzureBlobEndpoint(ObjectStoreEndpoint):
    # a listagem do Azure ja traz os metadados: a data volta sem custo extra
    preserves_mtime = True

    def __init__(self, prefix: str, remote):
        from azure.storage.blob import ContainerClient
        super().__init__(prefix)
        container = str(remote.opt("container", "")).strip()
        if not container:
            raise ValueError("Informe o container.")
        conn = str(remote.opt("connection_string", "")).strip()
        if conn:
            self.cli = ContainerClient.from_connection_string(conn, container)
        else:
            account = str(remote.opt("account", "")).strip()
            if not account:
                raise ValueError("Informe a conta ou a connection string.")
            suffix = str(remote.opt("endpoint_suffix", "")) or "core.windows.net"
            self.cli = ContainerClient(
                account_url="https://%s.blob.%s" % (account, suffix),
                container_name=container,
                credential=remote.password or None)

    def scan(self) -> Dict[str, FileInfo]:
        out: Dict[str, FileInfo] = {}
        kw = {"include": ["metadata"]}
        if self.prefix:
            kw["name_starts_with"] = self.prefix + "/"
        for b in self.cli.list_blobs(**kw):
            rel = self._rel(b.name)
            if not rel or b.name.endswith("/"):
                continue
            meta = b.metadata or {}
            mtime = _parse_mtime(meta.get(MTIME_META), _dt_to_epoch(b.last_modified))
            md5 = ""
            cs = getattr(b, "content_settings", None)
            if cs is not None:
                md5 = _md5_from_b64(getattr(cs, "content_md5", None))
            out[rel] = FileInfo(size=int(b.size or 0), mtime=mtime, etag=md5)
        return out

    def open_read(self, rel: str):
        d = self.cli.download_blob(self._key(rel))
        return _ChunkReader(d.chunks())

    def write(self, rel: str, fobj, size: int, mtime: float) -> None:
        self.cli.upload_blob(name=self._key(rel), data=fobj, overwrite=True,
                             metadata={MTIME_META: _fmt_mtime(mtime)})

    def delete(self, rel: str) -> None:
        try:
            self.cli.delete_blob(self._key(rel))
        except Exception:
            pass

    def probe(self) -> None:
        self.cli.get_container_properties()

    def close(self) -> None:
        try:
            self.cli.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Azure Files (compartilhamento SMB) - tem data de verdade e diretorios
# ---------------------------------------------------------------------------
class AzureFileEndpoint(Endpoint):
    preserves_mtime = True
    parallel_safe = True
    has_dirs = True

    def __init__(self, root: str, remote):
        import inspect
        from azure.storage.fileshare import ShareClient, ShareFileClient
        # define a data no proprio upload; set_http_headers() nao serve porque
        # exige content_settings e apagaria o tipo de conteudo do arquivo
        self._data_no_upload = "file_last_write_time" in inspect.signature(
            ShareFileClient.upload_file).parameters
        self.preserves_mtime = self._data_no_upload
        share = str(remote.opt("share", "")).strip()
        if not share:
            raise ValueError("Informe o compartilhamento (share).")
        conn = str(remote.opt("connection_string", "")).strip()
        if conn:
            self.share = ShareClient.from_connection_string(conn, share)
        else:
            account = str(remote.opt("account", "")).strip()
            if not account:
                raise ValueError("Informe a conta ou a connection string.")
            suffix = str(remote.opt("endpoint_suffix", "")) or "core.windows.net"
            self.share = ShareClient(
                account_url="https://%s.file.%s" % (account, suffix),
                share_name=share, credential=remote.password or None)
        self.root = root.strip("/")
        self._pastas_ok = set()   # pastas que ja sabemos existir

    def _dir(self, rel: str):
        path = posixpath.join(self.root, rel) if rel else self.root
        return self.share.get_directory_client(path or "")

    def _file(self, rel: str):
        return self.share.get_file_client(posixpath.join(self.root, rel)
                                          if self.root else rel)

    def scan(self) -> Dict[str, FileInfo]:
        out: Dict[str, FileInfo] = {}
        self._scan_dir("", out)
        return out

    def _scan_dir(self, rel: str, out: Dict[str, FileInfo]) -> None:
        d = self._dir(rel)
        try:
            entradas = list(d.list_directories_and_files(include=["timestamps"]))
        except Exception:
            try:   # SDK antigo, sem o parametro include
                entradas = list(d.list_directories_and_files())
            except Exception:
                return
        for e in entradas:
            nome = e["name"] if isinstance(e, dict) else e.name
            filho = posixpath.join(rel, nome) if rel else nome
            is_dir = e["is_directory"] if isinstance(e, dict) else e.is_directory
            if is_dir:
                self._scan_dir(filho, out)
                continue
            size = (e.get("size") if isinstance(e, dict) else getattr(e, "size", 0)) or 0
            ts = None
            # FileProperties expoe 'last_write_time' (com include=timestamps);
            # 'last_modified' eh o retorno minimo da listagem
            for attr in ("last_write_time", "last_modified"):
                ts = e.get(attr) if isinstance(e, dict) else getattr(e, attr, None)
                if ts:
                    break
            if ts is None:
                # a listagem nao trouxe data: nao da para confiar na comparacao
                self.preserves_mtime = False
            out[filho] = FileInfo(size=int(size), mtime=_dt_to_epoch(ts))

    def open_read(self, rel: str):
        return _ChunkReader(self._file(rel).download_file().chunks())

    def _ensure_dirs(self, rel: str) -> None:
        """Cria as pastas que faltarem ate o arquivo, incluindo as da raiz.

        O Azure nao cria pastas intermediarias sozinho: cada nivel precisa
        existir antes do upload.
        """
        partes = [p for p in (self.root + "/" + rel).split("/") if p][:-1]
        acc = []
        for p in partes:
            acc.append(p)
            caminho = "/".join(acc)
            if caminho in self._pastas_ok:
                continue
            try:
                self.share.get_directory_client(caminho).create_directory()
            except Exception:
                pass   # ja existe
            self._pastas_ok.add(caminho)

    def write(self, rel: str, fobj, size: int, mtime: float) -> None:
        import datetime as _dt
        self._ensure_dirs(rel)
        f = self._file(rel)
        extra = {}
        if self._data_no_upload:
            extra["file_last_write_time"] = _dt.datetime.fromtimestamp(
                mtime, _dt.timezone.utc)
        f.upload_file(fobj, length=size if size >= 0 else None, **extra)

    def delete(self, rel: str) -> None:
        try:
            self._file(rel).delete_file()
        except Exception:
            pass

    def move_to_backup(self, rel: str, backup_base: str) -> None:
        ObjectStoreEndpoint.move_to_backup(self, rel, backup_base)

    def probe(self) -> None:
        self.share.get_share_properties()

    def close(self) -> None:
        try:
            self.share.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Google Cloud Storage
# ---------------------------------------------------------------------------
class GcsEndpoint(ObjectStoreEndpoint):
    # a listagem do GCS traz metadados e md5: data e hash saem de graca
    preserves_mtime = True

    def __init__(self, prefix: str, remote):
        from google.cloud import storage
        super().__init__(prefix)
        nome = str(remote.opt("bucket", "")).strip()
        if not nome:
            raise ValueError("Informe o bucket.")
        cred = str(remote.opt("credentials_file", "")).strip()
        projeto = str(remote.opt("project", "")).strip() or None
        if cred:
            self.cli = storage.Client.from_service_account_json(cred, project=projeto)
        else:   # credenciais padrao do ambiente (GOOGLE_APPLICATION_CREDENTIALS)
            self.cli = storage.Client(project=projeto)
        self.bucket = self.cli.bucket(nome)

    def scan(self) -> Dict[str, FileInfo]:
        out: Dict[str, FileInfo] = {}
        pref = (self.prefix + "/") if self.prefix else None
        for b in self.cli.list_blobs(self.bucket, prefix=pref):
            rel = self._rel(b.name)
            if not rel or b.name.endswith("/"):
                continue
            meta = b.metadata or {}
            mtime = _parse_mtime(meta.get(MTIME_META), _dt_to_epoch(b.updated))
            out[rel] = FileInfo(size=int(b.size or 0), mtime=mtime,
                                etag=_md5_from_b64(b.md5_hash))
        return out

    def open_read(self, rel: str):
        blob = self.bucket.blob(self._key(rel))
        try:
            return blob.open("rb")
        except Exception:   # versoes antigas da biblioteca
            return io.BytesIO(blob.download_as_bytes())

    def write(self, rel: str, fobj, size: int, mtime: float) -> None:
        blob = self.bucket.blob(self._key(rel))
        blob.metadata = {MTIME_META: _fmt_mtime(mtime)}
        blob.upload_from_file(fobj, size=size if size >= 0 else None)

    def delete(self, rel: str) -> None:
        try:
            self.bucket.blob(self._key(rel)).delete()
        except Exception:
            pass

    def probe(self) -> None:
        self.bucket.reload()


# ---------------------------------------------------------------------------
# Registro
# ---------------------------------------------------------------------------
register(EndpointSpec(
    kind="webdav",
    label="WebDAV (Nextcloud, ownCloud...)",
    factory=lambda path, remote: WebDavEndpoint(path, remote),
    path_label="Pasta remota:",
    requires=["requests"],
    note="O servidor define a data do arquivo no envio; use comparacao 'auto'.",
    fields=[
        Field("base_url", "URL base", width=38, required=True,
              help="ex: https://host/remote.php/dav/files/usuario"),
        Field("user", "Usuario"),
        Field("password", "Senha", kind="password", width=16),
        Field("verify_tls", "Validar certificado TLS", kind="bool", default=True),
    ],
))

register(EndpointSpec(
    kind="s3",
    label="Amazon S3",
    factory=lambda path, remote: S3Endpoint(path, remote),
    path_label="Prefixo (pasta):",
    requires=["boto3"],
    fields=[
        Field("bucket", "Bucket", required=True),
        Field("user", "Access key ID", width=26),
        Field("password", "Secret access key", kind="password", width=26),
        Field("region", "Regiao", help="ex: sa-east-1"),
        Field("endpoint_url", "Endpoint", width=34,
              help="vazio = AWS; preencha para MinIO e compativeis"),
        Field("head_mtime", "Ler data original (1 requisicao por arquivo)",
              kind="bool"),
    ],
))

register(EndpointSpec(
    kind="b2",
    label="Backblaze B2",
    factory=lambda path, remote: S3Endpoint(path, remote),
    path_label="Prefixo (pasta):",
    requires=["boto3"],
    note="Usa a API compativel com S3 do B2. O endpoint aparece no painel do "
         "bucket, em Endpoint.",
    fields=[
        Field("bucket", "Bucket", required=True),
        Field("user", "keyID", width=26),
        Field("password", "applicationKey", kind="password", width=30),
        Field("endpoint_url", "Endpoint", width=34, required=True,
              help="ex: https://s3.us-west-004.backblazeb2.com"),
        Field("region", "Regiao", help="ex: us-west-004"),
        Field("head_mtime", "Ler data original (1 requisicao por arquivo)",
              kind="bool"),
    ],
))

register(EndpointSpec(
    kind="azureblob",
    label="Microsoft Azure Blob Storage",
    factory=lambda path, remote: AzureBlobEndpoint(path, remote),
    path_label="Prefixo (pasta):",
    requires=["azure.storage.blob"],
    fields=[
        Field("container", "Container", required=True),
        Field("connection_string", "Connection string", kind="password", width=38,
              help="se preencher, ignora conta e chave"),
        Field("account", "Conta de armazenamento", width=26),
        Field("password", "Chave ou token SAS", kind="password", width=30),
        Field("endpoint_suffix", "Sufixo do endpoint",
              help="vazio = core.windows.net"),
    ],
))

register(EndpointSpec(
    kind="azurefiles",
    label="Microsoft Azure File Storage",
    factory=lambda path, remote: AzureFileEndpoint(path, remote),
    path_label="Pasta no compartilhamento:",
    requires=["azure.storage.fileshare:azure-storage-file-share"],
    fields=[
        Field("share", "Compartilhamento (share)", required=True, width=26),
        Field("connection_string", "Connection string", kind="password", width=38,
              help="se preencher, ignora conta e chave"),
        Field("account", "Conta de armazenamento", width=26),
        Field("password", "Chave ou token SAS", kind="password", width=30),
        Field("endpoint_suffix", "Sufixo do endpoint",
              help="vazio = core.windows.net"),
    ],
))

register(EndpointSpec(
    kind="gcs",
    label="Google Cloud Storage",
    factory=lambda path, remote: GcsEndpoint(path, remote),
    path_label="Prefixo (pasta):",
    requires=["google.cloud.storage"],
    fields=[
        Field("bucket", "Bucket", required=True),
        Field("credentials_file", "JSON da conta de servico", kind="file", width=34,
              help="vazio = usa GOOGLE_APPLICATION_CREDENTIALS"),
        Field("project", "Projeto", help="opcional"),
    ],
))
