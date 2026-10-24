# Fontes no prefixo Wine — 22/10/2026

## Sintoma

No chat, o nome do jogador e o canal (`[ Guilda ]`, `[ Mensagem ]`) aparecem
normalmente, mas o texto das mensagens vira uma sequência de quadrados.

## Causa

O chat (`chat.view.ChatOutputViewII`, no módulo de código `2.png`) monta cada
linha com dois tipos de texto:

- nomes, canais e links (`ChatCellData.createLinkTxt`) usam um `TextField`
  comum. O Flash pede a fonte ao GDI e o Wine substitui `Arial` por
  Liberation Sans, então esse texto aparece;
- o corpo da mensagem (`ChatFormat.createGeneralCellData`) é um `TextElement`
  do Flash Text Engine com `FontDescription("Arial")`. O Flash Text Engine
  procura o nome gravado no próprio arquivo de fonte e ignora as substituições
  do Wine. Sem uma Arial instalada, ele cai em uma fonte sem letras latinas.

Um SWF de teste com o Flash Text Engine, executado no `flashhost` com o mesmo
prefixo e o mesmo OCX, confirmou o comportamento:
`FontDescription.isDeviceFontCompatible("Arial")` devolve `false`, e o texto
em Arial, SimSun e `_sans` aparece como quadrados. Tahoma e Liberation Sans
funcionam, e um `TextField` em Arial também. O módulo `3.png` usa Verdana pelo
mesmo motor.

O defeito não vem do cliente: o prefixo era criado sem nenhuma fonte em
`drive_c/windows/Fonts`. O cliente do Brov usa o mesmo OCX e é afetado da
mesma forma.

## Correção

Uma vez por prefixo:

```bash
WINEPREFIX=~/Games/legend-online winetricks -q corefonts
```

Esse comando instala Arial, Verdana, Times New Roman e as outras fontes básicas
da Microsoft em `drive_c/windows/Fonts`. Feche e abra o jogo de novo depois da
instalação. Renomear o arquivo da Liberation Sans ou criar uma entrada em
`FontSubstitutes` não resolve, porque o Flash Text Engine lê o nome gravado
dentro do arquivo.

Ao abrir o jogo, `legendclient` verifica se `arial.ttf` e `verdana.ttf` estão
no prefixo e, se faltarem, registra um aviso em `.local/logs/legendclient.log`.
