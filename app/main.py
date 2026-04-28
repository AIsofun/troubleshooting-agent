"""
CLI entry. Run:
    python -m app.main                    # interactive
    python -m app.main "2号相机掉线了"    # one-shot
"""
from __future__ import annotations
import json
import sys

from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule

from app.agent.core import Agent, TraceEvent

console = Console()


def render_event(ev: TraceEvent) -> None:
    if ev.kind == "user":
        console.print(Rule(f"[bold cyan]Step {ev.step} · USER"))
        console.print(Panel(ev.payload["query"], border_style="cyan"))
    elif ev.kind == "plan":
        thought = ev.payload.get("thought") or ""
        if ev.payload["action"] == "final":
            console.print(Rule(f"[bold magenta]Step {ev.step} · PLAN → FINAL"))
        else:
            console.print(Rule(f"[bold yellow]Step {ev.step} · PLAN"))
            console.print(
                f"[yellow]→ call[/yellow] [bold]{ev.payload['tool']}[/bold]"
                f"  args={ev.payload['args']}"
            )
            if thought:
                console.print(f"[dim]reason: {thought}[/dim]")
    elif ev.kind == "tool_call":
        policy = ev.payload.get("policy")
        extra = f" [red]({policy})[/red]" if policy else ""
        console.print(f"[blue]⚙ ACT[/blue] {ev.payload['tool']}({ev.payload['args']}){extra}")
    elif ev.kind == "tool_result":
        ok = "✅" if ev.payload.get("ok") else "❌"
        console.print(f"[green]👁 OBSERVE[/green] {ok} {ev.payload['summary']}")
    elif ev.kind == "final":
        ans = ev.payload["answer"]
        console.print(Rule("[bold green]FINAL ANSWER"))
        console.print(Panel.fit(
            f"[bold]intent[/bold]: {ans.get('intent')}\n"
            f"[bold]结论[/bold]: {ans.get('conclusion')}\n\n"
            f"[bold]证据[/bold]:\n- " + "\n- ".join(ans.get("evidence", []) or ["(无)"]) +
            f"\n\n[bold]处置建议[/bold]:\n- " + "\n- ".join(ans.get("suggestions", []) or ["(无)"]) +
            f"\n\n[bold]可执行的低风险动作[/bold]: {ans.get('safe_actions') or '(无)'}",
            border_style="green",
        ))
    elif ev.kind == "error":
        console.print(f"[red]ERROR[/red] {ev.payload}")


def run_once(query: str) -> None:
    agent = Agent(on_event=render_event)
    result = agent.run(query)
    console.print(Rule("[dim]raw answer (JSON)[/dim]"))
    console.print_json(json.dumps(result.answer, ensure_ascii=False))


def main() -> None:
    if len(sys.argv) > 1:
        run_once(" ".join(sys.argv[1:]))
        return
    console.print(Panel.fit(
        "生产异常排查 Agent Demo\n输入问题，Ctrl+C 退出。例：2号相机掉线了",
        border_style="cyan",
    ))
    while True:
        try:
            q = console.input("[bold cyan]you> [/bold cyan]").strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\nbye.")
            return
        if not q:
            continue
        run_once(q)


if __name__ == "__main__":
    main()
