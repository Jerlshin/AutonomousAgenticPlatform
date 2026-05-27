from rich.console import Console
from rich.panel import Panel

console = Console()

def print_header(title: str):
    console.print(
        Panel.fit(title, style="bold cyan")
    )
    
