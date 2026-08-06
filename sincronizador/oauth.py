"""Autorizacao OAuth 2.0 para Dropbox, OneDrive e Google Drive.

Fluxo Authorization Code com PKCE e redirecionamento para o proprio
computador (loopback), que eh o recomendado para aplicativos de desktop:

  1. o programa sobe um servidor HTTP em 127.0.0.1:<porta>;
  2. abre o navegador na tela de consentimento do provedor;
  3. o provedor devolve um "code" para esse endereco;
  4. o code eh trocado por um refresh token, que fica gravado (cifrado, ver
     segredos.py) na configuracao da tarefa.

Dai em diante o programa renova sozinho o token de acesso, sem abrir o
navegador de novo - ate que o usuario revogue o acesso no provedor.

O client_id (e, no Google, o client_secret) vem do cadastro do aplicativo que
cada usuario faz no console do provedor. Nao ha credencial embutida aqui: seria
publica no executavel, e os provedores nao permitem.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Dict, Optional

logger = logging.getLogger("sincronizador")

#: porta padrao do redirecionamento. Fixa de proposito: o endereco precisa
#: ser cadastrado no console do provedor, e porta aleatoria nao daria.
PORTA_PADRAO = 53682

#: margem para renovar o token antes de ele expirar de fato
FOLGA_EXPIRACAO = 120.0


@dataclass
class Provedor:
    nome: str
    rotulo: str
    auth_url: str
    token_url: str
    scope: str
    #: parametros extras na URL de autorizacao (pedir acesso offline etc.)
    extra_auth: Dict[str, str] = field(default_factory=dict)
    #: o provedor exige client_secret mesmo em aplicativo de desktop?
    usa_segredo: bool = False
    ajuda: str = ""


PROVEDORES: Dict[str, Provedor] = {
    "dropbox": Provedor(
        nome="dropbox",
        rotulo="Dropbox",
        auth_url="https://www.dropbox.com/oauth2/authorize",
        token_url="https://api.dropboxapi.com/oauth2/token",
        scope=("files.metadata.read files.metadata.write "
               "files.content.read files.content.write"),
        # sem isto o Dropbox devolve so um token de acesso de curta duracao
        extra_auth={"token_access_type": "offline"},
        ajuda="Crie um app em dropbox.com/developers/apps (tipo 'Scoped access', "
              "'Full Dropbox') e cadastre o endereco de redirecionamento.",
    ),
    "microsoft": Provedor(
        nome="microsoft",
        rotulo="Microsoft OneDrive",
        auth_url="https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize",
        token_url="https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        scope="offline_access Files.ReadWrite.All User.Read",
        ajuda="Registre um aplicativo no portal do Azure (Microsoft Entra ID) "
              "como cliente publico/nativo e cadastre o redirecionamento.",
    ),
    "google": Provedor(
        nome="google",
        rotulo="Google Drive",
        auth_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        scope="https://www.googleapis.com/auth/drive",
        # sem access_type=offline + prompt=consent o Google nao manda o
        # refresh token na segunda autorizacao em diante
        extra_auth={"access_type": "offline", "prompt": "consent"},
        usa_segredo=True,
        ajuda="No Google Cloud Console crie uma credencial OAuth do tipo "
              "'Aplicativo para computador' e ative a API do Drive.",
    ),
}


def provedor(nome: str) -> Provedor:
    try:
        return PROVEDORES[nome]
    except KeyError:
        raise ValueError("Provedor OAuth desconhecido: %r" % nome)


def redirect_uri(porta: int = PORTA_PADRAO) -> str:
    """Endereco que precisa estar cadastrado no console do provedor."""
    return "http://localhost:%d/" % porta


# ---------------------------------------------------------------------------
# PKCE
# ---------------------------------------------------------------------------
def _pkce():
    verificador = base64.urlsafe_b64encode(os.urandom(64)).decode("ascii").rstrip("=")
    desafio = base64.urlsafe_b64encode(
        hashlib.sha256(verificador.encode("ascii")).digest()).decode("ascii").rstrip("=")
    return verificador, desafio


# ---------------------------------------------------------------------------
# Servidor que recebe o redirecionamento
# ---------------------------------------------------------------------------
_PAGINA = """<!doctype html><html lang="pt-br"><meta charset="utf-8">
<title>Sincronizador</title>
<body style="font-family:Segoe UI,Arial,sans-serif;text-align:center;padding:60px">
<h2>%s</h2><p>%s</p><p style="color:#666">Pode fechar esta aba.</p></body></html>"""


class _Receptor:
    """Servidor HTTP de vida curta que captura o 'code' do redirecionamento."""

    def __init__(self, porta: int):
        from http.server import BaseHTTPRequestHandler, HTTPServer
        self.resultado = {}
        self.evento = threading.Event()
        pai = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                consulta = urllib.parse.urlparse(self.path).query
                dados = {k: v[0] for k, v in urllib.parse.parse_qs(consulta).items()}
                if "code" in dados or "error" in dados:
                    pai.resultado = dados
                    pai.evento.set()
                if "error" in dados:
                    corpo = _PAGINA % ("Autorizacao negada",
                                       dados.get("error_description", dados["error"]))
                elif "code" in dados:
                    corpo = _PAGINA % ("Tudo certo!",
                                       "O Sincronizador ja recebeu a autorizacao.")
                else:
                    corpo = _PAGINA % ("Aguardando...", "Nada recebido nesta chamada.")
                dados_bytes = corpo.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(dados_bytes)))
                self.end_headers()
                self.wfile.write(dados_bytes)

            def log_message(self, *a):
                pass      # nao poluir o log do programa

        self.servidor = HTTPServer(("127.0.0.1", porta), Handler)
        self.porta = self.servidor.server_address[1]

    def __enter__(self):
        threading.Thread(target=self.servidor.serve_forever, daemon=True).start()
        return self

    def esperar(self, timeout: float) -> dict:
        self.evento.wait(timeout)
        return self.resultado

    def __exit__(self, *exc):
        try:
            self.servidor.shutdown()
            self.servidor.server_close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Fluxo completo
# ---------------------------------------------------------------------------
def autorizar(nome_provedor: str, client_id: str, client_secret: str = "",
              tenant: str = "common", porta: int = PORTA_PADRAO,
              timeout: float = 300.0, abrir_navegador=None) -> dict:
    """Abre o navegador, espera o consentimento e devolve os tokens.

    Retorna {'refresh_token', 'access_token', 'expires_at'}. Levanta
    RuntimeError com o motivo se o usuario negar ou o tempo esgotar.
    """
    import requests

    prov = provedor(nome_provedor)
    if not client_id:
        raise ValueError("Informe o Client ID do aplicativo.")
    if prov.usa_segredo and not client_secret:
        raise ValueError("%s exige tambem o Client Secret." % prov.rotulo)

    verificador, desafio = _pkce()
    estado = secrets.token_urlsafe(24)

    with _Receptor(porta) as receptor:
        destino = redirect_uri(receptor.porta)
        params = {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": destino,
            "scope": prov.scope,
            "state": estado,
            "code_challenge": desafio,
            "code_challenge_method": "S256",
        }
        params.update(prov.extra_auth)
        url = prov.auth_url.format(tenant=tenant or "common") + "?" + \
            urllib.parse.urlencode(params)

        if abrir_navegador is None:
            import webbrowser
            abrir_navegador = webbrowser.open
        abrir_navegador(url)

        dados = receptor.esperar(timeout)

    if not dados:
        raise RuntimeError("Tempo esgotado esperando a autorizacao no navegador.")
    if "error" in dados:
        raise RuntimeError("Autorizacao negada: %s"
                           % dados.get("error_description", dados["error"]))
    if dados.get("state") != estado:
        # protege contra uma resposta forjada chegando no servidor local
        raise RuntimeError("Resposta de autorizacao com 'state' invalido.")

    corpo = {
        "grant_type": "authorization_code",
        "code": dados["code"],
        "redirect_uri": destino,
        "client_id": client_id,
        "code_verifier": verificador,
    }
    if client_secret:
        corpo["client_secret"] = client_secret

    resp = requests.post(prov.token_url.format(tenant=tenant or "common"),
                         data=corpo, timeout=60)
    tokens = _ler_tokens(resp)
    if not tokens.get("refresh_token"):
        raise RuntimeError(
            "O provedor nao devolveu refresh token. Revogue o acesso do "
            "aplicativo na sua conta e autorize de novo.")
    return tokens


def renovar(nome_provedor: str, client_id: str, refresh_token: str,
            client_secret: str = "", tenant: str = "common") -> dict:
    """Troca o refresh token por um token de acesso novo."""
    import requests

    prov = provedor(nome_provedor)
    corpo = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }
    if client_secret:
        corpo["client_secret"] = client_secret
    resp = requests.post(prov.token_url.format(tenant=tenant or "common"),
                         data=corpo, timeout=60)
    return _ler_tokens(resp)


def _ler_tokens(resp) -> dict:
    if resp.status_code >= 400:
        detalhe = resp.text[:300]
        raise RuntimeError("Falha ao obter o token (HTTP %d): %s"
                           % (resp.status_code, detalhe))
    dados = resp.json()
    validade = float(dados.get("expires_in", 3600) or 3600)
    return {
        "access_token": dados.get("access_token", ""),
        "refresh_token": dados.get("refresh_token", ""),
        "expires_at": time.time() + validade,
    }


# ---------------------------------------------------------------------------
# Sessao usada pelos endpoints
# ---------------------------------------------------------------------------
class Sessao:
    """Mantem um token de acesso valido a partir do refresh token.

    Renova sozinha quando o token esta perto de vencer e tambem quando o
    servico responde 401 - que acontece se o token for revogado do outro lado.
    """

    def __init__(self, nome_provedor: str, client_id: str, refresh_token: str,
                 client_secret: str = "", tenant: str = "common"):
        if not refresh_token:
            raise ValueError("Conta nao conectada: use o botao Conectar.")
        self.provedor = nome_provedor
        self.client_id = client_id
        self.client_secret = client_secret
        self.tenant = tenant or "common"
        self.refresh_token = refresh_token
        self._token = ""
        self._expira = 0.0
        self._lock = threading.Lock()

    def token(self, forcar: bool = False) -> str:
        with self._lock:
            if forcar or not self._token or time.time() >= self._expira - FOLGA_EXPIRACAO:
                dados = renovar(self.provedor, self.client_id, self.refresh_token,
                                self.client_secret, self.tenant)
                self._token = dados["access_token"]
                self._expira = dados["expires_at"]
                if dados.get("refresh_token"):
                    # alguns provedores rotacionam o refresh token
                    self.refresh_token = dados["refresh_token"]
            return self._token

    def cabecalho(self, forcar: bool = False) -> dict:
        return {"Authorization": "Bearer " + self.token(forcar)}


def sessao_de(remote, nome_provedor: str) -> Sessao:
    """Monta a Sessao a partir dos campos de um config.Remote."""
    return Sessao(
        nome_provedor,
        client_id=str(remote.opt("client_id", "")),
        refresh_token=str(remote.opt("refresh_token", "")),
        client_secret=str(remote.opt("client_secret", "")),
        tenant=str(remote.opt("tenant", "")) or "common",
    )
