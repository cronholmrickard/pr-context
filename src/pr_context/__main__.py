from pr_context.config import get_settings
from pr_context.server import mcp

settings = get_settings()

if settings.transport == "sse":
    mcp.settings.host = "0.0.0.0"
    mcp.settings.port = settings.port
    mcp.run(transport="sse")
else:
    mcp.run(transport="stdio")
