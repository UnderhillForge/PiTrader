import asyncio
import json
import os
from datetime import datetime

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Static
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

try:
    import websockets
except ImportError:
    websockets = None


class TradingDashboard(App):
    TITLE = "Trading btop Console"
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("m", "cycle_mode", "Cycle Mode"),
        ("1", "toggle_cpu", "CPU"),
        ("2", "toggle_mem", "MEM"),
        ("3", "toggle_net", "NET"),
        ("4", "toggle_proc", "PROC"),
        ("left", "prev_asset", "Prev Asset"),
        ("right", "next_asset", "Next Asset"),
        ("p", "toggle_park", "Park/Resume"),
    ]

    CSS = """
    Screen {
        layout: vertical;
        background: #050607;
        color: #d7dce2;
    }
    #tabs_row {
        height: 2;
    }
    #tabs {
        border: round #2bd66f;
        padding: 0 1;
    }
    #cpu_row {
        height: 32%;
    }
    #cpu_panel {
        border: round #2bd66f;
        padding: 0 1;
    }
    #main_row {
        height: 68%;
    }
    #left_col {
        width: 45%;
    }
    #right_col {
        width: 55%;
    }
    #mem_row {
        height: 56%;
    }
    #net_row {
        height: 44%;
    }
    #mem_panel {
        border: round #f5d90a;
        padding: 0 1;
    }
    #net_panel {
        border: round #b46cff;
        padding: 0 1;
    }
    #proc_panel {
        border: round #ff5555;
        padding: 0 1;
    }
    """

    def __init__(self):
        super().__init__()
        self.snapshot = {
            "ts": None,
            "equity": 0.0,
            "equity_raw": 0.0,
            "sim_base_equity": 0.0,
            "sim_realized_pnl": 0.0,
            "mode": "unknown",
            "readiness_hours": 0,
            "ready_to_trade": False,
            "aggr_target": 0.0,
            "safe_target": 0.0,
            "parked": False,
            "basket_size": 0,
            "price_count": 0,
            "open_trades_count": 0,
            "new_alts": [],
            "top_prices": [],
            "top_histories": {},
            "open_trades": [],
            "rumors_summary": "none",
            "rumor_headlines": [],
            "last_decision": "n/a",
            "last_decision_asset": None,
            "last_decision_reason": "",
            "last_decision_ts": None,
            "focus_asset": None,
            "focus_price_history": [],
        }
        self.last_error = ""
        self.selected_asset = None
        self.ws_url = os.getenv("DASHBOARD_WS_URL", "ws://127.0.0.1:8765")
        self.park_flag = self._resolve_park_flag()
        self.shown_boxes = {"cpu", "mem", "net", "proc"}
        self.view_modes = [
            ("full", {"cpu", "mem", "net", "proc"}),
            ("proc", {"cpu", "proc"}),
            ("stat", {"cpu", "mem", "net"}),
            ("user", {"cpu", "mem", "net", "proc"}),
        ]
        self.view_mode_index = 0

    def _resolve_park_flag(self):
        env_override = os.getenv("PARK_FLAG")
        if env_override:
            return env_override
        path = os.getenv("CONFIG_JSON_PATH", "config.json")
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            return str(data.get("PARK_FLAG") or "parked.flag").strip() or "parked.flag"
        except Exception:
            return "parked.flag"

    def compose(self) -> ComposeResult:
        with Vertical(id="tabs_row"):
            yield Static(id="tabs")
        with Vertical(id="cpu_row"):
            yield Static(id="cpu_panel")
        with Horizontal(id="main_row"):
            with Vertical(id="left_col"):
                with Vertical(id="mem_row"):
                    yield Static(id="mem_panel")
                with Vertical(id="net_row"):
                    yield Static(id="net_panel")
            with Vertical(id="right_col"):
                yield Static(id="proc_panel")

    async def on_mount(self) -> None:
        self.set_interval(1.0, self._refresh_ui)
        self.run_worker(self._feed_loop(), exclusive=True)

    async def _feed_loop(self):
        if websockets is None:
            self.last_error = "Missing websockets package"
            return

        while True:
            try:
                async with websockets.connect(self.ws_url, ping_interval=15, ping_timeout=15) as ws:
                    self.last_error = ""
                    async for message in ws:
                        payload = json.loads(message)
                        if isinstance(payload, dict):
                            self.snapshot = payload
            except Exception as exc:
                self.last_error = str(exc)
                await asyncio.sleep(2)

    def _assets(self):
        return [row.get("asset") for row in (self.snapshot.get("top_prices") or []) if row.get("asset")]

    def _set_park(self, parked):
        if parked:
            with open(self.park_flag, "w", encoding="utf-8"):
                pass
        elif os.path.exists(self.park_flag):
            os.remove(self.park_flag)
        self.snapshot["parked"] = parked

    def action_toggle_park(self):
        self._set_park(not bool(self.snapshot.get("parked")))

    def action_prev_asset(self):
        assets = self._assets()
        if not assets:
            return
        if self.selected_asset not in assets:
            self.selected_asset = assets[0]
            return
        index = assets.index(self.selected_asset)
        self.selected_asset = assets[(index - 1) % len(assets)]

    def action_next_asset(self):
        assets = self._assets()
        if not assets:
            return
        if self.selected_asset not in assets:
            self.selected_asset = assets[0]
            return
        index = assets.index(self.selected_asset)
        self.selected_asset = assets[(index + 1) % len(assets)]

    def _toggle_box(self, name):
        if name in self.shown_boxes:
            if len(self.shown_boxes) > 1:
                self.shown_boxes.remove(name)
        else:
            self.shown_boxes.add(name)

    def action_toggle_cpu(self):
        self._toggle_box("cpu")

    def action_toggle_mem(self):
        self._toggle_box("mem")

    def action_toggle_net(self):
        self._toggle_box("net")

    def action_toggle_proc(self):
        self._toggle_box("proc")

    def action_cycle_mode(self):
        self.view_mode_index = (self.view_mode_index + 1) % len(self.view_modes)
        _, boxes = self.view_modes[self.view_mode_index]
        self.shown_boxes = set(boxes)

    def _sparkline(self, values, width=120):
        points = [float(v) for v in values if isinstance(v, (int, float))]
        if not points:
            return "·" * min(width, 40)
        if len(points) > width:
            step = max(1, len(points) // width)
            points = points[::step][:width]
        chars = "▁▂▃▄▅▆▇█"
        low = min(points)
        high = max(points)
        span = max(high - low, 1e-9)
        return "".join(chars[int((v - low) / span * (len(chars) - 1))] for v in points)

    def _aggregate_ohlc(self, history, bins=64):
        values = [float(v) for v in history if isinstance(v, (int, float))]
        if len(values) < 6:
            return []
        step = max(2, len(values) // bins)
        out = []
        for index in range(0, len(values), step):
            chunk = values[index : index + step]
            if len(chunk) < 2:
                continue
            out.append({"open": chunk[0], "high": max(chunk), "low": min(chunk), "close": chunk[-1]})
        return out

    def _icicle_rows(self, ohlc_rows):
        if not ohlc_rows:
            return Text("insufficient history", style="#f5d90a"), Text()

        spreads = [row["high"] - row["low"] for row in ohlc_rows]
        scale = max(max(spreads), 1e-9)

        wick_row = Text()
        body_row = Text()
        for row in ohlc_rows:
            spread = row["high"] - row["low"]
            strength = spread / scale
            if strength > 0.8:
                body, wick = "█", "┃"
            elif strength > 0.6:
                body, wick = "▇", "┃"
            elif strength > 0.4:
                body, wick = "▆", "│"
            elif strength > 0.2:
                body, wick = "▅", "│"
            else:
                body, wick = "▃", "╵"
            style = "#36ff87" if row["close"] >= row["open"] else "#ff5f6d"
            wick_row.append(wick, style=style)
            body_row.append(body, style=style)
        return wick_row, body_row

    def _tabs_panel(self):
        ts = self.snapshot.get("ts")
        clock = "--:--:--"
        if ts:
            try:
                clock = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).strftime("%H:%M:%S")
            except ValueError:
                pass

        mode_name, _ = self.view_modes[self.view_mode_index]
        shown = " ".join([name for name in ["cpu", "mem", "net", "proc"] if name in self.shown_boxes])
        focus = self.selected_asset or self.snapshot.get("focus_asset") or "-"

        text = Text()
        text.append("cpu", style="#2bd66f")
        text.append("|", style="#9aa5b1")
        text.append("mem", style="#f5d90a")
        text.append("|", style="#9aa5b1")
        text.append("net", style="#b46cff")
        text.append("|", style="#9aa5b1")
        text.append("proc", style="#ff5555")
        text.append("  menu  mini  ", style="#718096")
        text.append(f"view:{mode_name}  boxes:{shown}  focus:{focus}  {clock}", style="#d7dce2")
        if self.last_error:
            text.append(f"  feed:{self.last_error}", style="#ff6b6b")
        return Panel(text, border_style="#2bd66f")

    def _cpu_panel(self):
        snap = self.snapshot
        status = "PARKED" if bool(snap.get("parked")) else "ACTIVE"
        gate = "READY" if bool(snap.get("ready_to_trade")) else "WARMUP"
        mode = str(snap.get("mode") or "unknown").upper()
        focus = self.selected_asset or snap.get("focus_asset") or "-"

        history = (snap.get("top_histories") or {}).get(focus) or snap.get("focus_price_history") or []
        graph = self._sparkline(history, width=145)

        equity = float(snap.get("equity") or 0)
        raw_equity = float(snap.get("equity_raw") or 0)
        sim_pnl = float(snap.get("sim_realized_pnl") or 0)
        pnl_style = "#36ff87" if sim_pnl >= 0 else "#ff5f6d"

        out = Text()
        out.append(f"mode:{mode}  status:{status}  gate:{gate}  ", style="#d7dce2")
        out.append(f"equity:${equity:,.2f}  raw:${raw_equity:,.2f}  ", style="#7dd3fc")
        out.append(f"sim_pnl:{sim_pnl:+.2f}\n", style=pnl_style)
        out.append(graph + "\n", style="#2bd66f")
        out.append(
            f"basket:{int(snap.get('basket_size') or 0)}  prices:{int(snap.get('price_count') or 0)}  open:{int(snap.get('open_trades_count') or 0)}",
            style="#b0bac5",
        )

        return Panel(out, title="cpu", border_style="#2bd66f")

    def _mem_panel(self):
        snap = self.snapshot
        eq = float(snap.get("equity") or 0)
        aggr = float(snap.get("aggr_target") or 0)
        safe = float(snap.get("safe_target") or 0)

        info = Table(show_header=False, expand=True)
        info.add_column("k", style="#f5d90a")
        info.add_column("v", justify="right", style="#e2e8f0")
        info.add_row("Total", f"${eq:,.2f}")
        info.add_row("Agg", f"${aggr:,.2f}")
        info.add_row("Safe", f"${safe:,.2f}")

        pos = Table(show_header=True, expand=True)
        pos.add_column("asset", style="#67e8f9")
        pos.add_column("side")
        pos.add_column("entry", justify="right")
        pos.add_column("rem", justify="right")

        open_trades = snap.get("open_trades") or []
        for trade in open_trades[:8]:
            pos.add_row(
                str(trade.get("asset") or "-"),
                str(trade.get("side") or "-"),
                f"{float(trade.get('entry') or 0):.5f}",
                f"{float(trade.get('remaining_size') or 0):.4f}",
            )
        if not open_trades:
            pos.add_row("-", "-", "-", "-")

        return Panel(Group(info, pos), title="mem", border_style="#f5d90a")

    def _net_panel(self):
        prices = self.snapshot.get("top_prices") or []
        focus = self.selected_asset or self.snapshot.get("focus_asset")
        history = (self.snapshot.get("top_histories") or {}).get(focus) or self.snapshot.get("focus_price_history") or []
        ohlc_rows = self._aggregate_ohlc(history)
        wick_row, body_row = self._icicle_rows(ohlc_rows)

        chart = Text()
        chart.append(f"focus:{focus or '-'}\n", style="#d7dce2")
        chart.append_text(wick_row)
        chart.append("\n")
        chart.append_text(body_row)

        table = Table(show_header=True, expand=True)
        table.add_column("asset", style="#67e8f9")
        table.add_column("px", justify="right")
        table.add_column("a1", justify="right")
        table.add_column("a6", justify="right")
        for row in prices[:8]:
            table.add_row(
                str(row.get("asset") or ""),
                f"{float(row.get('price') or 0):.5f}",
                f"{float(row.get('atr_1h') or 0):.4f}" if row.get("atr_1h") is not None else "-",
                f"{float(row.get('atr_6h') or 0):.4f}" if row.get("atr_6h") is not None else "-",
            )
        if not prices:
            table.add_row("-", "-", "-", "-")

        return Panel(Group(chart, table), title="net", border_style="#b46cff")

    def _proc_panel(self):
        snap = self.snapshot
        decision = str(snap.get("last_decision") or "n/a")
        decision_asset = str(snap.get("last_decision_asset") or "-")
        decision_ts = str(snap.get("last_decision_ts") or "n/a")
        reason = str(snap.get("last_decision_reason") or "")
        if len(reason) > 120:
            reason = reason[:117] + "..."

        info = Text()
        info.append(f"decision:{decision}  asset:{decision_asset}  ts:{decision_ts}\n", style="#67e8f9")
        info.append(f"reason:{reason or '-'}\n", style="#d7dce2")
        info.append(f"rumors:{str(snap.get('rumors_summary') or 'none')[:140]}\n", style="#f5d90a")

        table = Table(show_header=True, expand=True)
        table.add_column("asset", style="#67e8f9")
        table.add_column("sent", justify="right")
        table.add_column("pump", justify="center")
        table.add_column("headline")
        for item in (snap.get("rumor_headlines") or [])[:14]:
            headline = str(item.get("rumor") or "").replace("\n", " ").strip()
            if len(headline) > 56:
                headline = headline[:53] + "..."
            table.add_row(
                str(item.get("asset") or "?"),
                f"{float(item.get('sent') or 0):+.1f}",
                "Y" if int(item.get("pump") or 0) else "N",
                headline,
            )
        if not (snap.get("rumor_headlines") or []):
            table.add_row("-", "-", "-", "no rumor rows")

        footer = Text("keys: q quit | m cycle-view | 1/2/3/4 toggle boxes | ←/→ focus | p park", style="#94a3b8")

        return Panel(Group(info, table, footer), title="proc", border_style="#ff5555")

    def _apply_visibility(self):
        self.query_one("#cpu_row", Vertical).styles.display = "block" if "cpu" in self.shown_boxes else "none"

        left_visible = bool({"mem", "net"} & self.shown_boxes)
        right_visible = "proc" in self.shown_boxes

        self.query_one("#left_col", Vertical).styles.display = "block" if left_visible else "none"
        self.query_one("#right_col", Vertical).styles.display = "block" if right_visible else "none"

        self.query_one("#mem_row", Vertical).styles.display = "block" if "mem" in self.shown_boxes else "none"
        self.query_one("#net_row", Vertical).styles.display = "block" if "net" in self.shown_boxes else "none"

    def _refresh_ui(self):
        self._apply_visibility()
        self.query_one("#tabs", Static).update(self._tabs_panel())
        if "cpu" in self.shown_boxes:
            self.query_one("#cpu_panel", Static).update(self._cpu_panel())
        if "mem" in self.shown_boxes:
            self.query_one("#mem_panel", Static).update(self._mem_panel())
        if "net" in self.shown_boxes:
            self.query_one("#net_panel", Static).update(self._net_panel())
        if "proc" in self.shown_boxes:
            self.query_one("#proc_panel", Static).update(self._proc_panel())


if __name__ == "__main__":
    TradingDashboard().run()
