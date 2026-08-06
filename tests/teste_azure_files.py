"""Azure Files nao tem emulador (o Azurite so faz Blob/Queue/Table).

Entao o teste usa create_autospec sobre as CLASSES REAIS do SDK: os dublês
tem as assinaturas verdadeiras, e qualquer chamada com argumento errado ou
faltando levanta TypeError - foi assim que apareceu o set_http_headers().
Um "compartilhamento" em memoria guarda os dados para conferir o resultado.
"""
import datetime as dt, io, logging, os, shutil, sys, tempfile
from unittest.mock import create_autospec, MagicMock

sys.path.insert(0, r"c:\DEV\sincronizador")
from azure.storage.fileshare import (ShareClient, ShareDirectoryClient,
                                     ShareFileClient, FileProperties,
                                     DirectoryProperties)
from sincronizador import config as cfgmod, endpoints as ep, engine
from sincronizador import backends

logging.basicConfig(level=logging.WARNING, format="%(message)s")
log = logging.getLogger("t"); log.setLevel(logging.INFO)

falhas = []


def check(cond, msg):
    print(("  OK   " if cond else "  FALHA") + " " + msg)
    if not cond:
        falhas.append(msg)


class ShareFalso:
    """Compartilhamento em memoria: {caminho: (bytes, datetime)} e pastas."""

    def __init__(self):
        self.arquivos = {}
        self.pastas = {""}
        self.props_lidas = 0

    # -- clientes com a assinatura real do SDK ---------------------------
    def get_directory_client(self, directory_path=""):
        d = create_autospec(ShareDirectoryClient, instance=True)
        caminho = (directory_path or "").strip("/")
        d.create_directory.side_effect = lambda **kw: self.pastas.add(caminho)
        d.list_directories_and_files.side_effect = \
            lambda *a, **kw: self._listar(caminho, **kw)
        return d

    def get_file_client(self, file_path):
        f = create_autospec(ShareFileClient, instance=True)
        caminho = file_path.strip("/")

        def _upload(data, length=None, **kw):
            pai = caminho.rsplit("/", 1)[0] if "/" in caminho else ""
            if pai not in self.pastas:
                raise IOError("pasta inexistente: %r" % pai)
            self.arquivos[caminho] = (data.read(),
                                      kw.get("file_last_write_time"))
        f.upload_file.side_effect = _upload

        def _download(**kw):
            d = MagicMock()
            d.chunks.return_value = iter([self.arquivos[caminho][0]])
            return d
        f.download_file.side_effect = _download
        f.delete_file.side_effect = lambda **kw: self.arquivos.pop(caminho, None)
        return f

    def get_share_properties(self, **kw):
        self.props_lidas += 1
        return {}

    def close(self):
        pass

    def _listar(self, pasta, **kw):
        if kw.get("include") not in (None, ["timestamps"]):
            raise ValueError("include inesperado: %r" % kw.get("include"))
        pref = (pasta + "/") if pasta else ""
        vistos, saida = set(), []
        for cam, (dados, quando) in self.arquivos.items():
            if not cam.startswith(pref):
                continue
            resto = cam[len(pref):]
            if "/" in resto:
                nome = resto.split("/")[0]
                if nome not in vistos:
                    vistos.add(nome)
                    p = DirectoryProperties(); p.name = nome; p.is_directory = True
                    saida.append(p)
            else:
                p = FileProperties()
                p.name = resto; p.is_directory = False; p.size = len(dados)
                p.last_write_time = quando
                saida.append(p)
        return saida


compartilhamento = ShareFalso()


def _fabrica(path, remote):
    """Cria o endpoint real, mas com o ShareClient trocado pelo dublê."""
    import inspect
    e = backends.AzureFileEndpoint.__new__(backends.AzureFileEndpoint)
    e._data_no_upload = "file_last_write_time" in inspect.signature(
        ShareFileClient.upload_file).parameters
    e.preserves_mtime = e._data_no_upload
    e.share = compartilhamento
    e.root = path.strip("/")
    e._pastas_ok = set()
    return e


ep.register(ep.EndpointSpec(kind="azurefiles_t", label="Azure Files (dublê)",
                            factory=_fabrica))

print("\n=== Azure Files (dublê com assinaturas reais do SDK) ===")
check(_fabrica("", cfgmod.Remote())._data_no_upload,
      "SDK atual aceita file_last_write_time no upload_file")

base = tempfile.mkdtemp(prefix="sinc_af_")
src = os.path.join(base, "origem")
os.makedirs(os.path.join(src, "sub"))
dados = {"a.txt": b"conteudo A", "sub/b.bin": bytes(range(256)) * 4}
for n, d in dados.items():
    p = os.path.join(src, n.replace("/", os.sep))
    with open(p, "wb") as f:
        f.write(d)
    os.utime(p, (1_600_000_000, 1_600_000_000))

job = cfgmod.Job(name="t_af", mode="espelho", source=src, dest="dados/envio",
                 source_type="local", dest_type="azurefiles_t", validate=True)
st = engine.run_job(job, log, workers=4)
check(st.copied == 2 and not st.errors, f"enviou 2 arquivos (c={st.copied} err={st.errors})")
check(sorted(compartilhamento.arquivos) == ["dados/envio/a.txt", "dados/envio/sub/b.bin"],
      f"caminhos montados com a raiz: {sorted(compartilhamento.arquivos)}")
check(compartilhamento.arquivos["dados/envio/sub/b.bin"][0] == dados["sub/b.bin"],
      "conteudo binario integro")
check("dados/envio/sub" in compartilhamento.pastas,
      f"criou as pastas antes do upload: {sorted(compartilhamento.pastas)}")

quando = compartilhamento.arquivos["dados/envio/a.txt"][1]
check(isinstance(quando, dt.datetime) and abs(quando.timestamp() - 1_600_000_000) < 1,
      f"data original enviada no upload ({quando})")
check(not st.validation_failed, f"validacao ok ({st.validation_failed})")

st = engine.run_job(job, log, workers=4)
check(st.copied == 0 and st.updated == 0,
      f"2a passada nao reenvia (c={st.copied} u={st.updated})")

recs = []
h = logging.Handler(); h.emit = lambda r: recs.append(r.getMessage())
log.addHandler(h); engine.run_job(job, log, workers=4); log.removeHandler(h)
check(any("ate 4 arquivo(s) por vez" in m for m in recs), "Azure Files usa 4 threads")

e = _fabrica("dados/envio", cfgmod.Remote())
achados = sorted(e.scan())
check(achados == ["a.txt", "sub/b.bin"], f"scan recursivo: {achados}")
check(e.open_read("a.txt").read() == b"conteudo A", "leitura por chunks funciona")
check(e.preserves_mtime is True, "Azure Files declara preservar a data")
e.probe()
check(compartilhamento.props_lidas >= 1, "probe() consulta o compartilhamento")

# volta: Azure Files -> local
volta = os.path.join(base, "volta")
jv = cfgmod.Job(name="t_af_volta", mode="espelho", source="dados/envio", dest=volta,
                source_type="azurefiles_t", dest_type="local", validate=True)
st = engine.run_job(jv, log, workers=4)
check(st.copied == 2 and not st.errors, f"baixou 2 (c={st.copied} err={st.errors})")
with open(os.path.join(volta, "sub", "b.bin"), "rb") as f:
    check(f.read() == dados["sub/b.bin"], "binario voltou intacto")

# espelho apaga
os.remove(os.path.join(src, "a.txt"))
st = engine.run_job(job, log)
check("dados/envio/a.txt" not in compartilhamento.arquivos,
      f"espelho apagou no compartilhamento ({st.deleted} apagado)")

shutil.rmtree(base, ignore_errors=True)
for n in ("t_af", "t_af_volta"):
    try: os.remove(engine._state_path(n))
    except OSError: pass

print("\n" + ("AZURE FILES: TODOS OS TESTES PASSARAM" if not falhas else "FALHAS: %d" % len(falhas)))
for f in falhas:
    print("  - " + f)
sys.exit(1 if falhas else 0)
