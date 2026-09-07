# R36S Tracker

Um tracker self-hosted para organizar sua biblioteca retro a partir de um R36S com ArkOS.

O projeto conecta no R36S via SSH/SFTP, lê os `gamelist.xml` do EmulationStation, importa o catálogo de jogos e mantém um histórico pessoal separado no próprio servidor.

A ideia é simples: o R36S fornece os jogos e metadados; o tracker guarda o que você está jogando e o que já zerou.

---

## Recursos

- Importação automática dos jogos do R36S via SSH/SFTP
- Leitura de `gamelist.xml` por sistema
- Suporte a snapshots/imagens dos jogos
- Cache local das imagens no servidor
- Interface web responsiva
- Layout otimizado para desktop e mobile
- Paginação
- Busca por nome
- Filtro por sistema
- Status manual por jogo:
  - `Jogando`
  - `Zerado`
  - sem status
- Cards com destaque visual:
  - azul para `Jogando`
  - verde para `Zerado`
- Atualização de status sem recarregar a página
- Persistência em SQLite
- Retry automático de SSH
- Timeout de conexão/SFTP
- Progresso de sincronização
- Endpoint de health check
- Docker / Docker Compose

---

## Como funciona

O R36S é usado como fonte de catálogo.

O tracker lê informações como:

- nome do jogo
- sistema
- caminho da ROM
- gênero
- desenvolvedor
- publisher
- `playcount`
- `lastplayed`
- imagem / thumbnail

O estado pessoal do usuário fica salvo no SQLite do tracker.

Atualmente os estados são:

- `playing` → Jogando
- `completed` → Zerado
- `NULL` → sem status

A flag `favorite` do EmulationStation não é usada como fonte de verdade para os status do tracker.

---

## Arquitetura

```text
R36S / ArkOS
192.168.0.122
      |
      | SSH / SFTP
      v
+----------------------+
| R36S Tracker         |
|                      |
| FastAPI              |
| SQLite               |
| Snapshot Cache       |
| Web UI               |
+----------+-----------+
           |
           v
http://servidor:8150
```

---

## Requisitos

No servidor:

- Docker
- Docker Compose

No R36S:

- ArkOS
- Wi-Fi conectado
- Remote Services / SSH habilitado

O projeto usa por padrão:

```text
Host: 192.168.0.122
Porta SSH: 22
Usuário: ark
```

---

## Instalação

Clone o repositório:

```bash
git clone https://github.com/SEU-USUARIO/r36s-tracker.git
cd r36s-tracker
```

Crie o arquivo `.env`:

```bash
cp .env.example .env
```

Edite o `.env`:

```env
R36S_HOST=192.168.0.122
R36S_PORT=22
R36S_USER=ark
R36S_PASSWORD=ark
SYNC_INTERVAL_SECONDS=300
SSH_RETRIES=3
```

Ajuste a senha se necessário.

Depois suba o projeto:

```bash
docker compose up -d --build
```

---

## Porta

O `docker-compose.yml` usa:

```yaml
ports:
  - "8150:8080"
```

Então a interface fica disponível em:

```text
http://IP-DO-SERVIDOR:8150
```

Exemplo:

```text
http://192.168.0.10:8150
```

---

## Persistência

Os dados ficam na pasta:

```text
data/
```

Estrutura típica:

```text
data/
├── r36s.db
└── media/
    ├── snes/
    ├── megadrive/
    ├── psx/
    ├── n64/
    └── ...
```

O SQLite fica em:

```text
data/r36s.db
```

As imagens cacheadas ficam em:

```text
data/media/
```

Não apague essa pasta se quiser preservar seus status e snapshots.

---

## Sincronização

A sincronização roda automaticamente de acordo com:

```env
SYNC_INTERVAL_SECONDS=300
```

Também existe o botão:

```text
Sincronizar agora
```

A aplicação tenta reconectar ao R36S em caso de instabilidade de Wi-Fi.

---

## Health check

Endpoint:

```text
/health
```

Exemplo:

```bash
curl http://localhost:8150/health
```

Resposta típica:

```json
{
  "ok": true,
  "r36s_host": "192.168.0.122",
  "sync_running": true,
  "phase": "baixando snapshots do catálogo",
  "progress": {
    "current": 1600,
    "total": 1963
  },
  "last_ok": null,
  "last_error": null
}
```

---

## Snapshots

O tracker tenta usar os campos do `gamelist.xml`:

```xml
<image>...</image>
```

ou:

```xml
<thumbnail>...</thumbnail>
```

As imagens são copiadas do R36S para o servidor e ficam em cache local.

Isso permite que os snapshots continuem aparecendo mesmo com o R36S desligado.

---

## Status dos jogos

Cada jogo pode ser marcado manualmente como:

### Jogando

```text
🎮 Jogando
```

### Zerado

```text
🏁 Zerado
```

### Sem status

Use o botão:

```text
Limpar
```

Os status são independentes do `favorite` do R36S.

---

## Interface

A interface possui:

- busca
- paginação
- filtros
- seleção por sistema
- cards com snapshot
- destaque visual por status

No desktop, os cards são exibidos em grid.

<img width="1919" height="864" alt="image" src="https://github.com/user-attachments/assets/3bb79f6e-1413-4b3b-884e-c5b71df4b3d1" />

No mobile, o layout muda para uma coluna.

---

## Mobile

O layout mobile foi pensado para funcionar bem em iPhone e dispositivos de largura semelhante.

Inclui:

- 1 card por linha
- botões maiores
- altura mínima de toque de 44 px
- busca em largura total
- paginação amigável
- stats em grid
- suporte a safe area
- `viewport-fit=cover`

---

## Atualizando o projeto

Antes de atualizar:

```bash
cp data/r36s.db data/r36s.db.bak
```

Preserve sempre:

```text
.env
data/
```

Depois:

```bash
docker compose down
docker compose up -d --build
```

---

## Logs

Para acompanhar:

```bash
docker logs -f r36s-tracker
```

Ou:

```bash
docker compose logs -f r36s-tracker
```

---

## Estrutura do projeto

```text
r36s-tracker/
├── app/
│   ├── __init__.py
│   └── main.py
├── data/
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
└── README.md
```

---

## Tecnologias

- Python
- FastAPI
- SQLite
- Paramiko
- Uvicorn
- Docker
- Docker Compose
- HTML / CSS / JavaScript

---

## Observações

O R36S precisa estar:

- ligado
- conectado ao Wi-Fi
- com SSH / Remote Services habilitado

Se o dispositivo estiver desligado, a interface continua funcionando com os dados já sincronizados e as imagens em cache.

---

## Roadmap

Algumas ideias para versões futuras:

- status adicionais
- notas pessoais
- avaliação por estrelas
- data em que o jogo foi zerado
- tempo total jogado
- dashboard de estatísticas
- exportação CSV/JSON
- integração com RetroAchievements
- edição manual de metadados
- modo PWA / adicionar à tela inicial
- autenticação
- múltiplos dispositivos
- múltiplos usuários
- backup automático do banco

---

## Licença

Escolha a licença que fizer mais sentido para o projeto.

Exemplos comuns:

- MIT
- Apache-2.0
- GPL-3.0

Se o objetivo for deixar o projeto simples e permissivo, MIT é uma boa opção.

---

## Aviso

Este projeto é independente e não possui afiliação oficial com R36S, ArkOS ou EmulationStation.

Use apenas ROMs, BIOS e conteúdos que você tenha direito de utilizar.
