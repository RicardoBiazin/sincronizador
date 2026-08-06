# Sincronizador

Ferramenta em Python para **sincronizar pastas e arquivos** no Windows, com
interface gráfica para configurar e um modo silencioso para ser **agendado pelo
Agendador de Tarefas do Windows**.

Você descreve uma **tarefa** (de onde, para onde, em que modo), e ela roda
sozinha na hora marcada — copiando só o que mudou, guardando versão do que for
sobrescrito, conferindo o resultado no final e avisando por e-mail se algo der
errado. Origem e destino podem ser pasta local, servidor ou nuvem, em qualquer
combinação: dá para ir de uma pasta para o S3, de um FTP para o OneDrive, ou
entre duas pastas de rede.

## Para que serve

- **Backup automático** de pastas de trabalho para um HD externo, um NAS, um
  servidor ou um bucket na nuvem, todo dia no horário que você escolher.
- **Publicar arquivos** num servidor FTP/SFTP ou WebDAV sem subir tudo de novo
  a cada vez.
- **Espelhar uma pasta de rede** para a máquina local (ou o contrário), com
  cópia de segurança do que for substituído.
- **Manter duas pastas iguais nos dois sentidos**, resolvendo conflito pelo
  arquivo mais recente e guardando o perdedor.
- **Levar arquivos entre serviços diferentes** — o programa trata todo destino
  pela mesma interface, então Dropbox → S3 ou SFTP → Google Drive funciona
  igual a pasta → pasta.

Não é um cliente de sincronização contínua: ele roda quando você manda (ou
quando o agendador manda), faz o trabalho e sai. Isso o torna previsível e
fácil de auditar pelo log.

## Recursos

- **3 modos de sincronização:**
  - **Espelho** — origem → destino idêntico (apaga no destino o que não existe na origem). Ideal para backup.
  - **Incremental** — origem → destino, só adiciona/atualiza, **nunca apaga**.
  - **Bidirecional** — propaga mudanças nos dois sentidos; em conflito, vence o arquivo mais recente (o perdedor vai para o versionamento).
- **Locais suportados:**

  | Tipo | Precisa instalar | Preserva a data original |
  |---|---|---|
  | Pasta local / rede (inclui Google Drive e OneDrive montados) | — | sim |
  | FTP / FTPS (TLS) | — | se o servidor aceitar `MFMT` |
  | SFTP (SSH) | `paramiko` | sim |
  | WebDAV (Nextcloud, ownCloud, IIS...) | `requests` | não (o servidor define a data) |
  | Amazon S3 | `boto3` | opcional (1 requisição por arquivo) |
  | Backblaze B2 | `boto3` | opcional (1 requisição por arquivo) |
  | Microsoft Azure Blob Storage | `azure-storage-blob` | sim |
  | Microsoft Azure File Storage | `azure-storage-file-share` | sim |
  | Google Cloud Storage | `google-cloud-storage` | sim |
  | Dropbox | — (login OAuth) | sim |
  | Microsoft OneDrive / SharePoint | — (login OAuth) | sim |
  | Google Drive (API) | — (login OAuth) | sim |

  Os pacotes de nuvem estão em `requirements-nuvem.txt` — instale só o que for
  usar. Se faltar algum, o tipo aparece na tela com o aviso do que instalar,
  em vez de dar erro no meio da sincronização. Dropbox, OneDrive e Google Drive
  não precisam de pacote nenhum: falam HTTP direto e já funcionam no executável
  básico.
- **Interface gráfica** (Tkinter) para criar/editar tarefas e rodar manualmente.
- **Modo silencioso** para agendamento.
- **Log em arquivo** (pasta `logs\`), com rotação e limpeza automática.
- **Filtros** de inclusão/exclusão por padrão (`*.tmp`, `~$*`, etc.).
- **Notificação por e-mail** (SMTP) ao terminar — sempre ou só em caso de erro.
- **Versionamento/backup** — guarda cópia antes de sobrescrever/apagar.
- **Validação ao final** — confere se os arquivos ficaram iguais (tamanho/data e, opcionalmente, por conteúdo/hash).
- **Critério de comparação configurável** (campo "Como detectar que um arquivo mudou"):
  - `auto` (padrão) — compara por tamanho + data quando os dois lados preservam a data original; se algum lado não preserva (object storage, FTP sem `MFMT`), passa a comparar só por tamanho, evitando reenviar tudo a cada execução.
  - `data` — sempre tamanho + data (comportamento das versões anteriores).
  - `tamanho` — só o tamanho; mais rápido, menos seguro.
  - `conteudo` — confere o hash do conteúdo; pega alterações de mesmo tamanho e mesma data, ao custo de ler os dois lados.
- **Testar conexão** — botão na tela da tarefa que valida host/credenciais antes de salvar.
- **Credenciais protegidas** — senhas, chaves e tokens são gravados cifrados com a DPAPI do Windows, amarrados à sua conta de usuário. O JSON copiado para outra máquina não entrega nada.
- **Temas de cores** — Claro, Escuro, Azul, Verde, Sépia + cor de destaque configurável (botão "Aparência").
- **Progresso em tempo real** — barra de progresso com cronômetro, velocidade (MB/s) e tempo restante (ETA).
- **Cópia paralela** — sincroniza vários arquivos ao mesmo tempo (campo "Arquivos/vez", 1 a 16). Cada tipo de local declara se aguenta paralelismo: pastas locais/Drive/OneDrive sim; FTP/SFTP caem para sequencial automaticamente.
- **Instância única** — impede abrir dois programas ou rodar duas sincronizações ao mesmo tempo na máquina.
- **Parar a qualquer momento** — botão "Parar" cancela a sincronização em andamento (mantém o que já foi copiado); aviso ao fechar a janela durante uma sincronização; arquivos gravados de forma atômica (temporário `.sinctmp` + troca), limpos automaticamente se algo for interrompido.

## Nuvem: o que preencher em cada tipo

- **Amazon S3** — bucket, *access key ID*, *secret access key* e região. O
  campo "Prefixo" é a pasta dentro do bucket (pode ficar vazio).
- **Backblaze B2** — bucket, *keyID*, *applicationKey* e o **Endpoint** que
  aparece no painel do bucket (ex.: `https://s3.us-west-004.backblazeb2.com`).
  O B2 é acessado pela API compatível com S3, então não precisa de outro pacote.
- **Azure Blob / Azure Files** — o mais simples é colar a *connection string*
  da conta de armazenamento; ela já contém conta e chave. Como alternativa,
  preencha a conta e a chave (ou um token SAS).
- **Google Cloud Storage** — bucket e o arquivo JSON da conta de serviço. Se
  deixar o JSON em branco, usa a credencial padrão do ambiente
  (`GOOGLE_APPLICATION_CREDENTIALS`).
- **WebDAV** — a URL base do servidor (no Nextcloud é algo como
  `https://host/remote.php/dav/files/usuario`), usuário e senha.

Use o botão **Testar** antes de salvar: ele conecta de verdade e informa o erro
exato se algo estiver errado.

### Sobre a data dos arquivos na nuvem

S3, B2, Azure Blob e GCS não têm "data do arquivo": a listagem devolve a data
do upload. O Sincronizador grava a data original num metadado (`sincmtime`) e
a lê de volta quando dá:

- **Azure Blob e GCS** entregam metadados na própria listagem — a data volta
  sem custo e a comparação por data funciona normalmente.
- **S3 e B2** não entregam; ler a data exigiria uma requisição por arquivo.
  Por isso o campo *"Ler data original"* vem desligado, e a comparação `auto`
  passa a usar só o tamanho. Ligue se preferir precisão a velocidade.
- **Azure Files** tem data de verdade (SMB) e ela é definida no envio.

Esses serviços também informam o MD5 na listagem, então a comparação
`conteudo` fica barata do lado da nuvem — só o lado local precisa ler os
arquivos.

## Dropbox, OneDrive e Google Drive (login OAuth)

Esses três exigem autorizar a conta uma vez. O programa **não traz credencial
embutida** — ela ficaria visível dentro do executável, e os provedores não
permitem. Você cadastra um aplicativo (gratuito) na sua própria conta:

| Serviço | Onde cadastrar | O que preencher |
|---|---|---|
| Dropbox | dropbox.com/developers/apps → *Scoped access*, *Full Dropbox* | App key (e App secret, opcional) |
| OneDrive | portal.azure.com → Microsoft Entra ID → Registros de aplicativo, como **cliente público/nativo** | ID do aplicativo (e o tenant, se for conta corporativa) |
| Google Drive | console.cloud.google.com → Credenciais → OAuth, tipo **Aplicativo para computador**, com a API do Drive ativada | Client ID e Client secret |

Em todos, cadastre este endereço de redirecionamento, **exatamente assim**:

```
http://localhost:53682/
```

Depois, na tela da tarefa, clique em **Conectar…**: o navegador abre, você
autoriza, e o programa guarda um *refresh token* cifrado. A partir daí ele
renova o acesso sozinho, sem abrir o navegador de novo — até você revogar o
acesso na sua conta.

Detalhes que valem saber:

- **Google Drive** trabalha por ID de arquivo, não por caminho. O campo
  "Pasta no Drive" é resolvido para uma pasta real, criada se não existir.
  Documentos, Planilhas e Apresentações do Google são **ignorados**: não têm
  conteúdo binário para copiar (precisariam ser exportados).
- **Drives compartilhados** do Google: marque a caixa correspondente.
- **SharePoint / OneDrive corporativo**: preencha o "ID do drive" para apontar
  para uma biblioteca específica; vazio usa o OneDrive do próprio usuário.
- Arquivos grandes sobem em pedaços (sessão de upload) nos três serviços.

## Google Drive e OneDrive como pasta local

Alternativa sem cadastrar aplicativo nenhum: use os **clientes oficiais**.
- **Google Drive para Desktop** monta o Drive como unidade (ex.: `G:\Meu Drive\...`).
- **OneDrive** cria a pasta local `C:\Users\<você>\OneDrive\...`.

Para o Sincronizador esses caminhos são apenas **pastas locais** — aponte
origem/destino para eles com o tipo `local`. É mais simples; em compensação
depende do cliente estar instalado e sincronizando.

## Como as credenciais são guardadas

Senhas, chaves de acesso, connection strings e refresh tokens vão para o JSON
**cifrados com a DPAPI do Windows** (prefixo `dpapi:`). A chave é derivada da
sua conta de usuário do Windows e não fica guardada em lugar nenhum, então:

- outro usuário da mesma máquina não consegue ler;
- o arquivo copiado para outra máquina também não — ao abrir lá, o campo volta
  vazio e o programa avisa que é preciso informar a credencial de novo.

Configurações antigas, com senha em texto puro, continuam funcionando e são
cifradas na primeira vez que você salvar.

## Como gerar o executável

1. Tenha o Python 3.10+ instalado.
2. Na pasta do projeto, rode:

   ```
   build.bat            versão básica: local, FTP/FTPS, SFTP e WebDAV
   build.bat nuvem      inclui também S3, B2, Azure e Google Cloud
   ```

3. O executável sai em `dist\Sincronizador.exe`.

A versão básica tem cerca de 17 MB. A versão `nuvem` passa de 150 MB, porque
carrega os SDKs da Amazon, da Microsoft e do Google — gere-a só se for usar
esses serviços a partir do executável.

Copie `Sincronizador.exe` para uma pasta fixa (ex.: `C:\Sincronizador\`). Os
arquivos `sincronizador.config.json`, `logs\`, `backup\` e `state\` são criados
**ao lado do executável**.

## Uso

Sem argumentos abre a **interface gráfica**:

```
Sincronizador.exe
```

Modo silencioso (para agendar):

```
Sincronizador.exe --all           roda todas as tarefas ativas
Sincronizador.exe --run "Nome"    roda uma tarefa específica
Sincronizador.exe --list          lista as tarefas
Sincronizador.exe --config C:\caminho\config.json
```

Código de saída: `0` = sucesso, `1` = terminou com erros, `2` = já havia execução em andamento, `3` = tarefa não encontrada.

## Agendar no Windows

### Pela interface do Agendador de Tarefas
1. Abra **Agendador de Tarefas** (`taskschd.msc`).
2. **Criar Tarefa Básica** → dê um nome (ex.: "Sincronizar backup").
3. Escolha a frequência (diária, ao logar, etc.).
4. Ação: **Iniciar um programa**.
   - Programa: `C:\Sincronizador\Sincronizador.exe`
   - Argumentos: `--all`
   - Iniciar em: `C:\Sincronizador\`
5. Em Propriedades, marque **"Executar estando o usuário conectado ou não"** e
   **"Executar com privilégios mais altos"** se sincronizar pastas protegidas.

### Por linha de comando (PowerShell, como administrador)

```powershell
$acao    = New-ScheduledTaskAction -Execute "C:\Sincronizador\Sincronizador.exe" -Argument "--all" -WorkingDirectory "C:\Sincronizador"
$gatilho = New-ScheduledTaskTrigger -Daily -At 22:00
Register-ScheduledTask -TaskName "Sincronizar arquivos" -Action $acao -Trigger $gatilho -Description "Sincronizacao diaria de pastas"
```

Como o `.exe` é gerado com `--windowed`, **não aparece janela** ao rodar agendado;
o resultado fica no log e (se configurado) no e-mail.

## Estrutura do projeto

```
sincronizador/
  config.py      configuração (tarefas, e-mail, log) em JSON
  filters.py     filtros de inclusão/exclusão
  endpoints.py   acesso a local / FTP / SFTP + registro de tipos
  backends.py    WebDAV, S3, B2, Azure Blob, Azure Files e GCS
  backends_oauth.py  Dropbox, OneDrive e Google Drive
  oauth.py       autorizacao OAuth (PKCE + redirecionamento local)
  segredos.py    cifragem das credenciais (DPAPI do Windows)
  engine.py      motor de sincronização (3 modos + versionamento)
  notify.py      log em arquivo e e-mail
  gui.py         interface gráfica (Tkinter)
  cli.py         linha de comando / modo silencioso
app.py           ponto de entrada (alvo do PyInstaller)
build.bat        gera o Sincronizador.exe
```

## Como adicionar um novo tipo de local (WebDAV, S3, B2, Azure, GCS, Dropbox...)

Tudo o que um tipo novo precisa fica declarado em `endpoints.py`; nem a interface
nem o motor precisam ser alterados.

1. Escreva a classe herdando de `Endpoint`, implementando `scan`, `open_read`,
   `write`, `delete`, `move_to_backup` e (opcional) `probe`. Declare as
   capacidades:

   ```python
   class S3Endpoint(Endpoint):
       preserves_mtime = False   # object storage só devolve a data do upload
       parallel_safe = True      # cliente HTTP aguenta várias threads
       has_dirs = False          # não existe diretório de verdade
   ```

   `scan()` devolve `{caminho_relativo: FileInfo(size, mtime, etag)}`. Preencher
   `etag` quando o serviço fornecer um hash faz o motor reconhecer arquivos
   iguais sem depender da data.

2. Registre o tipo, declarando os campos de conexão e os pacotes necessários:

   ```python
   register(EndpointSpec(
       kind="s3", label="Amazon S3", factory=lambda path, remote: S3Endpoint(path, remote),
       path_label="Prefixo:", requires=["boto3"],
       fields=[Field("bucket", "Bucket", required=True),
               Field("user", "Access key"),
               Field("password", "Secret key", kind="password"),
               Field("region", "Região"),
               Field("endpoint_url", "Endpoint", help="use para Backblaze B2")],
   ))
   ```

O resto acontece sozinho: o tipo aparece no combo da tarefa, os campos viram
formulário (`text`, `password`, `int`, `bool`, `file`, `dir`, `oauth`), o botão
"Testar" chama `probe()`, e falta de pacote vira o aviso *"Requer: pip install
boto3"* em vez de erro em tempo de execução.

Campos cujo nome existe em `config.Remote` (`host`, `port`, `user`, `password`,
`key_file`, `tls`, `passive`) são gravados lá; qualquer outro nome vai para
`Remote.options` e é lido com `remote.opt("bucket")`. Campos `password` e
`oauth` são gravados cifrados automaticamente.

Para um serviço com login OAuth, acrescente o provedor em `oauth.PROVEDORES` e
declare `Field("refresh_token", "Conta", kind="oauth", provedor="nome")`: a
interface passa a mostrar o botão "Conectar", e `oauth.sessao_de(remote, "nome")`
entrega uma sessão que renova o token sozinha.

## Observações

- O programa **ignora automaticamente as próprias pastas de trabalho** (`logs`, `state`, `backup`) quando elas estão dentro da origem/destino — assim ele nunca sincroniza nem valida os próprios arquivos temporários.
- Se você sincronizar uma pasta de projeto, adicione aos **filtros de exclusão** o que não deve ir junto (ex.: `build`, `dist`, `__pycache__`, `.git`). Novas tarefas já excluem `__pycache__` e `.git` por padrão.
- A comparação de arquivos usa **tamanho + data de modificação** (tolerância de 2s) sempre que os dois lados preservam a data; veja o critério `auto` acima.
- O modo **bidirecional** guarda um "snapshot" em `state\` para saber o que foi
  criado/apagado desde a última execução. Não apague essa pasta.
- Para **e-mail via Gmail**, use uma *senha de app* (não a senha normal da conta).
