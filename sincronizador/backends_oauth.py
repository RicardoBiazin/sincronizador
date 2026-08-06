"""Endpoints que exigem login OAuth: Dropbox, OneDrive e Google Drive.

Todos falam HTTP direto (via requests) com a API oficial de cada servico, sem
SDK proprio: o que eles tem em comum - autorizacao, renovacao de token, repetir
a chamada quando o token vence - fica em oauth.py e na classe base aqui.

Os tres preservam a data original do arquivo:
  Dropbox      client_modified no envio
  OneDrive     fileSystemInfo.lastModifiedDateTime
  Google Drive modifiedTime

Antes de usar, a conta precisa ser conectada uma vez pelo botao "Conectar" na
tela da tarefa, o que grava o refresh token (cifrado) na configuracao.
"""
from __future__ import annotations

import datetime as _dt
import io
import json
import os
import posixpath
import shutil
import threading
from typing import Dict, List, Optional

from . import oauth
from .endpoints import Endpoint, EndpointSpec, Field, FileInfo, register

#: acima disto o envio eh feito em pedacos (limites das proprias APIs)
LIMITE_ENVIO_SIMPLES = 4 * 1024 * 1024          # OneDrive
LIMITE_ENVIO_DROPBOX = 140 * 1024 * 1024        # Dropbox permite ate 150 MB
PEDACO = 8 * 1024 * 1024                        # tamanho de cada pedaco


def _iso(mtime: float, milissegundos: bool = False) -> str:
    q = _dt.datetime.fromtimestamp(float(mtime), _dt.timezone.utc)
    if milissegundos:
        return q.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (q.microsecond // 1000)
    return q.strftime("%Y-%m-%dT%H:%M:%SZ")


def _de_iso(s: Optional[str]) -> float:
    if not s:
        return 0.0
    try:
        texto = s.replace("Z", "+00:00")
        q = _dt.datetime.fromisoformat(texto)
        if q.tzinfo is None:
            q = q.replace(tzinfo=_dt.timezone.utc)
        return q.timestamp()
    except ValueError:
        return 0.0


class _LeitorPedacos:
    """Objeto com .read(n) a partir de um iterador de blocos."""

    def __init__(self, blocos):
        self._it = iter(blocos)
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


def _sessao_http():
    """requests.Session que repete chamadas em falhas transitorias.

    So repete o que da para repetir sem risco: queda de conexao antes do envio
    e as respostas de "tente de novo" (429, 5xx), e apenas nos metodos
    idempotentes - a lista padrao do urllib3, que deixa POST de fora. Repetir
    um POST poderia criar o arquivo duas vezes no Google Drive.
    """
    import requests
    from requests.adapters import HTTPAdapter
    try:
        from urllib3.util.retry import Retry
    except ImportError:                     # pragma: sem cobertura
        return requests.Session()
    politica = Retry(total=4, connect=4, read=2, status=3, backoff_factor=0.6,
                     status_forcelist=(429, 500, 502, 503, 504),
                     raise_on_status=False, respect_retry_after_header=True)
    s = requests.Session()
    adaptador = HTTPAdapter(max_retries=politica, pool_maxsize=16)
    s.mount("http://", adaptador)
    s.mount("https://", adaptador)
    return s


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------
class OAuthEndpoint(Endpoint):
    """Parte comum: sessao HTTP por thread, token renovado, erros legiveis."""

    parallel_safe = True
    preserves_mtime = True
    has_dirs = True
    timeout = 120

    def __init__(self, sessao: "oauth.Sessao"):
        self.sessao = sessao
        self._threads = threading.local()

    @property
    def http(self):
        # uma requests.Session por thread: a classe nao promete ser segura
        # para uso simultaneo, e o engine copia varios arquivos em paralelo
        s = getattr(self._threads, "sessao", None)
        if s is None:
            s = _sessao_http()
            self._threads.sessao = s
        return s

    def _pedir(self, metodo: str, url: str, **kw):
        cab = dict(kw.pop("headers", None) or {})
        corpo = kw.get("data")
        # so da para repetir a chamada se o corpo puder ser reenviado
        repetivel = corpo is None or isinstance(corpo, (bytes, bytearray, str))
        cab.update(self.sessao.cabecalho())
        r = self.http.request(metodo, url, headers=cab, timeout=self.timeout, **kw)
        if r.status_code == 401 and repetivel:
            cab.update(self.sessao.cabecalho(forcar=True))
            r = self.http.request(metodo, url, headers=cab, timeout=self.timeout, **kw)
        if r.status_code >= 400:
            raise IOError("%s %s -> HTTP %d: %s"
                          % (metodo, url.split("?")[0], r.status_code, r.text[:200]))
        return r

    def move_to_backup(self, rel: str, backup_base: str) -> None:
        try:
            dados = self.open_read(rel)
        except Exception:
            return
        destino = os.path.join(backup_base, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(destino), exist_ok=True)
        try:
            with open(destino, "wb") as f:
                shutil.copyfileobj(dados, f, length=1024 * 1024)
        finally:
            try:
                dados.close()
            except Exception:
                pass

    def close(self) -> None:
        s = getattr(self._threads, "sessao", None)
        if s is not None:
            try:
                s.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Dropbox
# ---------------------------------------------------------------------------
class DropboxEndpoint(OAuthEndpoint):
    API = "https://api.dropboxapi.com/2"
    CONTEUDO = "https://content.dropboxapi.com/2"

    def __init__(self, root: str, remote):
        super().__init__(oauth.sessao_de(remote, "dropbox"))
        self.api = str(remote.opt("api_url", "")) or self.API
        self.conteudo = str(remote.opt("content_url", "")) or self.CONTEUDO
        self.root = "/" + root.strip("/") if root.strip("/") else ""

    def _caminho(self, rel: str = "") -> str:
        return (self.root + "/" + rel) if rel else self.root

    def _rpc(self, metodo: str, corpo: dict) -> dict:
        r = self._pedir("POST", self.api + metodo,
                        headers={"Content-Type": "application/json"},
                        data=json.dumps(corpo))
        return r.json() if r.content else {}

    # -- leitura ------------------------------------------------------------
    def scan(self) -> Dict[str, FileInfo]:
        saida: Dict[str, FileInfo] = {}
        try:
            pagina = self._rpc("/files/list_folder",
                               {"path": self.root, "recursive": True,
                                "include_deleted": False})
        except IOError as e:
            if "path/not_found" in str(e):
                return saida         # pasta ainda nao existe no destino
            raise
        while True:
            for item in pagina.get("entries", []):
                if item.get(".tag") != "file":
                    continue
                completo = item.get("path_display") or item.get("path_lower") or ""
                rel = self._relativo(completo)
                if rel:
                    saida[rel] = FileInfo(
                        size=int(item.get("size", 0)),
                        mtime=_de_iso(item.get("client_modified")),
                        etag=item.get("content_hash", ""))
            if not pagina.get("has_more"):
                break
            pagina = self._rpc("/files/list_folder/continue",
                               {"cursor": pagina["cursor"]})
        return saida

    def _relativo(self, completo: str) -> Optional[str]:
        base = self.root
        if not base:
            return completo.lstrip("/")
        if completo.lower().startswith(base.lower() + "/"):
            return completo[len(base) + 1:]
        return None

    def open_read(self, rel: str):
        r = self._pedir("POST", self.conteudo + "/files/download",
                        headers={"Dropbox-API-Arg":
                                 json.dumps({"path": self._caminho(rel)})},
                        stream=True)
        r.raw.decode_content = True
        return r.raw

    # -- escrita ------------------------------------------------------------
    def write(self, rel: str, fobj, size: int, mtime: float) -> None:
        arg = {"path": self._caminho(rel), "mode": "overwrite",
               "autorename": False, "mute": True,
               "client_modified": _iso(mtime)}
        if size >= 0 and size <= LIMITE_ENVIO_DROPBOX:
            self._pedir("POST", self.conteudo + "/files/upload",
                        headers={"Dropbox-API-Arg": json.dumps(arg),
                                 "Content-Type": "application/octet-stream"},
                        data=fobj.read())
            return
        self._enviar_em_sessao(fobj, arg)

    def _enviar_em_sessao(self, fobj, arg: dict) -> None:
        """Arquivos grandes: /upload_session/start + append + finish."""
        primeiro = fobj.read(PEDACO)
        r = self._pedir("POST", self.conteudo + "/files/upload_session/start",
                        headers={"Dropbox-API-Arg": json.dumps({"close": False}),
                                 "Content-Type": "application/octet-stream"},
                        data=primeiro)
        sessao_id = r.json()["session_id"]
        deslocamento = len(primeiro)
        while True:
            pedaco = fobj.read(PEDACO)
            if not pedaco:
                break
            self._pedir(
                "POST", self.conteudo + "/files/upload_session/append_v2",
                headers={"Dropbox-API-Arg": json.dumps(
                    {"cursor": {"session_id": sessao_id, "offset": deslocamento},
                     "close": False}),
                    "Content-Type": "application/octet-stream"},
                data=pedaco)
            deslocamento += len(pedaco)
        self._pedir(
            "POST", self.conteudo + "/files/upload_session/finish",
            headers={"Dropbox-API-Arg": json.dumps(
                {"cursor": {"session_id": sessao_id, "offset": deslocamento},
                 "commit": arg}),
                "Content-Type": "application/octet-stream"},
            data=b"")

    def delete(self, rel: str) -> None:
        try:
            self._rpc("/files/delete_v2", {"path": self._caminho(rel)})
        except IOError:
            pass

    def probe(self) -> None:
        self._pedir("POST", self.api + "/users/get_current_account",
                    headers={"Content-Type": "application/json"}, data="null")


# ---------------------------------------------------------------------------
# OneDrive / SharePoint (Microsoft Graph)
# ---------------------------------------------------------------------------
class OneDriveEndpoint(OAuthEndpoint):
    GRAPH = "https://graph.microsoft.com/v1.0"

    def __init__(self, root: str, remote):
        super().__init__(oauth.sessao_de(remote, "microsoft"))
        base = str(remote.opt("graph_url", "")) or self.GRAPH
        drive = str(remote.opt("drive_id", "")).strip()
        self.drive = base + ("/drives/" + drive if drive else "/me/drive")
        self.root = root.strip("/")
        self._pastas_ok = set()

    def _item(self, rel: str) -> str:
        """URL do item pelo caminho (o Graph usa a sintaxe root:/caminho:)."""
        caminho = posixpath.join(self.root, rel) if self.root else rel
        caminho = caminho.strip("/")
        if not caminho:
            return self.drive + "/root"
        from urllib.parse import quote
        return self.drive + "/root:/" + quote(caminho)

    # -- leitura ------------------------------------------------------------
    def scan(self) -> Dict[str, FileInfo]:
        saida: Dict[str, FileInfo] = {}
        self._scan_dir("", saida)
        return saida

    def _scan_dir(self, rel: str, saida: Dict[str, FileInfo]) -> None:
        base = self._item(rel)
        url = (base + "/children") if base.endswith("/root") else (base + ":/children")
        while url:
            try:
                dados = self._pedir("GET", url).json()
            except IOError as e:
                if "HTTP 404" in str(e):
                    return           # pasta ainda nao existe
                raise
            for item in dados.get("value", []):
                nome = item.get("name", "")
                filho = posixpath.join(rel, nome) if rel else nome
                if "folder" in item:
                    self._scan_dir(filho, saida)
                    continue
                fsi = item.get("fileSystemInfo") or {}
                saida[filho] = FileInfo(
                    size=int(item.get("size", 0)),
                    mtime=_de_iso(fsi.get("lastModifiedDateTime")
                                  or item.get("lastModifiedDateTime")))
            url = dados.get("@odata.nextLink")

    def open_read(self, rel: str):
        r = self._pedir("GET", self._item(rel) + ":/content", stream=True)
        r.raw.decode_content = True
        return r.raw

    # -- escrita ------------------------------------------------------------
    def _criar_pastas(self, rel: str) -> None:
        partes = [p for p in
                  ((self.root + "/" + rel) if self.root else rel).split("/") if p][:-1]
        acumulado = []
        for p in partes:
            acumulado.append(p)
            caminho = "/".join(acumulado)
            if caminho in self._pastas_ok:
                continue
            pai = "/".join(acumulado[:-1])
            from urllib.parse import quote
            url = (self.drive + "/root/children") if not pai else \
                (self.drive + "/root:/" + quote(pai) + ":/children")
            try:
                self._pedir("POST", url,
                            headers={"Content-Type": "application/json"},
                            data=json.dumps({
                                "name": p, "folder": {},
                                "@microsoft.graph.conflictBehavior": "replace"}))
            except IOError:
                pass    # ja existe
            self._pastas_ok.add(caminho)

    def write(self, rel: str, fobj, size: int, mtime: float) -> None:
        self._criar_pastas(rel)
        fsi = {"lastModifiedDateTime": _iso(mtime)}
        if size >= 0 and size <= LIMITE_ENVIO_SIMPLES:
            r = self._pedir("PUT", self._item(rel) + ":/content",
                            headers={"Content-Type": "application/octet-stream"},
                            data=fobj.read())
            item = r.json() if r.content else {}
            ident = item.get("id")
            if ident:   # a data so pode ser ajustada depois do envio simples
                self._pedir("PATCH", self.drive + "/items/" + ident,
                            headers={"Content-Type": "application/json"},
                            data=json.dumps({"fileSystemInfo": fsi}))
            return
        self._enviar_em_sessao(rel, fobj, size, fsi)

    def _enviar_em_sessao(self, rel: str, fobj, size: int, fsi: dict) -> None:
        r = self._pedir("POST", self._item(rel) + ":/createUploadSession",
                        headers={"Content-Type": "application/json"},
                        data=json.dumps({"item": {
                            "@microsoft.graph.conflictBehavior": "replace",
                            "fileSystemInfo": fsi}}))
        url = r.json()["uploadUrl"]
        enviado = 0
        total = size
        while True:
            pedaco = fobj.read(PEDACO)
            if not pedaco:
                break
            fim = enviado + len(pedaco) - 1
            faixa = "bytes %d-%d/%d" % (enviado, fim, total)
            # a URL da sessao ja carrega a autorizacao; nao repetir o Bearer
            resp = self.http.put(url, data=pedaco,
                                 headers={"Content-Length": str(len(pedaco)),
                                          "Content-Range": faixa},
                                 timeout=self.timeout)
            if resp.status_code >= 400:
                raise IOError("envio em pedacos -> HTTP %d: %s"
                              % (resp.status_code, resp.text[:200]))
            enviado += len(pedaco)

    def delete(self, rel: str) -> None:
        try:
            self._pedir("DELETE", self._item(rel) + ":")
        except IOError:
            pass

    def probe(self) -> None:
        self._pedir("GET", self.drive)


# ---------------------------------------------------------------------------
# Google Drive
# ---------------------------------------------------------------------------
PASTA_GOOGLE = "application/vnd.google-apps.folder"


class GoogleDriveEndpoint(OAuthEndpoint):
    API = "https://www.googleapis.com/drive/v3"
    UPLOAD = "https://www.googleapis.com/upload/drive/v3"

    def __init__(self, root: str, remote):
        super().__init__(oauth.sessao_de(remote, "google"))
        self.api = str(remote.opt("api_url", "")) or self.API
        self.upload = str(remote.opt("upload_url", "")) or self.UPLOAD
        self.root = root.strip("/")
        self.raiz_id = str(remote.opt("folder_id", "")).strip() or "root"
        self.drive_compartilhado = bool(remote.opt("shared_drive", False))
        self._ids: Dict[str, str] = {}      # caminho relativo -> id
        self._raiz_resolvida = None
        self._ignorados = 0

    # -- utilidades ---------------------------------------------------------
    def _comuns(self) -> dict:
        if not self.drive_compartilhado:
            return {}
        return {"supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}

    def _listar(self, pai: str) -> List[dict]:
        itens, token = [], None
        while True:
            params = {
                "q": "'%s' in parents and trashed = false" % pai,
                "fields": "nextPageToken,files(id,name,mimeType,size,"
                          "modifiedTime,md5Checksum)",
                "pageSize": "1000",
            }
            params.update(self._comuns())
            if token:
                params["pageToken"] = token
            dados = self._pedir("GET", self.api + "/files", params=params).json()
            itens.extend(dados.get("files", []))
            token = dados.get("nextPageToken")
            if not token:
                break
        return itens

    def _achar_filho(self, pai: str, nome: str, pasta: bool) -> Optional[str]:
        for item in self._listar(pai):
            if item.get("name") == nome and \
                    (item.get("mimeType") == PASTA_GOOGLE) == pasta:
                return item["id"]
        return None

    def _id_raiz(self, criar: bool = False) -> Optional[str]:
        """Resolve o caminho da pasta raiz da tarefa para um id do Drive."""
        if self._raiz_resolvida and not criar:
            return self._raiz_resolvida
        atual = self.raiz_id
        for parte in [p for p in self.root.split("/") if p]:
            achado = self._achar_filho(atual, parte, pasta=True)
            if achado is None:
                if not criar:
                    return None
                achado = self._criar_pasta(atual, parte)
            atual = achado
        self._raiz_resolvida = atual
        return atual

    def _criar_pasta(self, pai: str, nome: str) -> str:
        corpo = {"name": nome, "mimeType": PASTA_GOOGLE, "parents": [pai]}
        r = self._pedir("POST", self.api + "/files", params=self._comuns(),
                        headers={"Content-Type": "application/json"},
                        data=json.dumps(corpo))
        return r.json()["id"]

    # -- leitura ------------------------------------------------------------
    def scan(self) -> Dict[str, FileInfo]:
        saida: Dict[str, FileInfo] = {}
        self._ids = {}
        self._ignorados = 0
        raiz = self._id_raiz()
        if raiz is None:
            return saida        # pasta ainda nao existe
        self._scan_dir(raiz, "", saida)
        return saida

    def _scan_dir(self, pai: str, rel: str, saida: Dict[str, FileInfo]) -> None:
        for item in self._listar(pai):
            nome = item.get("name", "")
            filho = posixpath.join(rel, nome) if rel else nome
            if item.get("mimeType") == PASTA_GOOGLE:
                self._ids[filho + "/"] = item["id"]
                self._scan_dir(item["id"], filho, saida)
                continue
            if str(item.get("mimeType", "")).startswith("application/vnd.google-apps"):
                # Documentos/Planilhas Google nao tem conteudo binario para
                # copiar (precisariam ser exportados): ficam de fora
                self._ignorados += 1
                continue
            self._ids[filho] = item["id"]
            saida[filho] = FileInfo(size=int(item.get("size", 0) or 0),
                                    mtime=_de_iso(item.get("modifiedTime")),
                                    etag=str(item.get("md5Checksum", "") or ""))

    def content_hash(self, rel: str, info: Optional[FileInfo] = None) -> str:
        return info.etag if info is not None else ""

    def ignorados(self) -> int:
        return self._ignorados

    def _id_de(self, rel: str) -> Optional[str]:
        if rel in self._ids:
            return self._ids[rel]
        pai = self._id_raiz()
        if pai is None:
            return None
        partes = rel.split("/")
        for p in partes[:-1]:
            pai = self._achar_filho(pai, p, pasta=True)
            if pai is None:
                return None
        achado = self._achar_filho(pai, partes[-1], pasta=False)
        if achado:
            self._ids[rel] = achado
        return achado

    def open_read(self, rel: str):
        ident = self._id_de(rel)
        if not ident:
            raise IOError("arquivo nao encontrado no Drive: %s" % rel)
        params = {"alt": "media"}
        params.update(self._comuns())
        r = self._pedir("GET", self.api + "/files/" + ident, params=params,
                        stream=True)
        r.raw.decode_content = True
        return r.raw

    # -- escrita ------------------------------------------------------------
    def _pasta_de(self, rel: str) -> str:
        """Id da pasta que contem 'rel', criando o que faltar."""
        pai = self._id_raiz(criar=True)
        acumulado = []
        for parte in rel.split("/")[:-1]:
            acumulado.append(parte)
            chave = "/".join(acumulado) + "/"
            if chave in self._ids:
                pai = self._ids[chave]
                continue
            achado = self._achar_filho(pai, parte, pasta=True) or \
                self._criar_pasta(pai, parte)
            self._ids[chave] = achado
            pai = achado
        return pai

    def write(self, rel: str, fobj, size: int, mtime: float) -> None:
        pai = self._pasta_de(rel)
        meta = {"name": rel.split("/")[-1],
                "modifiedTime": _iso(mtime, milissegundos=True)}
        ident = self._id_de(rel)
        if ident:
            url = self.upload + "/files/" + ident
            metodo = "PATCH"
        else:
            meta["parents"] = [pai]
            url = self.upload + "/files"
            metodo = "POST"
        params = {"uploadType": "multipart"}
        params.update(self._comuns())
        corpo, tipo = self._multipart(meta, fobj.read())
        r = self._pedir(metodo, url, params=params,
                        headers={"Content-Type": tipo}, data=corpo)
        try:
            novo = r.json().get("id")
            if novo:
                self._ids[rel] = novo
        except ValueError:
            pass

    @staticmethod
    def _multipart(meta: dict, dados: bytes):
        """Corpo multipart/related: metadados JSON + conteudo, numa chamada."""
        limite = "==sincronizador-%s==" % os.urandom(8).hex()
        partes = [
            ("--" + limite).encode(),
            b"Content-Type: application/json; charset=UTF-8",
            b"",
            json.dumps(meta).encode("utf-8"),
            ("--" + limite).encode(),
            b"Content-Type: application/octet-stream",
            b"",
            dados,
            ("--" + limite + "--").encode(),
        ]
        return b"\r\n".join(partes), "multipart/related; boundary=" + limite

    def delete(self, rel: str) -> None:
        ident = self._id_de(rel)
        if not ident:
            return
        try:
            self._pedir("DELETE", self.api + "/files/" + ident,
                        params=self._comuns())
            self._ids.pop(rel, None)
        except IOError:
            pass

    def probe(self) -> None:
        self._pedir("GET", self.api + "/about", params={"fields": "user"})


# ---------------------------------------------------------------------------
# Registro
# ---------------------------------------------------------------------------
_AJUDA_REDIR = ("Cadastre no provedor o endereco de redirecionamento "
                + oauth.redirect_uri())

register(EndpointSpec(
    kind="dropbox",
    label="Dropbox",
    factory=lambda path, remote: DropboxEndpoint(path, remote),
    path_label="Pasta no Dropbox:",
    requires=["requests"],
    note=oauth.PROVEDORES["dropbox"].ajuda + " " + _AJUDA_REDIR,
    fields=[
        Field("client_id", "App key", width=30, required=True),
        Field("client_secret", "App secret", kind="password", width=30,
              help="opcional (PKCE dispensa)"),
        Field("refresh_token", "Conta", kind="oauth", provedor="dropbox"),
    ],
))

register(EndpointSpec(
    kind="onedrive",
    label="Microsoft OneDrive / SharePoint",
    factory=lambda path, remote: OneDriveEndpoint(path, remote),
    path_label="Pasta no OneDrive:",
    requires=["requests"],
    note=oauth.PROVEDORES["microsoft"].ajuda + " " + _AJUDA_REDIR,
    fields=[
        Field("client_id", "ID do aplicativo", width=38, required=True),
        Field("tenant", "Tenant", width=30, help="vazio = common"),
        Field("drive_id", "ID do drive", width=38,
              help="vazio = o OneDrive do usuario"),
        Field("refresh_token", "Conta", kind="oauth", provedor="microsoft"),
    ],
))

register(EndpointSpec(
    kind="gdrive",
    label="Google Drive",
    factory=lambda path, remote: GoogleDriveEndpoint(path, remote),
    path_label="Pasta no Drive:",
    requires=["requests"],
    note=oauth.PROVEDORES["google"].ajuda + " " + _AJUDA_REDIR,
    fields=[
        Field("client_id", "Client ID", width=38, required=True),
        Field("client_secret", "Client secret", kind="password", width=30,
              required=True),
        Field("folder_id", "ID da pasta inicial", width=38,
              help="vazio = Meu Drive"),
        Field("shared_drive", "E um drive compartilhado", kind="bool"),
        Field("refresh_token", "Conta", kind="oauth", provedor="google"),
    ],
))
