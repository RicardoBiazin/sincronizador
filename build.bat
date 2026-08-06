@echo off
REM ============================================================
REM  Gera o executavel Sincronizador.exe (pasta dist\)
REM
REM    build.bat          -> basico: local, FTP/FTPS, SFTP e WebDAV
REM    build.bat nuvem    -> inclui tambem S3, B2, Azure Blob/Files e GCS
REM                          (o executavel passa de 150 MB)
REM ============================================================
setlocal

set EXTRAS=
if /i "%~1"=="nuvem" (
  echo Modo NUVEM: instalando tambem os SDKs de S3/Azure/Google...
  python -m pip install -r requirements-nuvem.txt
  set EXTRAS=--collect-all boto3 --collect-all botocore --collect-all azure.storage.blob --collect-all azure.storage.fileshare --collect-all google.cloud.storage
) else (
  echo Modo BASICO: local, FTP/FTPS, SFTP e WebDAV.
  echo Use "build.bat nuvem" para incluir S3, Azure e Google Cloud.
  REM os SDKs sao importados sob demanda, mas o PyInstaller enxerga esses
  REM imports e os empacotaria so por estarem instalados na maquina
  set EXTRAS=--exclude-module boto3 --exclude-module botocore --exclude-module s3transfer --exclude-module azure --exclude-module google
)

echo.
echo Instalando dependencias...
python -m pip install -r requirements.txt

echo.
echo Gerando o executavel...
python -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --onefile ^
  --windowed ^
  --name Sincronizador ^
  --hidden-import paramiko ^
  --collect-submodules paramiko ^
  %EXTRAS% ^
  app.py

echo.
if exist dist\Sincronizador.exe (
  echo OK! Executavel gerado em: dist\Sincronizador.exe
) else (
  echo ERRO: o executavel nao foi gerado. Veja as mensagens acima.
)
endlocal
pause
