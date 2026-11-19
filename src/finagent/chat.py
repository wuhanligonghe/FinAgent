"""Deprecated Python adapter; no routing or independent loop is implemented here."""
from .harness.terminal import HELP, run_terminal
from .harness.host import SessionHost

def chat_loop(loop, limits, *, symbol=None, as_of=None, required_tools=(), require_data=None,
              session_id='default', read=None, write=None, research_settings=None):
    service = getattr(loop.registry,'service',None)
    settings = research_settings or service.settings
    host = SessionHost(settings,session_id,model=loop.model,service=service,write=write)
    if symbol:
        host.ctx.bind_symbol(symbol)
    if as_of:
        host.ctx.as_of, host.ctx.frozen_as_of, host.ctx.cutoff_explicit = as_of, as_of, True
    return run_terminal(settings,host=host,read=read,write=write or print)
