"""Optional stdio MCP adapter for the legacy server's existing HTTP endpoints."""

from .client import IPhoneClient


def create_server(url: str | None = None):
    try:
        from mcp.server.fastmcp import FastMCP, Image
    except ImportError as e:
        raise RuntimeError('Install the bridge with: pip install "hidagent[mcp]"') from e

    mcp = FastMCP("iPhone Agent", instructions=(
        "Observe with iphone_screenshot before acting and again after each action. "
        "Coordinates are image fractions 0–1. A command acknowledgement does not verify its visible effect. "
        "Do not mix direct controls with a delegated task. There is one current task, no task IDs or ownership leases. "
        "Poll task_status until running is false and inspect the steps and screen. "
        "stop_task is the emergency stop; it releases and re-homes the pointer. "
        "Only act on the user's requested device task."))

    @mcp.tool()
    def iphone_status() -> dict:
        """Read device, screen, calibration and current agent readiness."""
        with IPhoneClient(url) as client:
            return client.status()

    @mcp.tool()
    def iphone_screenshot() -> Image:
        """Read the latest AirPlay frame. It can precede the visible effect of a recent command."""
        with IPhoneClient(url) as client:
            return Image(data=client.screenshot(), format="jpeg")

    @mcp.tool()
    def iphone_tap(x: float, y: float, precise: bool = False) -> dict:
        """Tap a visible target at image fractions 0–1; observe the screen afterward.

        precise=True finds the pointer on screen and corrects a miss before tapping (about 1 s slower):
        use it for small or crowded targets, near screen edges, or after a tap missed."""
        with IPhoneClient(url) as client:
            return client.action("tap", x=x, y=y, precise=precise)

    @mcp.tool()
    def iphone_swipe(x1: float, y1: float, x2: float, y2: float) -> dict:
        """Swipe between image fractions 0–1; observe the screen afterward."""
        with IPhoneClient(url) as client:
            return client.action("swipe", x1=x1, y1=y1, x2=x2, y2=y2)

    @mcp.tool()
    def iphone_scroll(x: float, y: float, lines: int) -> dict:
        """Scroll at image fractions x,y; lines must be between -100 and 100."""
        with IPhoneClient(url) as client:
            return client.action("scroll", x=x, y=y, lines=lines)

    @mcp.tool()
    def iphone_type(text: str) -> dict:
        """Type one line into a visibly focused field, without submitting.

        Non-ASCII text (e.g. Chinese) is pasted through Universal Clipboard (the server's Mac and the
        iPhone need the same Apple Account with Handoff on) and takes about 5 s. iOS may then ask
        "Allow Paste": take a screenshot and tap Allow Paste. If Universal Clipboard did not deliver,
        the phone pastes its own older clipboard instead, so check the field before submitting.
        If the phone's keyboard is a Chinese input method, typed ASCII goes through it: switch to
        an English keyboard first."""
        with IPhoneClient(url) as client:
            return client.action("type", text=text)

    @mcp.tool()
    def iphone_key(combo: str) -> dict:
        """Press a keyboard combination, such as cmd+a or enter."""
        with IPhoneClient(url) as client:
            return client.action("key", text=combo)

    @mcp.tool()
    def iphone_home() -> dict:
        """Attempt the legacy Home swipe; inspect the screen to confirm it worked."""
        with IPhoneClient(url) as client:
            return client.action("home")

    @mcp.tool()
    def iphone_back() -> dict:
        """Attempt the legacy back gesture; inspect the screen afterward."""
        with IPhoneClient(url) as client:
            return client.action("back")

    @mcp.tool()
    def iphone_calibrate(x: float | None = None, y: float | None = None) -> dict:
        """Start calibration; if auto=false, ask the user to locate the pointer and supply both fractions."""
        with IPhoneClient(url) as client:
            return client.calibrate(x, y)

    @mcp.tool()
    def delegate_task(goal: str) -> dict:
        """Start the server's existing vision agent. Requires its model configuration. Poll task_status."""
        with IPhoneClient(url) as client:
            return client.delegate(goal)

    @mcp.tool()
    def task_status() -> dict:
        """Read the single current task's running flag, goal and steps; no historical task lookup."""
        with IPhoneClient(url) as client:
            return client.task_status()

    @mcp.tool()
    def stop_task() -> dict:
        """Stop the current agent and release/re-home the pointer, using the existing emergency stop."""
        with IPhoneClient(url) as client:
            return client.stop()

    return mcp


def serve(url: str | None = None):
    create_server(url).run(transport="stdio")
