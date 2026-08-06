"""Fluxo OAuth completo contra um servidor de autorizacao falso.

O "navegador" eh o requests: recebe a URL de consentimento, confere PKCE e
state, e chama de volta o servidor local do Sincronizador. Testa de ponta a
ponta o que eh nosso: PKCE, state, captura do code, troca por token, renovacao
e a Sessao (validade, renovacao antecipada, rotacao de refresh token).
"""
import base64, hashlib, json, sys, threading, time, urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, r"c:\DEV\sincronizador")
import requests
from sincronizador import oauth

falhas = []


def check(cond, msg):
    print(("  OK   " if cond else "  FALHA") + " " + msg)
    if not cond:
        falhas.append(msg)


# --- servidor de autorizacao falso -----------------------------------------
estado_srv = {"codes": {}, "refresh": {}, "chamadas": [], "rotacionar": False,
              "expires_in": 3600}


class AuthHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        corpo = {k: v[0] for k, v in
                 urllib.parse.parse_qs(self.rfile.read(n).decode()).items()}
        estado_srv["chamadas"].append(corpo)
        tipo = corpo.get("grant_type")

        if tipo == "authorization_code":
            info = estado_srv["codes"].get(corpo.get("code"))
            if not info:
                return self._json(400, {"error": "invalid_grant"})
            # confere o PKCE de verdade
            v = corpo.get("code_verifier", "")
            calc = base64.urlsafe_b64encode(
                hashlib.sha256(v.encode()).digest()).decode().rstrip("=")
            if calc != info["challenge"]:
                return self._json(400, {"error": "invalid_grant",
                                        "error_description": "PKCE nao confere"})
            if corpo.get("redirect_uri") != info["redirect_uri"]:
                return self._json(400, {"error": "redirect_uri_mismatch"})
            rt = "REFRESH-1"
            estado_srv["refresh"][rt] = True
            return self._json(200, {"access_token": "ACESSO-1", "refresh_token": rt,
                                    "expires_in": estado_srv["expires_in"]})

        if tipo == "refresh_token":
            rt = corpo.get("refresh_token")
            if rt not in estado_srv["refresh"]:
                return self._json(400, {"error": "invalid_grant"})
            resp = {"access_token": "ACESSO-%d" % (len(estado_srv["chamadas"])),
                    "expires_in": estado_srv["expires_in"]}
            if estado_srv["rotacionar"]:
                novo = rt + "+"
                estado_srv["refresh"][novo] = True
                resp["refresh_token"] = novo
            return self._json(200, resp)

        return self._json(400, {"error": "unsupported_grant_type"})

    def _json(self, cod, obj):
        b = json.dumps(obj).encode()
        self.send_response(cod)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):
        pass


srv = HTTPServer(("127.0.0.1", 0), AuthHandler)
porta_auth = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
TOKEN_URL = "http://127.0.0.1:%d/token" % porta_auth

# provedor de teste registrado no modulo
oauth.PROVEDORES["falso"] = oauth.Provedor(
    nome="falso", rotulo="Provedor de teste",
    auth_url="http://127.0.0.1:%d/authorize" % porta_auth,
    token_url=TOKEN_URL, scope="tudo",
    extra_auth={"access_type": "offline"})

print("\n=== Fluxo de autorizacao (PKCE + loopback) ===")

capturado = {}


def navegador_falso(url):
    """Faz o papel do navegador: valida a URL e chama o redirecionamento."""
    p = urllib.parse.urlparse(url)
    q = {k: v[0] for k, v in urllib.parse.parse_qs(p.query).items()}
    capturado.update(q)
    estado_srv["codes"]["CODE-XYZ"] = {"challenge": q["code_challenge"],
                                       "redirect_uri": q["redirect_uri"]}

    def bater():
        time.sleep(0.15)
        requests.get(q["redirect_uri"] + "?code=CODE-XYZ&state=" + q["state"],
                     timeout=10)
    threading.Thread(target=bater, daemon=True).start()


t = oauth.autorizar("falso", client_id="CID", porta=0, timeout=20,
                    abrir_navegador=navegador_falso)

check(capturado.get("code_challenge_method") == "S256", "usa PKCE S256")
check(len(capturado.get("code_challenge", "")) >= 43, "code_challenge no tamanho certo")
check(capturado.get("access_type") == "offline", "extra_auth do provedor vai na URL")
check(capturado.get("scope") == "tudo", "escopo enviado")
check(capturado.get("redirect_uri", "").startswith("http://localhost:"),
      f"redirect no loopback: {capturado.get('redirect_uri')}")
check(t["refresh_token"] == "REFRESH-1", "refresh token recebido")
check(t["access_token"] == "ACESSO-1", "access token recebido")
check(t["expires_at"] > time.time() + 3000, "validade calculada")

print("\n=== Protecoes ===")
# state diferente deve ser recusado
def navegador_state_ruim(url):
    q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(url).query).items()}
    estado_srv["codes"]["CODE-2"] = {"challenge": q["code_challenge"],
                                     "redirect_uri": q["redirect_uri"]}
    threading.Thread(target=lambda: (time.sleep(0.15), requests.get(
        q["redirect_uri"] + "?code=CODE-2&state=FORJADO", timeout=10)), daemon=True).start()

try:
    oauth.autorizar("falso", "CID", porta=0, timeout=20, abrir_navegador=navegador_state_ruim)
    ok = False
except RuntimeError as e:
    ok = "state" in str(e).lower()
check(ok, "recusa resposta com state forjado")

# usuario nega
def navegador_nega(url):
    q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(url).query).items()}
    threading.Thread(target=lambda: (time.sleep(0.15), requests.get(
        q["redirect_uri"] + "?error=access_denied&error_description=Usuario+negou",
        timeout=10)), daemon=True).start()

try:
    oauth.autorizar("falso", "CID", porta=0, timeout=20, abrir_navegador=navegador_nega)
    ok = False
except RuntimeError as e:
    ok = "negada" in str(e).lower()
check(ok, "reporta autorizacao negada pelo usuario")

# tempo esgotado
try:
    oauth.autorizar("falso", "CID", porta=0, timeout=1.0, abrir_navegador=lambda u: None)
    ok = False
except RuntimeError as e:
    ok = "tempo" in str(e).lower()
check(ok, "reporta tempo esgotado")

try:
    oauth.autorizar("falso", "", porta=0, abrir_navegador=lambda u: None); ok = False
except ValueError:
    ok = True
check(ok, "exige client_id")

try:
    oauth.autorizar("google", "CID", porta=0, abrir_navegador=lambda u: None); ok = False
except ValueError as e:
    ok = "secret" in str(e).lower()
check(ok, "Google exige client_secret")

print("\n=== Sessao (renovacao automatica) ===")
s = oauth.Sessao("falso", "CID", "REFRESH-1")
a1 = s.token()
check(a1.startswith("ACESSO-"), f"primeiro token obtido ({a1})")
antes = len(estado_srv["chamadas"])
a2 = s.token()
check(a2 == a1 and len(estado_srv["chamadas"]) == antes,
      "token valido eh reaproveitado, sem nova chamada")

s._expira = time.time() + 10      # dentro da folga de expiracao
a3 = s.token()
check(a3 != a1, f"renova antes de expirar ({a1} -> {a3})")

a4 = s.token(forcar=True)
check(a4 != a3, "forcar=True renova (usado quando o servico devolve 401)")

estado_srv["rotacionar"] = True
s2 = oauth.Sessao("falso", "CID", "REFRESH-1")
s2.token(); antigo = s2.refresh_token
s2.token(forcar=True)
check(s2.refresh_token != "REFRESH-1" and s2.refresh_token.startswith("REFRESH-1"),
      f"acompanha rotacao do refresh token ({s2.refresh_token})")
estado_srv["rotacionar"] = False

try:
    oauth.Sessao("falso", "CID", "").token(); ok = False
except ValueError:
    ok = True
check(ok, "sem refresh token avisa que a conta nao esta conectada")

try:
    oauth.Sessao("falso", "CID", "NAO-EXISTE").token(); ok = False
except RuntimeError as e:
    ok = "HTTP 400" in str(e)
check(ok, "refresh token invalido vira erro explicativo")

print("\n=== Provedores reais declarados ===")
for nome in ("dropbox", "microsoft", "google"):
    p = oauth.provedor(nome)
    check(p.auth_url.startswith("https://") and p.token_url.startswith("https://"),
          f"{nome}: URLs https")
check("offline" in oauth.provedor("dropbox").extra_auth.get("token_access_type", ""),
      "Dropbox pede acesso offline")
check("offline_access" in oauth.provedor("microsoft").scope,
      "Microsoft pede offline_access")
check(oauth.provedor("google").extra_auth.get("prompt") == "consent",
      "Google forca consent (senao nao reenvia refresh token)")
check("{tenant}" in oauth.provedor("microsoft").token_url, "Microsoft parametriza o tenant")
check(oauth.redirect_uri(53682) == "http://localhost:53682/", "redirect_uri previsivel")

srv.shutdown()
print("\n" + ("OAUTH: TODOS OS TESTES PASSARAM" if not falhas else "FALHAS: %d" % len(falhas)))
for f in falhas:
    print("  - " + f)
sys.exit(1 if falhas else 0)
