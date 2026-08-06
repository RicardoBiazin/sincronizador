"""Ponto de entrada do Sincronizador.

Sem argumentos abre a interface grafica; com argumentos roda em modo
silencioso (ver sincronizador/cli.py). Este arquivo eh o alvo do PyInstaller.
"""
import sys

from sincronizador.cli import main

if __name__ == "__main__":
    sys.exit(main())
