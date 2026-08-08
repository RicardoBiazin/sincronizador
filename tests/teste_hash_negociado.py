"""Negociacao do algoritmo de hash entre dois endpoints.

Por que este teste existe: a validacao final afirma "o destino e' igual a
origem". Com MD5, cujas colisoes sao construiveis, um destino hostil poderia
satisfazer essa afirmacao com conteudo trocado. Passamos a usar sha256 quando
nos mesmos lemos os bytes dos dois lados -- e a manter MD5 quando um dos lados
so' oferece MD5 pronto (object storage), porque trocar o algoritmo ali forcaria
baixar o arquivo inteiro a cada validacao.

O que NAO pode acontecer em nenhum caso: comparar hashes de algoritmos
diferentes, que daria "diferente" para arquivos iguais e recopiaria tudo.
"""
import hashlib
import io
import sys

sys.path.insert(0, r"c:\DEV\sincronizador")
from sincronizador import endpoints as ep, engine

falhas = []


def checa(ok, desc):
    print(f"  {'OK   ' if ok else 'FALHA'} {desc}")
    if not ok:
        falhas.append(desc)


class Fake(ep.Endpoint):
    """Endpoint minimo: guarda bytes e, opcionalmente, informa MD5 pronto."""

    def __init__(self, dados: dict, informa_md5: bool):
        self.dados = dados
        self.informa_md5 = informa_md5
        self.leituras = 0

    def open_read(self, rel):
        self.leituras += 1
        return io.BytesIO(self.dados[rel])

    def content_hash(self, rel, info=None):
        if not self.informa_md5:
            return ""
        return hashlib.md5(self.dados[rel]).hexdigest()


CONTEUDO = b"conteudo de teste " * 100
OUTRO = b"conteudo DIFERENTE " * 100

print("\n[1] nenhum lado informa hash -> sha256 nos dois")
a = Fake({"x": CONTEUDO}, informa_md5=False)
b = Fake({"x": CONTEUDO}, informa_md5=False)
ha, hb, alg = engine._hashes_para_comparar(a, b, "x")
checa(alg == "sha256", f"algoritmo escolhido: {alg}")
checa(ha == hb, "arquivos iguais batem")
checa(ha == hashlib.sha256(CONTEUDO).hexdigest(), "o valor e' sha256 de verdade")
checa(a.leituras == 1 and b.leituras == 1, "leu uma vez de cada lado")

print("\n[2] um lado informa MD5 pronto -> MD5 nos dois (sem download extra)")
a = Fake({"x": CONTEUDO}, informa_md5=True)
b = Fake({"x": CONTEUDO}, informa_md5=False)
ha, hb, alg = engine._hashes_para_comparar(a, b, "x")
checa(alg == "md5", f"algoritmo escolhido: {alg}")
checa(ha == hb, "arquivos iguais batem")
checa(ha == hashlib.md5(CONTEUDO).hexdigest(), "o valor e' md5")
checa(a.leituras == 0, "o lado que informou o hash NAO foi baixado")
checa(b.leituras == 1, "o outro lado foi lido uma vez")

print("\n[3] os dois informam MD5 -> nenhum download")
a = Fake({"x": CONTEUDO}, informa_md5=True)
b = Fake({"x": CONTEUDO}, informa_md5=True)
ha, hb, alg = engine._hashes_para_comparar(a, b, "x")
checa(alg == "md5" and ha == hb, "compara pelos hashes informados")
checa(a.leituras == 0 and b.leituras == 0, "nenhum lado foi baixado")

print("\n[4] conteudo diferente e' detectado nos dois modos")
for informa, esperado in ((False, "sha256"), (True, "md5")):
    a = Fake({"x": CONTEUDO}, informa_md5=informa)
    b = Fake({"x": OUTRO}, informa_md5=informa)
    ha, hb, alg = engine._hashes_para_comparar(a, b, "x")
    checa(alg == esperado and ha != hb, f"{alg}: diferenca detectada")

print("\n[5] nunca compara algoritmos diferentes")
# O caso perigoso: um lado com MD5 pronto e o outro calculado. Se o calculado
# saisse em sha256, arquivos IGUAIS pareceriam diferentes e o programa
# recopiaria a pasta inteira em toda execucao.
a = Fake({"x": CONTEUDO}, informa_md5=True)
b = Fake({"x": CONTEUDO}, informa_md5=False)
ha, hb, _ = engine._hashes_para_comparar(a, b, "x")
checa(len(ha) == len(hb), f"mesmo comprimento de digest ({len(ha)} vs {len(hb)})")
checa(ha == hb, "arquivos iguais NAO parecem diferentes")

print()
if falhas:
    print(f"{len(falhas)} FALHA(S)")
    sys.exit(1)
print("TODOS OS TESTES PASSARAM")
