# Telegram Server Monitor Bot

Bot simples em Python para consultar, via Telegram, se um servidor Linux aparenta estar em uso na **GPU** e/ou na **CPU**.

## O que ele faz

Comandos disponíveis:

- `/start` — mostra as rotas disponíveis
- `/status` — coleta algumas amostras e responde com um resumo de GPU e CPU
- `/enable_aow` — ativa alerta de inicialização para o chat atual
- `/disable_aow` — remove alerta de inicialização para o chat atual

## Como o bot decide se há uso

### GPU

O bot executa `nvidia-smi` algumas vezes (por padrão, **5 amostras** com **1 segundo** entre elas) e observa:

- uso médio e pico da GPU
- uso médio e pico de VRAM
- processos de compute encontrados pelo `nvidia-smi`

Com base nisso, ele devolve um veredito:

- **sem indício forte de uso**
- **possível uso**
- **forte indício de uso**

### CPU

O bot coleta algumas amostras com `psutil` e observa:

- uso médio e pico da CPU
- uso médio de RAM
- `load average` por núcleo
- top processos por CPU

Como o sistema operacional sempre usa um pouco da CPU, os limiares foram deixados **mais elásticos** do que na GPU.

> Observação: isso é uma **heurística**, não uma auditoria exata. A ideia é indicar se existe **forte chance** de alguém estar usando a máquina.

---

## Estrutura do projeto

```text
telegram-server-monitor/
├── bot.py
├── requirements.txt
├── .env.example
├── server-monitor-bot.service.example
└── README.md
```

---

## Requisitos

- Linux
- Python 3.10+
- `nvidia-smi` disponível no PATH, caso o servidor tenha GPU NVIDIA
- token de bot do Telegram

Se não houver GPU NVIDIA, o bot continuará funcionando, mas a seção da GPU será marcada como indisponível.

---

## 1) Criar o bot no Telegram

1. Abra o **@BotFather** no Telegram.
2. Rode `/newbot`.
3. Escolha nome e username do bot.
4. Copie o token gerado.

---

## 2) Subir o projeto no servidor

Exemplo usando `/opt/telegram-server-monitor`:

```bash
sudo mkdir -p /opt/telegram-server-monitor
sudo chown $USER:$USER /opt/telegram-server-monitor
cd /opt/telegram-server-monitor
```

Copie os arquivos do projeto para esse diretório.

---

## 3) Criar ambiente virtual e instalar dependências

```bash
cd /opt/telegram-server-monitor
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

---

## 4) Configurar o `.env`

Crie o arquivo `.env` a partir do exemplo:

```bash
cp .env.example .env
```

Edite o arquivo:

```env
TELEGRAM_BOT_TOKEN=coloque_aqui_o_token_do_bot
SERVER_NAME=servidor-gpu-01
AOW_SUBSCRIBERS_FILE=./aow_subscribers.txt
STATUS_SAMPLES=5
STATUS_INTERVAL_SECONDS=1
BOOT_ALERT_ON_START=true
LOG_LEVEL=INFO
```

### Variáveis

- `TELEGRAM_BOT_TOKEN`: token do bot no Telegram
- `SERVER_NAME`: nome amigável do servidor, usado nas respostas
- `AOW_SUBSCRIBERS_FILE`: arquivo TXT com os chats inscritos no alerta de boot
- `STATUS_SAMPLES`: quantidade de amostras em `/status`
- `STATUS_INTERVAL_SECONDS`: intervalo entre amostras
- `BOOT_ALERT_ON_START`: se `true`, envia alerta quando o serviço iniciar
- `LOG_LEVEL`: nível de log

---

## 5) Testar manualmente

Antes de configurar o boot automático, rode manualmente:

```bash
cd /opt/telegram-server-monitor
source .venv/bin/activate
python bot.py
```

Agora, no Telegram:

1. abra o bot
2. envie `/start`
3. envie `/status`
4. envie `/enable_aow` para cadastrar esse chat no alerta de inicialização

O arquivo `aow_subscribers.txt` será criado automaticamente.

### Formato do TXT

Cada linha fica assim:

```text
chat_id|rotulo
```

Exemplo:

```text
123456789|@seuusuario
987654321|Joao Silva
```

Na remoção com `/disable_aow`, o bot remove a linha do `chat_id` atual.

---

## 6) Fazer o bot iniciar junto com o servidor (systemd)

### 6.1 Copiar o arquivo de serviço

```bash
sudo cp server-monitor-bot.service.example /etc/systemd/system/server-monitor-bot.service
```

### 6.2 Editar os caminhos e usuário

Abra o arquivo:

```bash
sudo nano /etc/systemd/system/server-monitor-bot.service
```

Revise especialmente estas linhas:

```ini
User=ubuntu
Group=ubuntu
WorkingDirectory=/opt/telegram-server-monitor
ExecStart=/opt/telegram-server-monitor/.venv/bin/python /opt/telegram-server-monitor/bot.py
```

Troque `ubuntu` pelo usuário correto do servidor, se necessário.

### 6.3 Recarregar o systemd

```bash
sudo systemctl daemon-reload
```

### 6.4 Habilitar no boot

```bash
sudo systemctl enable server-monitor-bot.service
```

### 6.5 Iniciar agora

```bash
sudo systemctl start server-monitor-bot.service
```

### 6.6 Verificar status

```bash
sudo systemctl status server-monitor-bot.service
```

### 6.7 Ver logs

```bash
journalctl -u server-monitor-bot.service -f
```

---

## Como funciona o alerta de boot (`AOW`)

Quando o serviço sobe, o bot lê o arquivo `aow_subscribers.txt` e envia uma mensagem para todos os chats inscritos.

Na prática, isso cobre bem o caso de:

- servidor acabou de ligar
- servidor reiniciou
- serviço do bot subiu junto com o sistema

### Importante

Como a lógica é propositalmente simples e não usa banco de dados:

- o alerta é disparado quando o **serviço inicia**
- se você reiniciar manualmente o serviço, o alerta também será enviado

Se você quiser diferenciar “boot real do sistema” de “restart manual do serviço”, aí já valeria acrescentar alguma lógica extra de estado.

---

## Exemplo de resposta do `/status`

```text
servidor-gpu-01
Leitura concluída em 2026-04-01 10:15:00

GPU
• Veredito: forte indício de uso
• Média de uso: 42.0%
• Pico de uso: 78.0%
• VRAM média: 36.5%
• Pico de VRAM: 8120/24564 MiB
• Leitura: o nvidia-smi encontrou processo(s) de compute associado(s) à GPU
• Processos de GPU:
  - PID 12345 | python | 7800 MiB

CPU
• Veredito: possível uso
• Média de uso: 19.5%
• Pico de uso: 35.2%
• RAM média: 47.0%
• Load/core: 0.41
• Leitura: houve atividade acima do nível esperado para apenas o sistema operacional
• Top processos:
  - PID 12345 | python | CPU 28.0% | RAM 950.3 MiB
```

---

## Publicar no GitHub

Dentro da pasta do projeto:

```bash
git init
git add .
git commit -m "Initial commit"
```

Se quiser, adicione também um `.gitignore` com pelo menos:

```gitignore
.venv/
.env
aow_subscribers.txt
__pycache__/
*.pyc
```

---

## Melhorias futuras possíveis

- comando para mostrar mais processos de GPU/CPU
- ajuste fino dos limiares por tipo de servidor
- suporte a múltiplas GPUs com regras por placa
- alerta periódico, não só no boot
- whitelist de usuários autorizados

---

## Licença

Use como quiser e adapte ao seu ambiente.
