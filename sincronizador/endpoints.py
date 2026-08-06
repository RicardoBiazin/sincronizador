"""Abstracao de "endpoints": local, FTP e SFTP.

Cada endpoint sabe:
  - scan()            -> {relpath: FileInfo}
  - open_read(rel)    -> objeto binario para leitura
  - write(rel, fobj, size, mtime)
  - delete(rel)
  - move_to_backup(rel, backup_base)  (versionamento antes de sobrescrever/apagar)
  - probe()           -> testa a conexao (levanta excecao se falhar)

Os caminhos relativos sempre usam '/'. mtime em segundos (float, UTC epoch).

Novos tipos (WebDAV, S3, Azure, ...) se acoplam por register(EndpointSpec(...)):
a fabrica, os campos de conexao da interface e os pacotes necessarios ficam
todos declarados no proprio registro - nem a GUI nem o engine precisam saber
que o tipo existe.
"""
from __future__ import annotations

import io
import os
import posixpath
import shutil
import stat as statmod
from dataclasses import dataclass, field as dcfield
from typing import Callable, Dict, List, Optional


# tolerancia de comparacao de data (FAT tem resolucao de 2s; FTP arredonda)
MTIME_TOLERANCE = 2.0


@dataclass
class FileInfo:
    size: int
    mtime: float
    etag: str = ""   # hash de conteudo, quando o servico fornece (S3, Drive...)


class Endpoint:
    """Interface base.

    As capacidades abaixo sao lidas pelo engine. Podem ser sobrescritas na
    classe (valor fixo do tipo) ou na instancia (descoberto na conexao, como
    o suporte a MFMT no FTP).
    """

    #: consegue gravar a data original do arquivo em write()
    preserves_mtime = True
    #: varias threads podem usar a MESMA instancia ao mesmo tempo
    parallel_safe = False
    #: existe hierarquia real de diretorios (object storage: False)
    has_dirs = True
    #: o endpoint eh uma pasta do sistema de arquivos local
    is_local = False

    def scan(self) -> Dict[str, FileInfo]:
        raise NotImplementedError

    def open_read(self, rel: str):
        raise NotImplementedError

    def write(self, rel: str, fobj, size: int, mtime: float) -> None:
        raise NotImplementedError

    def delete(self, rel: str) -> None:
        raise NotImplementedError

    def move_to_backup(self, rel: str, backup_base: str) -> None:
        raise NotImplementedError

    def probe(self) -> None:
        """Verifica se da para usar o endpoint. Levanta excecao se nao der."""
        self.scan()

    def content_hash(self, rel: str, info: Optional[FileInfo] = None) -> str:
        """MD5 do conteudo em hexadecimal, se o servico ja souber informar.

        Devolver "" significa "nao sei de graca" - ai o engine le o arquivo e
        calcula. Object storage costuma entregar o MD5 na propria listagem, o
        que torna a comparacao por conteudo barata desse lado.
        """
        return ""

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def same_file(a: FileInfo, b: FileInfo, compare_mtime: bool = True,
              tolerance: float = MTIME_TOLERANCE) -> bool:
    """Dois arquivos sao "iguais" para efeito de sincronizacao?

    Tamanho diferente sempre significa diferente. Etags iguais provam
    igualdade; etags diferentes NAO provam nada (cada servico calcula de um
    jeito), entao caem na comparacao por data. Com compare_mtime=False a data
    eh ignorada - usado quando algum dos lados nao preserva a data original.
    """
    if a.size != b.size:
        return False
    if a.etag and b.etag and a.etag == b.etag:
        return True
    if not compare_mtime:
        return True
    return abs(a.mtime - b.mtime) <= tolerance


# ---------------------------------------------------------------------------
# LOCAL / rede (inclui Google Drive e OneDrive montados como pasta)
# ---------------------------------------------------------------------------
class LocalEndpoint(Endpoint):
    preserves_mtime = True
    parallel_safe = True
    has_dirs = True
    is_local = True

    def __init__(self, root: str):
        self.root = os.path.abspath(root)

    def _abs(self, rel: str) -> str:
        return os.path.join(self.root, rel.replace("/", os.sep))

    def probe(self) -> None:
        if not os.path.isdir(self.root):
            raise OSError("pasta nao encontrada: %s" % self.root)

    def scan(self) -> Dict[str, FileInfo]:
        result: Dict[str, FileInfo] = {}
        if not os.path.isdir(self.root):
            return result
        for dirpath, _dirs, files in os.walk(self.root):
            for fn in files:
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, self.root).replace(os.sep, "/")
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                result[rel] = FileInfo(size=st.st_size, mtime=st.st_mtime)
        return result

    def open_read(self, rel: str):
        return open(self._abs(rel), "rb")

    def write(self, rel: str, fobj, size: int, mtime: float) -> None:
        dest = self._abs(rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = dest + ".sinctmp"
        with open(tmp, "wb") as out:
            shutil.copyfileobj(fobj, out, length=1024 * 1024)
        os.replace(tmp, dest)
        try:
            os.utime(dest, (mtime, mtime))
        except OSError:
            pass

    def delete(self, rel: str) -> None:
        try:
            os.remove(self._abs(rel))
        except FileNotFoundError:
            pass
        self._prune_empty(os.path.dirname(self._abs(rel)))

    def _prune_empty(self, path: str) -> None:
        try:
            while os.path.abspath(path) != self.root and not os.listdir(path):
                os.rmdir(path)
                path = os.path.dirname(path)
        except OSError:
            pass

    def move_to_backup(self, rel: str, backup_base: str) -> None:
        src = self._abs(rel)
        if not os.path.exists(src):
            return
        dst = os.path.join(backup_base, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        try:
            shutil.copy2(src, dst)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# FTP / FTPS
# ---------------------------------------------------------------------------
class FtpEndpoint(Endpoint):
    # uma conexao de controle so aguenta uma transferencia por vez
    parallel_safe = False
    has_dirs = True
    # otimista: vira False na primeira vez que o servidor recusar MFMT
    preserves_mtime = True

    def __init__(self, root: str, remote):
        from ftplib import FTP, FTP_TLS
        port = remote.port or 21
        if remote.tls:
            self.ftp = FTP_TLS()
        else:
            self.ftp = FTP()
        self.ftp.connect(remote.host, port, timeout=30)
        self.ftp.login(remote.user or "anonymous", remote.password or "")
        if remote.tls:
            self.ftp.prot_p()
        self.ftp.set_pasv(remote.passive)
        self.root = "/" + root.strip("/") if root.strip("/") else "/"

    def _rpath(self, rel: str) -> str:
        return posixpath.join(self.root, rel)

    def scan(self) -> Dict[str, FileInfo]:
        result: Dict[str, FileInfo] = {}
        self._scan_dir(self.root, "", result)
        return result

    def _scan_dir(self, abspath: str, rel: str, out: Dict[str, FileInfo]) -> None:
        entries = []
        try:
            # MLSD da metadados confiaveis; cai para nlst se indisponivel
            for name, facts in self.ftp.mlsd(abspath):
                if name in (".", ".."):
                    continue
                entries.append((name, facts))
        except Exception:
            try:
                names = self.ftp.nlst(abspath)
            except Exception:
                return
            for full in names:
                name = posixpath.basename(full)
                if name in (".", ".."):
                    continue
                entries.append((name, {}))
        for name, facts in entries:
            child_abs = posixpath.join(abspath, name)
            child_rel = posixpath.join(rel, name) if rel else name
            ftype = facts.get("type")
            if ftype == "dir":
                self._scan_dir(child_abs, child_rel, out)
            elif ftype == "file" or ftype is None:
                size = int(facts.get("size", 0)) if facts.get("size") else self._size(child_abs)
                mtime = self._parse_mtime(facts.get("modify")) if facts.get("modify") else self._mtime(child_abs)
                if size is None:  # provavel diretorio no fallback nlst
                    self._scan_dir(child_abs, child_rel, out)
                else:
                    out[child_rel] = FileInfo(size=size, mtime=mtime or 0.0)

    def _size(self, abspath: str) -> Optional[int]:
        try:
            return self.ftp.size(abspath)
        except Exception:
            return None

    def _mtime(self, abspath: str) -> Optional[float]:
        try:
            resp = self.ftp.sendcmd("MDTM " + abspath)  # "213 YYYYMMDDHHMMSS"
            return self._parse_mtime(resp.split()[-1])
        except Exception:
            return None

    @staticmethod
    def _parse_mtime(s: Optional[str]) -> Optional[float]:
        if not s:
            return None
        import calendar
        import time
        s = s.split(".")[0]
        try:
            t = time.strptime(s[:14], "%Y%m%d%H%M%S")
            return calendar.timegm(t)  # FTP MDTM/modify sao UTC
        except ValueError:
            return None

    def _ensure_dirs(self, abspath: str) -> None:
        parts = abspath.strip("/").split("/")
        cur = ""
        for p in parts[:-1]:
            cur += "/" + p
            try:
                self.ftp.mkd(cur)
            except Exception:
                pass

    def open_read(self, rel: str):
        buf = io.BytesIO()
        self.ftp.retrbinary("RETR " + self._rpath(rel), buf.write)
        buf.seek(0)
        return buf

    def write(self, rel: str, fobj, size: int, mtime: float) -> None:
        abspath = self._rpath(rel)
        self._ensure_dirs(abspath)
        self.ftp.storbinary("STOR " + abspath, fobj)
        try:  # nem todo servidor aceita MFMT
            import time
            self.ftp.sendcmd("MFMT " + time.strftime("%Y%m%d%H%M%S", time.gmtime(mtime)) + " " + abspath)
        except Exception:
            # sem MFMT o arquivo fica com a data do servidor: avisa o engine
            # para nao comparar por data (senao reenviaria tudo toda vez)
            self.preserves_mtime = False

    def delete(self, rel: str) -> None:
        try:
            self.ftp.delete(self._rpath(rel))
        except Exception:
            pass

    def move_to_backup(self, rel: str, backup_base: str) -> None:
        # backup de arquivos remotos: baixa para pasta local de backup
        try:
            data = self.open_read(rel)
        except Exception:
            return
        dst = os.path.join(backup_base, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "wb") as f:
            shutil.copyfileobj(data, f)

    def probe(self) -> None:
        self.ftp.voidcmd("NOOP")

    def close(self) -> None:
        try:
            self.ftp.quit()
        except Exception:
            try:
                self.ftp.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# SFTP (paramiko)
# ---------------------------------------------------------------------------
class SftpEndpoint(Endpoint):
    preserves_mtime = True
    # o SFTPClient nao eh thread-safe (um canal, requisicoes intercaladas)
    parallel_safe = False
    has_dirs = True

    def __init__(self, root: str, remote):
        import paramiko
        port = remote.port or 22
        self.transport = paramiko.Transport((remote.host, port))
        if remote.key_file:
            key = paramiko.RSAKey.from_private_key_file(remote.key_file, password=remote.password or None)
            self.transport.connect(username=remote.user, pkey=key)
        else:
            self.transport.connect(username=remote.user, password=remote.password)
        self.sftp = paramiko.SFTPClient.from_transport(self.transport)
        self.root = "/" + root.strip("/") if root.strip("/") else "."

    def _rpath(self, rel: str) -> str:
        return posixpath.join(self.root, rel)

    def scan(self) -> Dict[str, FileInfo]:
        result: Dict[str, FileInfo] = {}
        self._scan_dir(self.root, "", result)
        return result

    def _scan_dir(self, abspath: str, rel: str, out: Dict[str, FileInfo]) -> None:
        try:
            entries = self.sftp.listdir_attr(abspath)
        except IOError:
            return
        for attr in entries:
            name = attr.filename
            if name in (".", ".."):
                continue
            child_abs = posixpath.join(abspath, name)
            child_rel = posixpath.join(rel, name) if rel else name
            if statmod.S_ISDIR(attr.st_mode):
                self._scan_dir(child_abs, child_rel, out)
            elif statmod.S_ISREG(attr.st_mode):
                out[child_rel] = FileInfo(size=attr.st_size, mtime=float(attr.st_mtime))

    def _ensure_dirs(self, abspath: str) -> None:
        parts = abspath.strip("/").split("/")
        cur = "" if self.root.startswith("/") else "."
        for p in parts[:-1]:
            cur = (cur + "/" + p) if cur not in ("", ".") else ("/" + p if self.root.startswith("/") else p)
            try:
                self.sftp.stat(cur)
            except IOError:
                try:
                    self.sftp.mkdir(cur)
                except IOError:
                    pass

    def open_read(self, rel: str):
        return self.sftp.open(self._rpath(rel), "rb")

    def write(self, rel: str, fobj, size: int, mtime: float) -> None:
        abspath = self._rpath(rel)
        self._ensure_dirs(abspath)
        with self.sftp.open(abspath, "wb") as out:
            shutil.copyfileobj(fobj, out, length=1024 * 1024)
        try:
            self.sftp.utime(abspath, (mtime, mtime))
        except IOError:
            self.preserves_mtime = False

    def delete(self, rel: str) -> None:
        try:
            self.sftp.remove(self._rpath(rel))
        except IOError:
            pass

    def move_to_backup(self, rel: str, backup_base: str) -> None:
        try:
            data = self.open_read(rel)
        except IOError:
            return
        dst = os.path.join(backup_base, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "wb") as f:
            shutil.copyfileobj(data, f)

    def probe(self) -> None:
        try:
            self.sftp.stat(self.root)
        except IOError:
            self.sftp.listdir(".")   # raiz ainda nao existe, mas a sessao vive

    def close(self) -> None:
        try:
            self.sftp.close()
        finally:
            try:
                self.transport.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Registro de tipos de endpoint (fabrica + metadados para a interface)
# ---------------------------------------------------------------------------
#: tipos de campo aceitos em Field.kind
#: 'oauth' nao eh digitado: eh preenchido pelo botao "Conectar" (ver oauth.py)
FIELD_KINDS = ("text", "password", "int", "bool", "file", "dir", "oauth")

#: campos cujo valor eh credencial - gravados cifrados (ver segredos.py)
FIELD_KINDS_SECRETOS = ("password", "oauth")


@dataclass
class Field:
    """Um campo de conexao, renderizado automaticamente pela GUI.

    'key' que corresponda a um atributo de config.Remote eh gravado nele;
    qualquer outro nome vai para Remote.options (dicionario livre), que eh
    onde os tipos novos guardam bucket, regiao, token, etc.
    """
    key: str
    label: str
    kind: str = "text"
    help: str = ""
    required: bool = False
    default: object = ""
    width: int = 22
    #: so para kind='oauth': nome do provedor em oauth.PROVEDORES
    provedor: str = ""

    @property
    def secreto(self) -> bool:
        return self.kind in FIELD_KINDS_SECRETOS


@dataclass
class EndpointSpec:
    kind: str                                    # id usado na configuracao
    label: str                                   # texto na interface
    factory: Callable[..., Endpoint]             # (path, remote) -> Endpoint
    fields: List[Field] = dcfield(default_factory=list)
    path_label: str = "Caminho:"
    path_browse: bool = False                    # mostra o botao "..."
    #: modulos necessarios. "modulo" ou "modulo:nome-no-pip" quando os dois
    #: nomes diferem (ex.: "azure.storage.fileshare:azure-storage-file-share")
    requires: List[str] = dcfield(default_factory=list)
    note: str = ""                               # dica exibida na interface


_SPECS: "Dict[str, EndpointSpec]" = {}


def register(spec: EndpointSpec) -> EndpointSpec:
    _SPECS[spec.kind] = spec
    return spec


def get_spec(kind: str) -> EndpointSpec:
    try:
        return _SPECS[kind]
    except KeyError:
        raise ValueError("Tipo de endpoint desconhecido: %r" % kind)


def endpoint_kinds() -> List[str]:
    return list(_SPECS)


def endpoint_fields(kind: str) -> List[Field]:
    return get_spec(kind).fields if kind in _SPECS else []


def campos_secretos(kind: str) -> List[str]:
    """Chaves cujo valor deve ser gravado cifrado."""
    if kind not in _SPECS:
        return ["password"]     # tipo desconhecido: protege o obvio
    return [f.key for f in _SPECS[kind].fields if f.secreto]


def missing_requirements(kind: str) -> List[str]:
    """Pacotes declarados pelo tipo que nao estao instalados.

    'requires' lista o modulo ('azure.storage.blob'); o retorno eh o nome de
    instalacao ('azure-storage-blob'), que eh o que interessa ao usuario.
    """
    import importlib.util
    out = []
    for item in get_spec(kind).requires:
        mod, _, pip_name = item.partition(":")
        try:
            found = importlib.util.find_spec(mod) is not None
        except (ImportError, ValueError, AttributeError):
            found = False
        if not found:
            out.append(pip_name or mod.replace(".", "-"))
    return out


def make_endpoint(path: str, kind: str, remote) -> Endpoint:
    spec = get_spec(kind)
    faltando = missing_requirements(kind)
    if faltando:
        raise RuntimeError(
            "%s precisa dos pacotes: %s (pip install %s)"
            % (spec.label, ", ".join(faltando), " ".join(faltando)))
    return spec.factory(path, remote)


register(EndpointSpec(
    kind="local",
    label="Pasta local / rede",
    factory=lambda path, remote: LocalEndpoint(path),
    path_label="Pasta:",
    path_browse=True,
    note="Serve tambem para Google Drive e OneDrive sincronizados como pasta.",
))

register(EndpointSpec(
    kind="ftp",
    label="FTP / FTPS",
    factory=lambda path, remote: FtpEndpoint(path, remote),
    path_label="Pasta remota:",
    fields=[
        Field("host", "Host", required=True),
        Field("port", "Porta", kind="int", help="vazio = 21", width=6),
        Field("user", "Usuario"),
        Field("password", "Senha", kind="password", width=16),
        Field("tls", "TLS (FTPS)", kind="bool"),
        Field("passive", "Modo passivo", kind="bool", default=True),
    ],
))

register(EndpointSpec(
    kind="sftp",
    label="SFTP (SSH)",
    factory=lambda path, remote: SftpEndpoint(path, remote),
    path_label="Pasta remota:",
    requires=["paramiko"],
    fields=[
        Field("host", "Host", required=True),
        Field("port", "Porta", kind="int", help="vazio = 22", width=6),
        Field("user", "Usuario", required=True),
        Field("password", "Senha", kind="password", width=16,
              help="senha do usuario, ou da chave privada"),
        Field("key_file", "Chave privada", kind="file", width=30),
    ],
))


# Registra os demais tipos. Fica no fim do arquivo de proposito: esses modulos
# importam nomes daqui, que a esta altura ja estao todos definidos.
from . import backends as _backends              # noqa: E402,F401
from . import backends_oauth as _backends_oauth  # noqa: E402,F401
