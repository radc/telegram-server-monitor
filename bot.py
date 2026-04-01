from __future__ import annotations

import asyncio
import logging
import os
from concurrent.futures import ThreadPoolExecutor
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import psutil
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes


load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
SERVER_NAME = os.getenv("SERVER_NAME", "Servidor").strip() or "Servidor"
AOW_FILE = Path(os.getenv("AOW_SUBSCRIBERS_FILE", BASE_DIR / "aow_subscribers.txt"))
STATUS_SAMPLES = max(1, int(os.getenv("STATUS_SAMPLES", "5")))
STATUS_INTERVAL_SECONDS = max(0.2, float(os.getenv("STATUS_INTERVAL_SECONDS", "1")))
BOOT_ALERT_ON_START = os.getenv("BOOT_ALERT_ON_START", "true").strip().lower() in {"1", "true", "yes", "y", "on"}

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
)
logger = logging.getLogger("server-monitor-bot")

subscribers_lock = threading.Lock()


@dataclass
class ProcessInfo:
    pid: int
    name: str
    cpu_percent: float = 0.0
    memory_mb: float = 0.0
    gpu_memory_mb: Optional[int] = None


@dataclass
class GPUReport:
    available: bool
    verdict: str
    reason: str
    avg_util_percent: float = 0.0
    peak_util_percent: float = 0.0
    avg_vram_percent: float = 0.0
    peak_vram_used_mb: int = 0
    total_vram_mb: int = 0
    process_count: int = 0
    active_processes: List[ProcessInfo] = field(default_factory=list)
    per_gpu_lines: List[str] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class CPUReport:
    verdict: str
    reason: str
    avg_cpu_percent: float
    peak_cpu_percent: float
    avg_ram_percent: float
    load_per_core: Optional[float]
    top_processes: List[ProcessInfo] = field(default_factory=list)


def read_subscribers() -> Dict[int, str]:
    AOW_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not AOW_FILE.exists():
        AOW_FILE.touch()
        return {}

    subscribers: Dict[int, str] = {}
    with subscribers_lock:
        for raw_line in AOW_FILE.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if "|" in line:
                chat_id_str, label = line.split("|", 1)
            else:
                chat_id_str, label = line, ""
            try:
                subscribers[int(chat_id_str)] = label.strip()
            except ValueError:
                logger.warning("Linha inválida no arquivo de inscritos ignorada: %s", line)
    return subscribers



def write_subscribers(subscribers: Dict[int, str]) -> None:
    lines = [f"{chat_id}|{label}" if label else str(chat_id) for chat_id, label in sorted(subscribers.items())]
    with subscribers_lock:
        AOW_FILE.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")



def sanitize_label(label: str) -> str:
    return label.replace("\n", " ").replace("\r", " ").replace("|", "-").strip()



def get_user_label(update: Update) -> str:
    user = update.effective_user
    if user is None:
        return ""
    if user.username:
        return f"@{user.username}"
    full_name = " ".join(part for part in [user.first_name, user.last_name] if part).strip()
    return sanitize_label(full_name)



def run_command(command: List[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, check=False)



def parse_nvidia_gpu_line(line: str) -> Optional[dict]:
    parts = [part.strip() for part in line.split(",")]
    if len(parts) != 4:
        return None
    try:
        return {
            "index": int(parts[0]),
            "util": float(parts[1]),
            "mem_used": int(float(parts[2])),
            "mem_total": int(float(parts[3])),
        }
    except ValueError:
        return None



def parse_compute_app_line(line: str) -> Optional[ProcessInfo]:
    parts = [part.strip() for part in line.split(",")]
    if len(parts) != 3:
        return None
    try:
        return ProcessInfo(
            pid=int(parts[0]),
            name=parts[1],
            gpu_memory_mb=int(float(parts[2])),
        )
    except ValueError:
        return None



def sample_gpu(samples: int, interval_seconds: float) -> GPUReport:
    if not shutil_which("nvidia-smi"):
        return GPUReport(
            available=False,
            verdict="indisponível",
            reason="nvidia-smi não encontrado neste servidor.",
            error="nvidia-smi não encontrado",
        )

    per_gpu_history: Dict[int, dict] = {}
    aggregate_utils: List[float] = []
    aggregate_vram_pct: List[float] = []
    aggregate_used_mb: List[int] = []
    total_vram_mb = 0

    command = [
        "nvidia-smi",
        "--query-gpu=index,utilization.gpu,memory.used,memory.total",
        "--format=csv,noheader,nounits",
    ]

    for sample_idx in range(samples):
        result = run_command(command)
        if result.returncode != 0:
            error = result.stderr.strip() or result.stdout.strip() or "Falha ao executar nvidia-smi"
            return GPUReport(
                available=False,
                verdict="indisponível",
                reason="Não foi possível consultar a GPU via nvidia-smi.",
                error=error,
            )

        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        gpu_rows = [parse_nvidia_gpu_line(line) for line in lines]
        gpu_rows = [row for row in gpu_rows if row is not None]
        if not gpu_rows:
            return GPUReport(
                available=False,
                verdict="indisponível",
                reason="Nenhuma GPU NVIDIA detectada pelo nvidia-smi.",
                error="Sem GPUs no resultado do nvidia-smi",
            )

        sample_utils: List[float] = []
        sample_used = 0
        sample_total = 0
        for row in gpu_rows:
            idx = row["index"]
            per_gpu_history.setdefault(idx, {"util": [], "mem_used": [], "mem_total": row["mem_total"]})
            per_gpu_history[idx]["util"].append(row["util"])
            per_gpu_history[idx]["mem_used"].append(row["mem_used"])
            per_gpu_history[idx]["mem_total"] = row["mem_total"]
            sample_utils.append(row["util"])
            sample_used += row["mem_used"]
            sample_total += row["mem_total"]

        total_vram_mb = sample_total
        aggregate_utils.append(sum(sample_utils) / len(sample_utils))
        aggregate_used_mb.append(sample_used)
        aggregate_vram_pct.append((sample_used / sample_total) * 100 if sample_total else 0.0)

        if sample_idx < samples - 1:
            time.sleep(interval_seconds)

    compute_processes = query_gpu_processes()

    avg_util = mean_or_zero(aggregate_utils)
    peak_util = max(aggregate_utils, default=0.0)
    avg_vram_pct = mean_or_zero(aggregate_vram_pct)
    peak_vram_used = max(aggregate_used_mb, default=0)

    possible_conditions = [
        avg_util >= 7,
        peak_util >= 15,
        avg_vram_pct >= 8,
        peak_vram_used >= max(512, int(total_vram_mb * 0.06)) if total_vram_mb else False,
    ]
    strong_conditions = [
        avg_util >= 15,
        peak_util >= 35,
        avg_vram_pct >= 15,
        peak_vram_used >= max(1024, int(total_vram_mb * 0.12)) if total_vram_mb else False,
    ]

    if compute_processes:
        verdict = "forte indício de uso"
        reason = "o nvidia-smi encontrou processo(s) de compute associado(s) à GPU"
    elif sum(strong_conditions) >= 2:
        verdict = "forte indício de uso"
        reason = "utilização e/ou VRAM ficaram acima dos limiares fortes"
    elif any(possible_conditions):
        verdict = "possível uso"
        reason = "houve alguma atividade de GPU/VRAM acima do nível esperado para ocioso"
    else:
        verdict = "sem indício forte de uso"
        reason = "utilização média e VRAM ficaram em faixa baixa"

    per_gpu_lines = []
    for idx in sorted(per_gpu_history):
        item = per_gpu_history[idx]
        util_avg = mean_or_zero(item["util"])
        mem_avg = mean_or_zero(item["mem_used"])
        total_mem = item["mem_total"]
        mem_pct = (mem_avg / total_mem) * 100 if total_mem else 0.0
        per_gpu_lines.append(
            f"GPU {idx}: util média {util_avg:.1f}% | VRAM média {mem_avg:.0f}/{total_mem} MiB ({mem_pct:.1f}%)"
        )

    return GPUReport(
        available=True,
        verdict=verdict,
        reason=reason,
        avg_util_percent=avg_util,
        peak_util_percent=peak_util,
        avg_vram_percent=avg_vram_pct,
        peak_vram_used_mb=peak_vram_used,
        total_vram_mb=total_vram_mb,
        process_count=len(compute_processes),
        active_processes=compute_processes,
        per_gpu_lines=per_gpu_lines,
    )



def query_gpu_processes() -> List[ProcessInfo]:
    command = [
        "nvidia-smi",
        "--query-compute-apps=pid,process_name,used_memory",
        "--format=csv,noheader,nounits",
    ]
    result = run_command(command)
    if result.returncode != 0:
        return []

    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        return []
    if len(lines) == 1 and "No running processes found" in lines[0]:
        return []

    processes: List[ProcessInfo] = []
    for line in lines:
        proc = parse_compute_app_line(line)
        if proc is not None:
            processes.append(proc)
    return processes



def sample_cpu(samples: int, interval_seconds: float) -> CPUReport:
    cpu_samples: List[float] = []
    ram_samples: List[float] = []

    for _ in range(samples):
        cpu_samples.append(psutil.cpu_percent(interval=interval_seconds))
        ram_samples.append(psutil.virtual_memory().percent)

    avg_cpu = mean_or_zero(cpu_samples)
    peak_cpu = max(cpu_samples, default=0.0)
    avg_ram = mean_or_zero(ram_samples)

    try:
        load1, _, _ = os.getloadavg()
        cpu_count = psutil.cpu_count() or 1
        load_per_core = load1 / cpu_count
    except (AttributeError, OSError):
        load_per_core = None

    top_processes = get_top_cpu_processes()
    top_process_cpu = top_processes[0].cpu_percent if top_processes else 0.0

    possible_conditions = [
        avg_cpu >= 12,
        peak_cpu >= 25,
        (load_per_core or 0.0) >= 0.35,
        top_process_cpu >= 15,
        avg_ram >= 75,
    ]
    strong_conditions = [
        avg_cpu >= 25,
        peak_cpu >= 50,
        (load_per_core or 0.0) >= 0.70,
        top_process_cpu >= 35,
    ]

    if sum(strong_conditions) >= 2 or (sum(strong_conditions) >= 1 and sum(possible_conditions) >= 3):
        verdict = "forte indício de uso"
        reason = "CPU/load/processos ficaram acima dos limiares fortes"
    elif any(possible_conditions):
        verdict = "possível uso"
        reason = "houve atividade acima do nível esperado para apenas o sistema operacional"
    else:
        verdict = "sem indício forte de uso"
        reason = "CPU, RAM e carga ficaram em faixa baixa"

    return CPUReport(
        verdict=verdict,
        reason=reason,
        avg_cpu_percent=avg_cpu,
        peak_cpu_percent=peak_cpu,
        avg_ram_percent=avg_ram,
        load_per_core=load_per_core,
        top_processes=top_processes,
    )



def get_top_cpu_processes(limit: int = 5, probe_seconds: float = 0.4) -> List[ProcessInfo]:
    processes: List[psutil.Process] = []

    for proc in psutil.process_iter(attrs=["pid", "name"]):
        try:
            proc.cpu_percent(None)
            processes.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    time.sleep(probe_seconds)

    top_items: List[ProcessInfo] = []
    for proc in processes:
        try:
            cpu = proc.cpu_percent(None)
            if cpu <= 0.5:
                continue
            mem_mb = proc.memory_info().rss / (1024 * 1024)
            name = proc.info.get("name") or f"pid-{proc.pid}"
            top_items.append(ProcessInfo(pid=proc.pid, name=name, cpu_percent=cpu, memory_mb=mem_mb))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    top_items.sort(key=lambda item: item.cpu_percent, reverse=True)
    return top_items[:limit]



def mean_or_zero(values: List[float]) -> float:
    return sum(values) / len(values) if values else 0.0



def shutil_which(command: str) -> Optional[str]:
    for directory in os.getenv("PATH", "").split(os.pathsep):
        candidate = Path(directory) / command
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None



def format_gpu_section(report: GPUReport) -> str:
    if not report.available:
        return (
            "<b>GPU</b>\n"
            f"• Status: {report.verdict}\n"
            f"• Motivo: {escape_html(report.reason)}"
        )

    lines = [
        "<b>GPU</b>",
        f"• Veredito: <b>{escape_html(report.verdict)}</b>",
        f"• Média de uso: {report.avg_util_percent:.1f}%",
        f"• Pico de uso: {report.peak_util_percent:.1f}%",
        f"• VRAM média: {report.avg_vram_percent:.1f}%",
        f"• Pico de VRAM: {report.peak_vram_used_mb}/{report.total_vram_mb} MiB" if report.total_vram_mb else f"• Pico de VRAM: {report.peak_vram_used_mb} MiB",
        f"• Leitura: {escape_html(report.reason)}",
    ]
    for per_gpu_line in report.per_gpu_lines:
        lines.append(f"• {escape_html(per_gpu_line)}")
    if report.active_processes:
        lines.append("• Processos de GPU:")
        for proc in report.active_processes[:5]:
            lines.append(
                f"  - PID {proc.pid} | {escape_html(proc.name)} | {proc.gpu_memory_mb or 0} MiB"
            )
    return "\n".join(lines)



def format_cpu_section(report: CPUReport) -> str:
    lines = [
        "<b>CPU</b>",
        f"• Veredito: <b>{escape_html(report.verdict)}</b>",
        f"• Média de uso: {report.avg_cpu_percent:.1f}%",
        f"• Pico de uso: {report.peak_cpu_percent:.1f}%",
        f"• RAM média: {report.avg_ram_percent:.1f}%",
        f"• Load/core: {report.load_per_core:.2f}" if report.load_per_core is not None else "• Load/core: indisponível",
        f"• Leitura: {escape_html(report.reason)}",
    ]
    if report.top_processes:
        lines.append("• Top processos:")
        for proc in report.top_processes[:5]:
            lines.append(
                f"  - PID {proc.pid} | {escape_html(proc.name)} | CPU {proc.cpu_percent:.1f}% | RAM {proc.memory_mb:.1f} MiB"
            )
    return "\n".join(lines)



def escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )



def collect_status() -> str:
    with ThreadPoolExecutor(max_workers=2) as executor:
        gpu_future = executor.submit(sample_gpu, STATUS_SAMPLES, STATUS_INTERVAL_SECONDS)
        cpu_future = executor.submit(sample_cpu, STATUS_SAMPLES, STATUS_INTERVAL_SECONDS)
        gpu_report = gpu_future.result()
        cpu_report = cpu_future.result()

    now_str = time.strftime("%Y-%m-%d %H:%M:%S")
    parts = [
        f"<b>{escape_html(SERVER_NAME)}</b>",
        f"<i>Leitura concluída em {now_str}</i>",
        "",
        format_gpu_section(gpu_report),
        "",
        format_cpu_section(cpu_report),
        "",
        "<i>Heurística simples: serve para indicar provável atividade, não como auditoria exata.</i>",
    ]
    return "\n".join(parts)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "Rotas disponíveis:\n"
        "/status - verifica GPU e CPU\n"
        "/enable_aow - alerta ao iniciar\n"
        "/disable_aow - remove alerta"
    )
    await update.message.reply_text(text)


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = await update.message.reply_text("Coletando amostras por alguns segundos...")
    status_text = await asyncio.to_thread(collect_status)
    await message.edit_text(status_text, parse_mode=ParseMode.HTML)


async def enable_aow_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None:
        return

    subscribers = read_subscribers()
    label = get_user_label(update)
    subscribers[chat.id] = label
    write_subscribers(subscribers)

    await update.message.reply_text(
        f"Alerta de inicialização ativado para este chat em {SERVER_NAME}."
    )


async def disable_aow_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None:
        return

    subscribers = read_subscribers()
    removed = subscribers.pop(chat.id, None)
    write_subscribers(subscribers)

    if removed is None:
        await update.message.reply_text("Este chat não estava inscrito no alerta de inicialização.")
    else:
        await update.message.reply_text("Alerta de inicialização removido deste chat.")


async def notify_boot(application: Application) -> None:
    if not BOOT_ALERT_ON_START:
        logger.info("Envio de alerta de boot desativado por configuração.")
        return

    subscribers = read_subscribers()
    if not subscribers:
        logger.info("Nenhum inscrito para alerta de inicialização.")
        return

    boot_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(psutil.boot_time()))
    text = (
        f"🔔 {SERVER_NAME} iniciou.\n"
        f"Boot do sistema: {boot_time}"
    )

    for chat_id, label in subscribers.items():
        try:
            await application.bot.send_message(chat_id=chat_id, text=text)
            logger.info("Alerta de boot enviado para %s (%s)", chat_id, label)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Falha ao enviar alerta de boot para %s: %s", chat_id, exc)


async def on_post_init(application: Application) -> None:
    await notify_boot(application)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Erro durante o processamento do update: %s", context.error)



def validate_environment() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Defina TELEGRAM_BOT_TOKEN no arquivo .env.")



def main() -> None:
    validate_environment()
    logger.info("Iniciando bot do servidor %s", SERVER_NAME)

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(on_post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("enable_aow", enable_aow_command))
    app.add_handler(CommandHandler("disable_aow", disable_aow_command))
    app.add_error_handler(error_handler)

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
