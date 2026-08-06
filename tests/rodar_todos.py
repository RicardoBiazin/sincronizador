"""Roda todas as suites e resume o resultado.

    python tests/rodar_todos.py

Cada suite imprime uma linha por verificacao. Veja tests/README.md para o que
cada uma precisa ter no ar (emuladores).
"""
import os
import subprocess
import sys

AQUI = os.path.dirname(os.path.abspath(__file__))

SUITES = [
    ("teste_base.py", "registro de tipos, comparacao, paralelismo"),
    ("teste_s3_webdav.py", "S3 (moto) e WebDAV (wsgidav)"),
    ("teste_azure_gcs.py", "Azure Blob (Azurite) e GCS (emulador)"),
    ("teste_azure_files.py", "Azure Files (dubles do SDK real)"),
    ("teste_oauth.py", "fluxo OAuth com PKCE"),
    ("teste_oauth_backends.py", "Dropbox, OneDrive e Google Drive"),
]


def main():
    total_ok = total_falhas = 0
    quebradas = []
    for arquivo, descricao in SUITES:
        caminho = os.path.join(AQUI, arquivo)
        proc = subprocess.run([sys.executable, "-u", caminho],
                              capture_output=True, text=True, errors="replace")
        saida = proc.stdout + proc.stderr
        ok = saida.count("\n  OK   ")
        falhas = saida.count("\n  FALHA")
        total_ok += ok
        total_falhas += falhas
        estado = "ok" if proc.returncode == 0 else "PROBLEMA"
        print("%-26s %3d ok  %d falhas  [%s]  %s"
              % (arquivo, ok, falhas, estado, descricao))
        if proc.returncode != 0:
            quebradas.append((arquivo, saida))

    print("-" * 72)
    print("TOTAL: %d verificacoes ok, %d falhas" % (total_ok, total_falhas))
    for arquivo, saida in quebradas:
        print("\n===== saida de %s =====" % arquivo)
        print(saida[-3000:])
    return 1 if (total_falhas or quebradas) else 0


if __name__ == "__main__":
    sys.exit(main())
