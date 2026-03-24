"""
run_model.py – Strategy orchestration, advanced live dashboard, CSV export.
Imported and launched by main.py.
"""

from __future__ import annotations

import asyncio
import csv
import os
from datetime import datetime, timezone

from rich.live import Live
from rich.table import Table
from rich.panel import Panel
from rich.layout import Layout
from rich.text import Text

from utils.bot_state import BotState
from utils.config import INSTRUMENT
from strategies import (
    ContDeLarrard, KyleStrategy, OFIStrategy, CompositeAlpha,
)
from strategies.base import BaseStrategy


# ── strategy factory ────────────────────────────────────────────────────

def build_strategies() -> list[BaseStrategy]:
    return [
        ContDeLarrard(max_position=0.01, cooldown_ms=5_000),
        KyleStrategy(max_position=0.01, cooldown_ms=5_000),
        OFIStrategy(max_position=0.01, cooldown_ms=5_000),
        CompositeAlpha(max_position=0.01, cooldown_ms=30_000),
    ]


# ── event fan-out ───────────────────────────────────────────────────────

async def book_feed(state: BotState, strategies: list[BaseStrategy]):
    while True:
        await state.new_snapshot_event.wait()
        state.new_snapshot_event.clear()
        for s in strategies:
            s.on_book_update(state.book_snapshot)


async def trade_feed(state: BotState, strategies: list[BaseStrategy]):
    while True:
        await state.trades_event.wait()
        state.trades_event.clear()
        for s in strategies:
            s.on_trades(state.recent_trades)


# ── rendering helpers ───────────────────────────────────────────────────

_SPARK = "▁▂▃▄▅▆▇█"


def _pnl_color(val: float) -> str:
    if val > 0:
        return "green"
    if val < 0:
        return "red"
    return "white"


def _sparkline(values: list[float], width: int = 40) -> str:
    if len(values) < 2:
        return ""
    vals = values[-width:]
    mn, mx = min(vals), max(vals)
    rng = mx - mn if mx - mn > 0 else 1
    return "".join(
        _SPARK[min(7, int((v - mn) / rng * 7))] for v in vals
    )


def _signal_bar(value: float, width: int = 20) -> str:
    """Horizontal bar centred on zero for a value in [-1, +1]."""
    half = width // 2
    pos = int(max(0.0, min(1.0, abs(value))) * half)
    if value >= 0:
        left = "░" * half
        right = "█" * pos + "░" * (half - pos)
        return f"[green]{left}│{right}[/]"
    else:
        left = "░" * (half - pos) + "█" * pos
        right = "░" * half
        return f"[red]{left}│{right}[/]"


# ── dashboard panels ────────────────────────────────────────────────────

def _panel_strategies(strategies: list[BaseStrategy]) -> Table:
    mid = next((s.mid_price for s in strategies if s.mid_price), 0.0)
    tbl = Table(
        title=f"[bold]{INSTRUMENT}  Mid {mid:,.1f}[/]",
        expand=True, padding=(0, 1),
    )
    tbl.add_column("Strategy", style="cyan", min_width=16)
    tbl.add_column("Signal", justify="right")
    tbl.add_column("Pos", justify="right")
    tbl.add_column("Trades", justify="right")
    tbl.add_column("Realized", justify="right")
    tbl.add_column("Unrealized", justify="right")
    tbl.add_column("Total PnL", justify="right", min_width=11)

    for s in strategies:
        t = s.trader
        real = t.realized_pnl
        unreal = t.unrealized_pnl(s.mid_price)
        total = t.total_pnl(s.mid_price)
        tbl.add_row(
            s.name,
            f"{s.signal:+.3f}",
            f"{t.position:+.6f}",
            str(t.num_trades),
            f"[{_pnl_color(real)}]${real:+.4f}[/]",
            f"[{_pnl_color(unreal)}]${unreal:+.4f}[/]",
            f"[bold {_pnl_color(total)}]${total:+.4f}[/]",
        )
    return tbl


def _panel_sparklines(
    strategies: list[BaseStrategy],
    pnl_hist: dict[str, list[float]],
) -> Text:
    lines = Text()
    lines.append("Cumulative PnL\n\n", style="bold")
    for s in strategies:
        vals = pnl_hist.get(s.name, [])
        spark = _sparkline(vals, width=36)
        total = s.trader.total_pnl(s.mid_price)
        color = _pnl_color(total)
        lines.append(f"  {s.name:<16} ", style="cyan")
        lines.append(spark, style=color)
        lines.append(f"  ${total:+.4f}\n", style=f"bold {color}")
    return lines


def _panel_signal_breakdown(comp: CompositeAlpha) -> Text:
    t = Text()
    t.append("Composite α  Signal Breakdown\n\n", style="bold")

    rows = [
        ("Imbalance", comp.imb_signal),
        ("OFI      ", comp.ofi_signal),
        ("Flow     ", comp.flow_signal),
        ("Mean Rev ", comp.mr_signal),
    ]
    for label, val in rows:
        t.append(f"  {label}  ")
        t.append_text(Text.from_markup(_signal_bar(val)))
        t.append(f"  {val:+.3f}\n")

    t.append("  ─────────────────────────────────\n", style="dim")
    t.append(f"  Composite  ")
    t.append_text(Text.from_markup(_signal_bar(comp.composite)))
    t.append(f"  {comp.composite:+.3f}\n")

    conf_bar = int(comp.confidence * 20)
    conf_color = "green" if comp.confidence >= comp.confidence_threshold else "yellow"
    t.append(f"  Confidence ")
    t.append("█" * conf_bar + "░" * (20 - conf_bar), style=conf_color)
    t.append(f"  {comp.confidence:.2f}\n\n")

    # position bracket
    vol_label = (
        "LOW" if comp.vol_regime > 1.2
        else "HIGH" if comp.vol_regime < 0.8
        else "NORMAL"
    )
    t.append(f"  Vol: {vol_label}  ", style="dim")
    t.append(f"Jump: {'[red]YES[/]' if comp.jump_detected else '[green]no[/]'}  ")
    t.append(f"ATR: {comp.atr:.2f}\n")

    if comp.position_state != comp.FLAT:
        t.append(f"  State: [bold]{comp.position_state}[/]  ")
        t.append(f"Entry: {comp.entry_price:,.1f}  ")
        t.append(f"SL: [red]{comp.sl_price:,.1f}[/]  ")
        t.append(f"TP: [green]{comp.tp_price:,.1f}[/]\n")
    else:
        t.append("  State: [dim]FLAT[/]\n")
    return t


def _panel_thoughts(comp: CompositeAlpha, height: int = 14) -> Text:
    t = Text()
    thoughts = list(comp.thoughts)[-height:]
    for line in thoughts:
        if "ENTER" in line or ">>>" in line:
            t.append(line + "\n", style="bold green")
        elif "EXIT" in line or "<<<" in line:
            if "SL" in line:
                t.append(line + "\n", style="bold red")
            else:
                t.append(line + "\n", style="bold cyan")
        elif "SKIP" in line:
            t.append(line + "\n", style="dim")
        elif "analysis" in line:
            t.append(line + "\n", style="yellow")
        else:
            t.append(line + "\n")
    return t


def _panel_trades(strategies: list[BaseStrategy], n: int = 10) -> Table:
    all_tr = []
    for s in strategies:
        all_tr.extend(s.trader.trades)
    all_tr.sort(key=lambda r: r.timestamp, reverse=True)

    tbl = Table(expand=True, padding=(0, 1))
    tbl.add_column("Time", min_width=12)
    tbl.add_column("Strategy", style="cyan")
    tbl.add_column("Side")
    tbl.add_column("Price", justify="right")
    tbl.add_column("Size", justify="right")
    tbl.add_column("Pos", justify="right")
    tbl.add_column("PnL Δ", justify="right")

    for tr in all_tr[:n]:
        ts = datetime.fromtimestamp(
            tr.timestamp / 1000, tz=timezone.utc
        ).strftime("%H:%M:%S.%f")[:-3]
        sc = "green" if tr.side == "buy" else "red"
        arrow = "^" if tr.side == "buy" else "v"
        pnl = f"${tr.realized_pnl_delta:+.4f}" if tr.realized_pnl_delta else ""
        tbl.add_row(
            ts,
            tr.strategy,
            f"[{sc}]{arrow} {tr.side.upper()}[/]",
            f"{tr.price:,.1f}",
            f"{tr.size:.6f}",
            f"{tr.position_after:+.6f}",
            pnl,
        )
    return tbl


# ── main dashboard coroutine ────────────────────────────────────────────

async def dashboard(strategies: list[BaseStrategy]):
    pnl_hist: dict[str, list[float]] = {s.name: [] for s in strategies}

    # find the CompositeAlpha instance
    comp = next((s for s in strategies if isinstance(s, CompositeAlpha)), None)

    layout = Layout()
    layout.split_column(
        Layout(name="upper", ratio=2),
        Layout(name="lower", ratio=3),
    )
    layout["upper"].split_row(
        Layout(name="table", ratio=3),
        Layout(name="charts", ratio=2),
    )
    layout["lower"].split_column(
        Layout(name="mid", ratio=3),
        Layout(name="trades", ratio=2),
    )
    layout["mid"].split_row(
        Layout(name="signal", ratio=1),
        Layout(name="thoughts", ratio=1),
    )

    with Live(layout, refresh_per_second=2, screen=True) as live:
        while True:
            # snapshot PnL
            for s in strategies:
                pnl_hist[s.name].append(s.trader.total_pnl(s.mid_price))
                if len(pnl_hist[s.name]) > 300:
                    pnl_hist[s.name] = pnl_hist[s.name][-300:]

            layout["table"].update(
                Panel(
                    _panel_strategies(strategies),
                    title="[bold]Strategies[/]",
                    border_style="blue",
                )
            )
            layout["charts"].update(
                Panel(
                    _panel_sparklines(strategies, pnl_hist),
                    title="[bold]PnL History[/]",
                    border_style="blue",
                )
            )
            if comp:
                layout["signal"].update(
                    Panel(
                        _panel_signal_breakdown(comp),
                        title="[bold]Composite α[/]",
                        border_style="magenta",
                    )
                )
                layout["thoughts"].update(
                    Panel(
                        _panel_thoughts(comp),
                        title="[bold]Thought Process[/]",
                        border_style="yellow",
                    )
                )
            layout["trades"].update(
                Panel(
                    _panel_trades(strategies),
                    title="[bold]Recent Trades[/]",
                    border_style="blue",
                )
            )
            await asyncio.sleep(0.5)


# ── CSV export ──────────────────────────────────────────────────────────

def export_transactions(strategies: list[BaseStrategy]):
    os.makedirs("output", exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    header = [
        "timestamp", "datetime_utc", "strategy", "side", "price",
        "size", "position_after", "realized_pnl_delta", "cumulative_pnl",
    ]

    all_trades = []
    for s in strategies:
        all_trades.extend(s.trader.trades)
    all_trades.sort(key=lambda r: r.timestamp)

    if not all_trades:
        print("[EXPORT] No trades to export.")
        return

    filename = f"output/trades_{ts}.csv"
    with open(filename, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for tr in all_trades:
            dt = datetime.fromtimestamp(
                tr.timestamp / 1000, tz=timezone.utc
            ).isoformat()
            w.writerow([
                tr.timestamp, dt, tr.strategy, tr.side, tr.price,
                tr.size, tr.position_after, tr.realized_pnl_delta,
                tr.cumulative_pnl,
            ])

    print(f"[EXPORT] {len(all_trades)} trades saved to {filename}")
