"""Servidores falsos das APIs de Dropbox, Microsoft Graph e Google Drive.

Implementam as chamadas que o Sincronizador usa, no formato documentado por
cada provedor: rotas, cabecalhos, paginacao, envio em pedacos e erros. Servem
para exercitar o cliente por HTTP de verdade.
"""
import json
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _agora_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class BaseFake(BaseHTTPRequestHandler):
    estado = None       # definido na subclasse
    # HTTP/1.1 com Content-Length em toda resposta: keep-alive de verdade,
    # como nos servicos reais (com HTTP/1.0 o pool do requests reaproveitava
    # conexoes ja fechadas e a chamada morria de vez em quando)
    protocol_version = "HTTP/1.1"

    def handle_one_request(self):
        # com keep-alive o MESMO handler atende varias requisicoes na mesma
        # conexao: o corpo memorizado precisa ser esquecido a cada uma
        self.__dict__.pop("_corpo_lido", None)
        return BaseHTTPRequestHandler.handle_one_request(self)

    # -- utilidades ------------------------------------------------------
    def corpo(self):
        # memoriza: com keep-alive o corpo PRECISA ser lido inteiro antes de
        # responder, senao os bytes que sobram viram a proxima requisicao
        if not hasattr(self, "_corpo_lido"):
            n = int(self.headers.get("Content-Length", 0) or 0)
            self._corpo_lido = self.rfile.read(n) if n else b""
        return self._corpo_lido

    def json_corpo(self):
        b = self.corpo()
        return json.loads(b) if b else {}

    def responder(self, cod, obj=None, bruto=None, tipo="application/json"):
        if bruto is None:
            bruto = json.dumps(obj if obj is not None else {}).encode()
        self.send_response(cod)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(bruto)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(bruto)

    def erro(self, cod, msg):
        self.responder(cod, {"error": msg, "error_summary": msg})

    def autorizado(self):
        self.corpo()          # drena o corpo antes de qualquer resposta
        cab = self.headers.get("Authorization", "")
        est = type(self).estado
        if cab != "Bearer " + est["token_valido"]:
            est["nao_autorizadas"] += 1
            self.erro(401, "invalid_access_token")
            return False
        return True

    def log_message(self, *a):
        pass

    # -- token OAuth (comum aos tres) -------------------------------------
    def talvez_token(self):
        if not self.path.startswith("/oauth/token"):
            return False
        dados = {k: v[0] for k, v in
                 urllib.parse.parse_qs(self.corpo().decode()).items()}
        est = type(self).estado
        est["renovacoes"] += 1
        if dados.get("grant_type") == "refresh_token" and \
                dados.get("refresh_token") == est["refresh_token"]:
            self.responder(200, {"access_token": est["token_valido"],
                                 "expires_in": 3600})
        else:
            self.responder(400, {"error": "invalid_grant"})
        return True


# =========================================================================
# Dropbox
# =========================================================================
class DropboxFake(BaseFake):
    estado = None

    def do_POST(self):
        if self.talvez_token():
            return
        if not self.autorizado():
            return
        est = type(self).estado
        rota = urllib.parse.urlparse(self.path).path

        if rota == "/2/users/get_current_account":
            return self.responder(200, {"account_id": "dbid:teste",
                                        "email": "teste@exemplo.com"})

        if rota == "/2/files/list_folder":
            p = self.json_corpo()
            base = (p.get("path") or "").rstrip("/").lower()
            itens = []
            for caminho, (dados, quando) in sorted(est["arquivos"].items()):
                if base and not caminho.lower().startswith(base + "/"):
                    continue
                itens.append({".tag": "file", "name": caminho.split("/")[-1],
                              "path_display": caminho, "path_lower": caminho.lower(),
                              "size": len(dados), "client_modified": quando,
                              "server_modified": _agora_iso(),
                              "content_hash": "hash-" + str(len(dados))})
            if base and base not in est["pastas"] and not itens:
                return self.responder(409, {"error_summary": "path/not_found/..",
                                            "error": {".tag": "path"}})
            # devolve um item por pagina, para exercitar o cursor
            est["paginas"] = itens[1:]
            return self.responder(200, {"entries": itens[:1],
                                        "cursor": "CUR1",
                                        "has_more": bool(itens[1:])})

        if rota == "/2/files/list_folder/continue":
            restantes = est.get("paginas") or []
            est["paginas"] = restantes[1:]
            return self.responder(200, {"entries": restantes[:1], "cursor": "CUR1",
                                        "has_more": bool(restantes[1:])})

        if rota == "/2/files/download":
            arg = json.loads(self.headers.get("Dropbox-API-Arg", "{}"))
            item = est["arquivos"].get(arg.get("path"))
            if item is None:
                return self.responder(409, {"error_summary": "path/not_found/.."})
            return self.responder(200, bruto=item[0],
                                  tipo="application/octet-stream")

        if rota == "/2/files/upload":
            arg = json.loads(self.headers.get("Dropbox-API-Arg", "{}"))
            est["arquivos"][arg["path"]] = (self.corpo(), arg.get("client_modified"))
            est["envios_simples"] += 1
            return self.responder(200, {"name": arg["path"].split("/")[-1],
                                        "size": 0})

        if rota == "/2/files/upload_session/start":
            sid = "sess-%d" % (len(est["sessoes"]) + 1)
            est["sessoes"][sid] = bytearray(self.corpo())
            return self.responder(200, {"session_id": sid})

        if rota == "/2/files/upload_session/append_v2":
            arg = json.loads(self.headers.get("Dropbox-API-Arg", "{}"))
            cur = arg["cursor"]
            buf = est["sessoes"][cur["session_id"]]
            if cur["offset"] != len(buf):
                return self.erro(409, "incorrect_offset")
            buf.extend(self.corpo())
            return self.responder(200, {})

        if rota == "/2/files/upload_session/finish":
            arg = json.loads(self.headers.get("Dropbox-API-Arg", "{}"))
            cur, commit = arg["cursor"], arg["commit"]
            buf = est["sessoes"].pop(cur["session_id"])
            if cur["offset"] != len(buf):
                return self.erro(409, "incorrect_offset")
            est["arquivos"][commit["path"]] = (bytes(buf), commit.get("client_modified"))
            est["envios_em_sessao"] += 1
            return self.responder(200, {"name": commit["path"].split("/")[-1]})

        if rota == "/2/files/delete_v2":
            p = self.json_corpo().get("path")
            est["arquivos"].pop(p, None)
            return self.responder(200, {})

        return self.erro(404, "rota desconhecida: " + rota)


# =========================================================================
# Microsoft Graph (OneDrive)
# =========================================================================
class GraphFake(BaseFake):
    estado = None

    def _caminho(self):
        """Extrai o caminho do item da rota root:/<caminho>:/<acao>."""
        rota = urllib.parse.urlparse(self.path).path
        rota = rota[len("/me/drive"):] if rota.startswith("/me/drive") else rota
        m = re.match(r"^/root:/(.*?):?(/children|/content|/createUploadSession)?$", rota)
        if m:
            return urllib.parse.unquote(m.group(1)), (m.group(2) or "")
        m = re.match(r"^/root(/children|/content)?$", rota)
        if m:
            return "", (m.group(1) or "")
        return None, rota

    def do_GET(self):
        if not self.autorizado():
            return
        est = type(self).estado
        rota = urllib.parse.urlparse(self.path).path
        consulta = {k: v[0] for k, v in
                    urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).items()}
        if rota in ("/me/drive", "/me/drive/"):
            return self.responder(200, {"id": "drive-1", "driveType": "personal"})

        caminho, acao = self._caminho()
        if caminho is None:
            return self.erro(404, "rota: " + rota)

        if acao == "/children":
            if caminho and caminho not in est["pastas"]:
                return self.erro(404, "itemNotFound")
            filhos = []
            pref = (caminho + "/") if caminho else ""
            vistos = set()
            for p in sorted(est["pastas"]):
                if p.startswith(pref) and p != caminho:
                    nome = p[len(pref):].split("/")[0]
                    if nome and nome not in vistos:
                        vistos.add(nome)
                        filhos.append({"name": nome, "id": "f-" + p, "folder": {}})
            for p, (dados, quando) in sorted(est["arquivos"].items()):
                if p.startswith(pref) and "/" not in p[len(pref):]:
                    filhos.append({"name": p.split("/")[-1], "id": est["ids"][p],
                                   "size": len(dados),
                                   "file": {"hashes": {"quickXorHash": "xx"}},
                                   "fileSystemInfo": {"lastModifiedDateTime": quando}})
            # pagina de dois em dois, para exercitar o nextLink
            inicio = int(consulta.get("$skip", 0))
            fatia = filhos[inicio:inicio + 2]
            resp = {"value": fatia}
            if len(filhos) > inicio + 2:
                base = "http://127.0.0.1:%d%s" % (self.server.server_address[1], rota)
                resp["@odata.nextLink"] = base + "?$skip=%d" % (inicio + 2)
            return self.responder(200, resp)

        if acao == "/content":
            item = est["arquivos"].get(caminho)
            if item is None:
                return self.erro(404, "itemNotFound")
            return self.responder(200, bruto=item[0], tipo="application/octet-stream")

        return self.erro(404, "GET nao tratado: " + rota)

    def do_PUT(self):
        est = type(self).estado
        rota = urllib.parse.urlparse(self.path).path
        if rota.startswith("/upload-session/"):
            sid = rota.split("/")[-1]
            faixa = self.headers.get("Content-Range", "")
            buf = est["sessoes"][sid]["buf"]
            m = re.match(r"bytes (\d+)-(\d+)/(\d+)", faixa)
            if not m:
                return self.erro(400, "Content-Range ausente")
            inicio = int(m.group(1))
            if inicio != len(buf):
                return self.erro(416, "faixa fora de ordem")
            buf.extend(self.corpo())
            info = est["sessoes"][sid]
            if len(buf) >= int(m.group(3)):
                est["arquivos"][info["caminho"]] = (bytes(buf), info["quando"])
                est["ids"][info["caminho"]] = "id-" + info["caminho"]
                est["envios_em_sessao"] += 1
                del est["sessoes"][sid]
                return self.responder(201, {"id": "id-" + info["caminho"]})
            return self.responder(202, {"nextExpectedRanges": ["%d-" % len(buf)]})

        if not self.autorizado():
            return
        caminho, acao = self._caminho()
        if acao == "/content":
            pai = caminho.rsplit("/", 1)[0] if "/" in caminho else ""
            if pai and pai not in est["pastas"]:
                return self.erro(404, "pasta inexistente: " + pai)
            ident = "id-" + caminho
            est["arquivos"][caminho] = (self.corpo(), _agora_iso())
            est["ids"][caminho] = ident
            est["envios_simples"] += 1
            return self.responder(201, {"id": ident, "name": caminho.split("/")[-1]})
        return self.erro(404, "PUT nao tratado: " + rota)

    def do_POST(self):
        if self.talvez_token():
            return
        if not self.autorizado():
            return
        est = type(self).estado
        caminho, acao = self._caminho()

        if acao == "/children":
            corpo = self.json_corpo()
            novo = (caminho + "/" + corpo["name"]) if caminho else corpo["name"]
            if "folder" in corpo:
                if novo in est["pastas"]:
                    return self.erro(409, "nameAlreadyExists")
                est["pastas"].add(novo)
                return self.responder(201, {"id": "f-" + novo, "name": corpo["name"]})
            return self.erro(400, "so criacao de pasta")

        if acao == "/createUploadSession":
            corpo = self.json_corpo()
            fsi = (corpo.get("item") or {}).get("fileSystemInfo") or {}
            pai = caminho.rsplit("/", 1)[0] if "/" in caminho else ""
            if pai and pai not in est["pastas"]:
                return self.erro(404, "pasta inexistente: " + pai)
            sid = "s%d" % (len(est["sessoes"]) + 1)
            est["sessoes"][sid] = {"buf": bytearray(), "caminho": caminho,
                                   "quando": fsi.get("lastModifiedDateTime")}
            url = "http://127.0.0.1:%d/upload-session/%s" % (
                self.server.server_address[1], sid)
            return self.responder(200, {"uploadUrl": url})

        return self.erro(404, "POST nao tratado: " + self.path)

    def do_PATCH(self):
        if not self.autorizado():
            return
        est = type(self).estado
        rota = urllib.parse.urlparse(self.path).path
        m = re.match(r"^/me/drive/items/(.+)$", rota)
        if not m:
            return self.erro(404, "PATCH nao tratado: " + rota)
        ident = m.group(1)
        corpo = self.json_corpo()
        quando = (corpo.get("fileSystemInfo") or {}).get("lastModifiedDateTime")
        for p, i in est["ids"].items():
            if i == ident and p in est["arquivos"]:
                est["arquivos"][p] = (est["arquivos"][p][0], quando)
                est["patches"] += 1
                return self.responder(200, {"id": ident})
        return self.erro(404, "itemNotFound")

    def do_DELETE(self):
        if not self.autorizado():
            return
        est = type(self).estado
        caminho, _acao = self._caminho()
        est["arquivos"].pop(caminho, None)
        est["ids"].pop(caminho, None)
        return self.responder(204, bruto=b"")


# =========================================================================
# Google Drive
# =========================================================================
class DriveFake(BaseFake):
    estado = None
    PASTA = "application/vnd.google-apps.folder"

    def _novo_id(self):
        est = type(self).estado
        est["seq"] += 1
        return "id%d" % est["seq"]

    def do_GET(self):
        if not self.autorizado():
            return
        est = type(self).estado
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}

        if u.path == "/drive/v3/about":
            return self.responder(200, {"user": {"emailAddress": "t@exemplo.com"}})

        if u.path == "/drive/v3/files":
            m = re.search(r"'([^']+)' in parents", q.get("q", ""))
            if not m:
                return self.erro(400, "q sem parents: " + q.get("q", ""))
            pai = m.group(1)
            itens = [dict(v, id=k) for k, v in est["itens"].items()
                     if v["parent"] == pai]
            for i in itens:
                i.pop("dados", None)
                i.pop("parent", None)
            # pagina de dois em dois
            inicio = int(q.get("pageToken", 0) or 0)
            fatia = itens[inicio:inicio + 2]
            resp = {"files": fatia}
            if len(itens) > inicio + 2:
                resp["nextPageToken"] = str(inicio + 2)
            return self.responder(200, resp)

        m = re.match(r"^/drive/v3/files/([^/]+)$", u.path)
        if m and q.get("alt") == "media":
            item = est["itens"].get(m.group(1))
            if item is None:
                return self.erro(404, "notFound")
            return self.responder(200, bruto=item["dados"],
                                  tipo="application/octet-stream")
        return self.erro(404, "GET nao tratado: " + u.path)

    def _ler_multipart(self):
        tipo = self.headers.get("Content-Type", "")
        m = re.search(r"boundary=(.+)$", tipo)
        if not m:
            return None, None
        limite = ("--" + m.group(1)).encode()
        bruto = self.corpo()
        partes = [p for p in bruto.split(limite) if p.strip(b"\r\n-")]
        meta, dados = None, None
        for p in partes:
            cab, _, corpo = p.partition(b"\r\n\r\n")
            corpo = corpo.rstrip(b"\r\n")
            if b"application/json" in cab:
                meta = json.loads(corpo)
            else:
                dados = corpo
        return meta, dados

    def do_POST(self):
        if self.talvez_token():
            return
        if not self.autorizado():
            return
        est = type(self).estado
        u = urllib.parse.urlparse(self.path)

        if u.path == "/upload/drive/v3/files":
            meta, dados = self._ler_multipart()
            if meta is None:
                return self.erro(400, "multipart invalido")
            ident = self._novo_id()
            est["itens"][ident] = {"name": meta["name"], "parent": meta["parents"][0],
                                   "mimeType": "application/octet-stream",
                                   "size": str(len(dados or b"")),
                                   "modifiedTime": meta.get("modifiedTime"),
                                   "md5Checksum": "md5-%d" % len(dados or b""),
                                   "dados": dados or b""}
            est["criados"] += 1
            return self.responder(200, {"id": ident})

        if u.path == "/drive/v3/files":
            meta = self.json_corpo()
            if meta.get("mimeType") != self.PASTA:
                return self.erro(400, "so pasta por aqui")
            ident = self._novo_id()
            est["itens"][ident] = {"name": meta["name"], "parent": meta["parents"][0],
                                   "mimeType": self.PASTA, "dados": b""}
            est["pastas_criadas"] += 1
            return self.responder(200, {"id": ident})

        return self.erro(404, "POST nao tratado: " + u.path)

    def do_PATCH(self):
        if not self.autorizado():
            return
        est = type(self).estado
        u = urllib.parse.urlparse(self.path)
        m = re.match(r"^/upload/drive/v3/files/(.+)$", u.path)
        if not m:
            return self.erro(404, "PATCH nao tratado: " + u.path)
        ident = m.group(1)
        if ident not in est["itens"]:
            return self.erro(404, "notFound")
        meta, dados = self._ler_multipart()
        item = est["itens"][ident]
        item["dados"] = dados or b""
        item["size"] = str(len(item["dados"]))
        item["md5Checksum"] = "md5-%d" % len(item["dados"])
        if meta and meta.get("modifiedTime"):
            item["modifiedTime"] = meta["modifiedTime"]
        est["atualizados"] += 1
        return self.responder(200, {"id": ident})

    def do_DELETE(self):
        if not self.autorizado():
            return
        est = type(self).estado
        m = re.match(r"^/drive/v3/files/(.+)$", urllib.parse.urlparse(self.path).path)
        if m:
            est["itens"].pop(m.group(1), None)
            return self.responder(204, bruto=b"")
        return self.erro(404, "DELETE nao tratado")


# =========================================================================
def subir(handler_cls, estado):
    handler_cls.estado = estado
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def estado_base(**extra):
    d = {"token_valido": "TOKEN-BOM", "refresh_token": "REFRESH-BOM",
         "renovacoes": 0, "nao_autorizadas": 0}
    d.update(extra)
    return d
