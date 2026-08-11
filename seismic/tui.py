import time

from seismic.config import THRESHOLD, fmt_mag
from seismic.state import sensor_state


def run_tui():
    try:
        from rich.live import Live
        from rich.table import Table
        from rich.panel import Panel
        from rich.layout import Layout
        from rich import box
    except ImportError:
        print("rich not installed — TUI disabled (pip install rich)", flush=True)
        return

    def build_display():
        snap = sensor_state.to_dict()
        now = snap['now']

        sta_tbl = Table(box=box.SIMPLE_HEAVY, show_header=True, header_style="bold cyan",
                        title="[bold]Stations[/bold]", min_width=42)
        sta_tbl.add_column("Station", style="cyan")
        sta_tbl.add_column("Conf",    justify="right")
        sta_tbl.add_column("Mag",     justify="right")
        sta_tbl.add_column("Age",     justify="right", style="dim")
        for key, s in sorted(snap['stations'].items()):
            age = now - s['last_ts']
            c = s['conf']
            color = "green" if c >= THRESHOLD else "yellow" if c > 0.5 else "dim"
            sta_tbl.add_row(key,
                            f"[{color}]{c:.3f}[/{color}]",
                            fmt_mag(s['mag_est']),
                            f"{age:.0f}s")

        det_lines = []
        for det in reversed(snap['detections'][-12:]):
            mb = f"[green]mb={det['mb']:.1f}[/green]" if det['mb'] is not None else "[dim]mb…[/dim]"
            epi = ""
            if det['epicenter']:
                la, lo = det['epicenter']
                epi = (
                    f"  [yellow]{abs(la):.1f}°{'N' if la >= 0 else 'S'} "
                    f"{abs(lo):.1f}°{'E' if lo >= 0 else 'W'}[/yellow]"
                )
            sta_str = ', '.join(det['stations'])
            det_lines.append(f"[dim]{det['ts']}[/dim]  [cyan]{sta_str}[/cyan]  {mb}{epi}")

        det_panel = Panel(
            '\n'.join(det_lines) if det_lines else "[dim]No detections yet[/dim]",
            title="[bold]Detections[/bold]",
        )
        layout = Layout()
        layout.split_column(
            Layout(Panel(sta_tbl), size=len(snap['stations']) + 6, name="stations"),
            Layout(det_panel, name="detections"),
        )
        return layout

    with Live(build_display(), refresh_per_second=1, screen=True) as live:
        while True:
            live.update(build_display())
            time.sleep(1)
