"""Detect this machine's memory and suggest a local model that fits.

Apple Silicon uses unified memory shared by CPU and GPU (Metal/MLX), so the model,
its context (KV cache), macOS and your other apps all share one pool. By default macOS
lets the GPU wire roughly 2/3 of RAM (about 3/4 on larger machines).
"""

from __future__ import annotations

import platform
import shutil
import subprocess
from dataclasses import dataclass

# Tool-capable models in the Ollama library (checked October 2026). Sizes are the
# default Q4 downloads. Newer models appear often: https://ollama.com/search?c=tools
CANDIDATES = [
    ("qwen3:4b", 2.5, "small and fast; basic tool use"),
    ("bonsai-2:9b", 3.2, "ternary-weight 9B, light on memory"),
    ("qwen3:8b", 5.2, "dependable tool calling for its size"),
    ("bonsai-2:27b", 8.3, "27B-class quality in ~8 GB (ternary weights)"),
    ("qwen3:14b", 9.3, "good balance of quality and speed"),
    ("gpt-oss:20b", 14.0, "strong reasoning and tool use (MoE)"),
    ("qwen3.8:27b", 18.0, "newest Qwen, built for long agentic tasks"),
    ("qwen3:30b", 19.0, "MoE, fast for its size"),
    ("gpt-oss:120b", 65.0, "best local quality here; needs a big machine"),
]

# Starting-point preference when several models fit (best first).
PREFERENCE = ["gpt-oss:120b", "qwen3.8:27b", "gpt-oss:20b", "qwen3:30b", "qwen3:14b", "bonsai-2:27b", "qwen3:8b",
              "bonsai-2:9b", "qwen3:4b"]  # fmt: skip


@dataclass
class Hardware:
    system: str
    machine: str
    chip: str
    memory_gb: float | None
    apple_silicon: bool
    ollama_installed: bool


def _sysctl(name: str) -> str | None:
    try:
        return subprocess.run(["sysctl", "-n", name], capture_output=True, text=True, timeout=5, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def detect() -> Hardware:
    system = platform.system()
    machine = platform.machine()
    chip = ""
    memory = None
    if system == "Darwin":
        raw = _sysctl("hw.memsize")
        memory = int(raw) / 1024**3 if raw and raw.isdigit() else None
        chip = _sysctl("machdep.cpu.brand_string") or ""
    elif system == "Linux":
        try:
            with open("/proc/meminfo") as handle:
                for line in handle:
                    if line.startswith("MemTotal:"):
                        memory = int(line.split()[1]) / 1024**2
                        break
        except OSError:
            memory = None
        chip = platform.processor()
    apple = system == "Darwin" and machine == "arm64"
    return Hardware(system, machine, chip, round(memory, 1) if memory else None, apple, shutil.which("ollama") is not None)


def recommend(memory_gb: float | None) -> dict:
    if not memory_gb:
        return {"budget_gb": None, "num_ctx": 16384, "primary": "qwen3:8b", "options": [], "note": "Could not read memory size."}
    gpu_share = 0.75 if memory_gb >= 36 else 0.66
    budget = memory_gb * gpu_share
    if memory_gb <= 12:
        num_ctx, kv_gb = 16384, 0.8
    elif memory_gb <= 24:
        num_ctx, kv_gb = 32768, 3.0
    elif memory_gb <= 48:
        num_ctx, kv_gb = 32768, 5.0
    else:
        num_ctx, kv_gb = 65536, 9.0
    usable = budget - kv_gb - (1.0 if memory_gb <= 12 else 1.5)
    fitting = [(name, size, note) for name, size, note in CANDIDATES if size <= usable]
    preferred = [name for name in PREFERENCE if any(name == f[0] for f in fitting)]
    primary = preferred[0] if preferred else None
    note = ""
    if memory_gb < 16:
        note = ("With under 16 GB, local models are slow and noticeably weaker at multi-step web research. "
                "Use the Claude API for important tasks and keep local mode for privacy-sensitive ones.")  # fmt: skip
    return {
        "budget_gb": round(budget, 1),
        "num_ctx": num_ctx,
        "primary": primary,
        "options": [{"model": n, "download_gb": s, "note": t} for n, s, t in fitting[-4:]],
        "note": note,
    }


def report() -> str:
    hw = detect()
    lines = [f"System: {hw.system} {hw.machine} {hw.chip}".rstrip()]
    if hw.memory_gb:
        lines.append(f"Memory: {hw.memory_gb:g} GB {'unified (Apple Silicon, Metal)' if hw.apple_silicon else ''}".rstrip())
    else:
        lines.append("Memory: unknown")
    if hw.system == "Darwin" and not hw.apple_silicon:
        lines.append("This is not an Apple Silicon Mac; local models will be slow.")
    rec = recommend(hw.memory_gb)
    if rec["budget_gb"]:
        label = "GPU-usable memory (macOS default limit)" if hw.apple_silicon else "Memory budget for the model"
        lines.append(f"{label}: about {rec['budget_gb']} GB")
    if rec["primary"]:
        lines.append(f"\nRecommended: {rec['primary']}  with OLLAMA_NUM_CTX={rec['num_ctx']}")
        lines.append("Other models that fit:")
        for option in rec["options"]:
            lines.append(f"  - {option['model']:<16} ~{option['download_gb']:g} GB  {option['note']}")
        lines.append("\nSet up:")
        if not hw.ollama_installed and hw.system == "Darwin":
            lines.append("  brew install ollama        # or the Ollama app from https://ollama.com/download")
            lines.append("  brew services start ollama")
        elif not hw.ollama_installed:
            lines.append("  # install Ollama: https://ollama.com/download")
        lines.append(f"  ollama pull {rec['primary']}")
        lines.append(f"  # in .env: MODEL_PROVIDER=ollama  OLLAMA_MODEL={rec['primary']}  OLLAMA_NUM_CTX={rec['num_ctx']}")
    else:
        lines.append("\nNo listed local model fits comfortably; use the Claude API provider.")
    if rec["note"]:
        lines.append("\n" + rec["note"])
    lines.append("Model list checked October 2026; see https://ollama.com/search?c=tools for newer tool-capable models.")
    return "\n".join(lines)
