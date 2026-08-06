# Testes

Testes de integração: em vez de simular as bibliotecas, eles sobem **serviços
de verdade** (ou emuladores oficiais) e fazem sincronizações completas contra
eles — enviar, listar, baixar, apagar no modo espelho, segunda passada sem
reenvio, paralelismo e validação final.

```
python tests/rodar_todos.py          # roda tudo e resume
python tests/teste_base.py           # ou uma suite de cada vez
```

| Suíte | O que cobre | Precisa de |
|---|---|---|
| `teste_base.py` | registro de tipos, política de comparação, paralelismo por capacidade, snapshot | nada |
| `teste_s3_webdav.py` | Amazon S3 (e por extensão o Backblaze B2) e WebDAV | `moto[s3]`, `wsgidav`, `cheroot` |
| `teste_azure_gcs.py` | Azure Blob e Google Cloud Storage | Azurite no ar, `gcp-storage-emulator` |
| `teste_azure_files.py` | Azure Files | `azure-storage-file-share` |
| `teste_oauth.py` | Authorization Code + PKCE, `state`, renovação de token | nada |
| `teste_oauth_backends.py` | Dropbox, OneDrive e Google Drive | nada |

## Preparo

```
pip install -r requirements.txt -r requirements-nuvem.txt
pip install "moto[s3]" wsgidav cheroot gcp-storage-emulator
npx azurite --skipApiVersionCheck --blobPort 10000    # em outro terminal
```

A flag `--skipApiVersionCheck` é necessária: os SDKs atuais enviam uma versão
de API mais nova do que o Azurite reconhece.

No Windows, se o `pip install` falhar com erro de caminho longo, use um
ambiente virtual num diretório curto (ex.: `C:\dev\venv`).

## Como cada serviço é exercitado

- **S3** — `moto`, que implementa a API do S3 em processo.
- **WebDAV** — `wsgidav` servindo uma pasta temporária, por HTTP real.
- **Azure Blob** — Azurite, o emulador oficial da Microsoft.
- **GCS** — `gcp-storage-emulator`.
- **Azure Files** — não existe emulador (o Azurite só faz Blob/Queue/Table).
  O teste usa `unittest.mock.create_autospec` sobre as **classes reais do
  SDK**, então uma chamada com assinatura errada quebra o teste. Foi assim que
  apareceu um `set_http_headers()` que nunca teria funcionado.
- **Dropbox, OneDrive e Google Drive** — servidores HTTP que implementam as
  rotas documentadas de cada API (paginação por cursor / `nextLink` /
  `pageToken`, envio em pedaços, criação de pastas, 401 disparando renovação
  de token). Os servidores falam HTTP/1.1 com keep-alive, como os serviços
  reais, para exercitar o reuso de conexão.

## Limite conhecido

Azure Files, Dropbox, OneDrive e Google Drive **não foram testados com
credenciais reais**. Os testes cobrem a lógica do cliente contra a API
documentada, não a fidelidade ao serviço em produção. Antes de confiar neles
para dados importantes, faça uma passada com uma pasta pequena.
