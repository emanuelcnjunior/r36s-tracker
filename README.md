# R36S Tracker v9 Mobile

Versão com passe responsivo dedicado para celular, pensando principalmente em iPhone.

## Melhorias mobile

- layout em 1 coluna para os jogos
- busca em largura total
- botão "Sincronizar agora" em largura total
- botões com altura mínima de toque de 44 px
- status Jogando / Zerado em botões grandes
- Limpar ocupa a linha inteira
- stats em grid 2xN
- paginação mais amigável ao toque
- filtros e sistemas viram chips compactos
- margens menores
- suporte a safe areas de iPhone
- `viewport-fit=cover`

## Breakpoints

- até 700 px: layout mobile
- até 390 px: compactação extra para telas menores

## Atualização

Preserve:

- `.env`
- `data/`

Faça backup:

```bash
cp data/r36s.db data/r36s.db.bak
```

Depois substitua os arquivos da aplicação e rode:

```bash
docker compose down
docker compose up -d --build
```

A porta continua `8150:8080`.
