# Execução com Wine e log — 22/10/2026

O ponto de entrada do cliente de referência é `python3 pc/tools/client.py run`. O arquivo de diagnóstico é
`.local/logs/legend-online.log`, com rotação de 5 MiB e três backups.
Use `tail -F .local/logs/legend-online.log` para acompanhar a sessão.

O launcher mantém o Wine, o OCX e os executáveis originais. O primeiro teste
com diagnóstico abriu o cliente às 14:20 e capturou uma falha:

```text
Unhandled page fault on read access to FFFFFFFFFFFFFFFF
at address 000000014002B44F
Cliente encerrou com erro (código 5)
```

O disassembly nesse endereço contém `mov 0x10(%rax),%rdi`. O início da rotina
chama `QComboBox::model()` e depois acessa uma estrutura que contém a chave
`platform`. Isso localiza a falha na rotina que usa o modelo de seleção; não
comprova a causa nem permite considerar o bug corrigido. Nenhum patch foi
aplicado ao binário original.

Uma consulta independente ao endpoint público `http://cdn.brov.site/servermap.php`
retornou HTTP 301 e, seguindo o redirecionamento HTTPS, HTTP 426. A consulta
HTTP pelo H2Proxy também retornou 301. Essas consultas não reproduzem
necessariamente todos os detalhes da requisição do aplicativo e não provam
que a falha seja causada pelo servidor. Não alteramos a lista de servidores
nem as configurações de conta para contornar essa resposta.

A sessão seguinte, iniciada às 14:27:59, abriu o cliente às 14:28:02, obteve
HTTP 200 em ambas as sondas e teve sua janela de login confirmada por
`WM_CLASS=legendonline.exe` e captura visual. Ela permaneceu aberta durante
a verificação. Login e sessão de jogo não foram realizados pelo agente.

O launcher agora registra falhas durante a validação e execução, detecta
encerramento prematuro no teste, observa a queda do proxy que iniciou e
preserva o log entre sessões. Os sete testes automatizados passaram.
