"""Loopback ASGI endpoint; expose only through the TLS reverse proxy."""
from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from carbon.transport.web_tunnel import tunnel

app = Starlette(routes=[WebSocketRoute('/api/carbon-transport/{service}', tunnel)])
