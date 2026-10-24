# Análise do cliente Brov — 22/10/2026

Atualização de escopo: manter o Wine como parte da solução. O roteiro de port
abaixo é o estudo inicial, não uma tarefa de remover Wine. A execução atual e
o log unificado estão documentados no README.

## Escopo e método

Análise estática de PE, imports, recursos de versão, manifesto COM, strings
ASCII/UTF-16LE e referências no disassembly x86-64. Teste dinâmico limitado à
inicialização do proxy e do cliente. Não houve recuperação de código-fonte,
decompilação completa, patch de executáveis ou análise de credenciais.

Instalador localizado: `~/Downloads/LOH2_MinGW.20251228_x64.exe`.
Instalação: `~/Games/legend-online/drive_c/Program Files/Legend Online Client by Brov (H2_x64)`.
Wine: `/usr/bin/wine`. Launcher preexistente: `~/.local/bin/legend-online`.
Esse launcher já iniciava o proxy manualmente e registrava que o mecanismo
original baseado em PsExec não funcionava sob Wine.

## Inventário confirmado

| Componente | Evidência | Papel |
| --- | --- | --- |
| `LegendOnline.exe` | PE AMD64, 1.826.816 bytes, versão 5.0.0.20251228 | Aplicativo C++/Qt, integração com Flash e login |
| `Qt5Core.dll` | Versão 5.15.2.0; Qt Widgets/Network/Concurrent também importados | Estrutura do aplicativo |
| `flash/Flash64_15_0_0_167.ocx` | PE AMD64, 23.445.680 bytes, versão 15,0,0,167 | Adobe Flash Player ActiveX |
| `Adobe.Flash.Control.manifest` | Assembly 15.0.0.167 apontando para o OCX local | Associação COM privada do Flash |
| `H2Proxy.exe` | PE AMD64, Qt Network e WebSockets | Rede, cache, reverse proxy; há indícios de ponte TCP/WebSocket |
| `WebView2Loader.dll` | Loader presente; nome e função de criação referenciados no EXE | Integração com navegador WebView2 |
| `psexec.exe` | Presente junto ao cliente | Auxiliar do fluxo de inicialização original |

Não há QtWebKit/QtWebEngine nas dependências importadas pelo cliente analisado.
A existência de WebView2 não implica que Flash rode dentro dele: o manifesto
e as chamadas analisadas apontam para um controle ActiveX separado.

SHA-256 dos arquivos principais:

```text
instalador      9b98a5cb4e614b64dac1d70b72a98aa519a7cd859c7a7c7fede12ffce59ec94a
LegendOnline    d92385ba2ad40268e013faaf74578197700506e52dcf991e6aa4bc2a23c5b27d
H2Proxy         9d54f3dec8f50cb60c33ff42d10441e0ed26c9a0da7da398cca73d5ee94ebd4e
Flash OCX       7ac444d19ad9d7c8a26a1fe09a7052fb7c6c922ce6c4ee38798a963df42e38ec
```

Esses hashes identificam a cópia estudada; não são uma verificação de
autenticidade contra hashes publicados pelo fornecedor.

## Como o cliente reutiliza Flash

O manifesto do EXE declara dependência de `Adobe.Flash.Control` 15.0.0.167.
O manifesto externo resolve o CLSID
`{D27CDB6E-AE6D-11cf-96B8-444553540000}` para o OCX distribuído.
O cliente contém infraestrutura de QAxContainer e bindings de eventos ActiveX.

| Interface recuperada | Offset de arquivo no EXE | Evidência / limite |
| --- | --- | --- |
| CLSID ShockwaveFlash | `0xbd7f8` | Coincide com o manifesto |
| `FlashCall(QString)` | `0xbd987` | Evento conectado a `on_axWidget_FlashCall` |
| `FSCommand(QString,QString)` | `0xbd9c0` | Evento conectado a `on_axWidget_FSCommand` |
| `AllowScriptAccess` | `0xbda0f` | Referência em VA `0x140019e2f`; valor `always` construído antes |
| `FlashVars` | `0xbda5a` | Referência em VA `0x140019ed3`; valor convertido de QByteArray |
| `LoadMovie(int, QString)` | `0xbda72` | Referência em VA `0x14001a03a`; chamada em `0x14001a05a` |
| `CallFunction(QString)` | `0xbdb32` | Referência em VA `0x14001a4ec`; chamada em `0x14001a50c` |

Os dois setters usam a entrada de importação `0x1401abd98`, resolvida como
`QObject::setProperty(char const*, QVariant const&)`. `LoadMovie` e
`CallFunction` chegam à mesma rotina interna em `0x1400758a0`; a identificação
precisa dessa rotina exige análise adicional. As strings de assinatura são
compatíveis com a API dinâmica de ActiveQt.

Há ainda propriedades `Scale`/`ShowAll`, `Quality`, `WMode`,
`AllowFullScreen` e `AllowFullScreenInteractive`. Não atribuímos valores
padrão a todas elas sem verificar os argumentos e configurações.

O cliente contém um modelo XML `<invoke name="%1" returntype="xml">` com
`<arguments>` e argumentos `<string>`, associado à chamada `CallFunction`.
Isso indica uma interface bidirecional: chamadas do host para o SWF e eventos
do SWF para o host. O conjunto completo de nomes e semântica das funções
ainda precisa ser recuperado. Os metadados Qt preservam `initFlash`,
`movie`, `flashVars`, `startGame` e `callFlashFunction`.

## Proxy e recursos

| Rota encontrada | Offset em H2Proxy | Situação |
| --- | --- | --- |
| `/polipo/` | `0x3e2eb` | HTTP 200, corpo vazio no teste local |
| `/crossdomain.xml` | `0x3e273` | HTTP 200, XML de política Flash com 111 bytes no teste local |
| `/addRevProxyMapping/` | `0x3e308` | String `POST` adjacente; esquema do corpo ainda não validado |
| `/revproxy/` | `0x3e330` | Reescrita/mapeamento de recursos inferido pelas strings |
| `/getResServer/` | `0x3e5a3` | Consulta usada pelo EXE; parâmetros e resposta não validados |
| `/known-hosts/tsv/` | `0x3e576` | Estatísticas: host, latência, contadores, velocidade e download |

O cliente referencia `127.0.0.1:8125` e `localhost:8125`. Há também a string
`localhost:8124`; seu papel não foi confirmado. A presença do nome `/polipo/`
não demonstra que o executável seja o projeto Polipo.

O proxy importa `QNetworkDiskCache`, `QNetworkAccessManager` e `QWebSocket`.
Referências incluem `C:/cache/H2Proxy`, `RedirectCache.dat`, ETag,
Last-Modified e HTTP 304. Isso torna inadequado substituí-lo por um servidor
HTTP genérico sem estudar cache, reescrita de URLs e transporte do jogo.
As strings `WebSocketProxy` e os imports de mensagens binárias indicam uma
ponte de transporte; o enquadramento dos pacotes ainda não foi analisado.

Não foram encontrados arquivos com extensão `.swf` no prefixo durante a
inspeção. Há referências a `Loading.swf` e `/client/Loading.swf` no EXE,
além de URLs de provedores e listas de servidores. Ausência dessa extensão
não exclui recursos comprimidos ou cache com nomes opacos. Não foi encontrado
um backend de jogo local: executar o cliente localmente não equivale a ter
um servidor Legend Online offline.

## Resultado da execução

O comando `python3 tools/client.py run --smoke-seconds 15` iniciou o proxy,
validou as duas rotas locais acima e iniciou o cliente na sessão gráfica.
O processo do cliente permaneceu ativo por 15 segundos; ambos os processos
iniciados pelo teste foram encerrados pelo launcher. O teste retornou código 0.
Os logs capturados estavam vazios com `WINEDEBUG=-all`.

Isso comprova inicialização de processos e resposta do proxy. Não comprova
renderização correta da janela, instanciação efetiva do Flash durante uma
sessão, funcionamento de WebView2, login, download de SWF ou jogabilidade.

## Caminho para Linux preservando o comportamento

1. Usar o cliente original executável como referência e capturar uma sessão
   válida: URL do movie, FlashVars, respostas do proxy e eventos da ponte.
   Tratar tokens/cookies como dados locais, sem adicioná-los ao repositório.
2. Especificar o contrato real de `/addRevProxyMapping/`, `/getResServer/`,
   cache e transporte, com exemplos locais sanitizados e testes de equivalência.
3. Implementar rede/cache em processo Linux nativo e comparar com o original.
   Manter um host Windows pequeno no Wine para o OCX, com IPC explícito para
   carregar movie/FlashVars e encaminhar chamadas/eventos. Esse host ainda
   não foi implementado: por enquanto executamos o cliente inteiro original.
4. Para eliminar Wine, avaliar um runtime Linux contra os SWFs recuperados.
   Será preciso substituir ActiveX e validar ActionScript, ExternalInterface,
   sockets, renderização, fontes e desempenho. Nenhuma alternativa foi
   declarada compatível sem esses testes.

A aplicação pode ganhar componentes nativos progressivamente, mas o mesmo
OCX Windows não vira uma biblioteca ELF por recompilação do launcher. O código
fonte do cliente também não está presente nesta instalação.

## Fontes externas consultadas

- [Brov: execução em Linux/macOS, 29/01/2025](https://brov.site/2025/01/jak-uruchomic-klienta-na-systemach-linux-i-macos/): o autor descreve execução pelo Wine e uma correção de diretório para a versão H2. O diretório `.LegendOnline` já existe neste prefixo.
- [Qt: ActiveX e COM](https://doc.qt.io/qt-6/activeqt-container.html): explica QAxWidget/QAxContainer e a interface de propriedades, métodos e eventos. É documentação conceitual; a versão instalada foi determinada pelos próprios binários.
- [Brov: problemas de login, 15/07/2026](https://brov.site/2026/07/naprawiamy-logowanie-do-legend-online/): o autor relata alterações de proteção nos servidores afetando autenticação. Isso pode afetar a futura validação online; não foi diagnosticado na sessão desta máquina.
