"""Smoke test against SPADE's local embedded XMPP server, not a MAS integration test."""
import asyncio
import spade
from spade.agent import Agent


async def main():
    agent = Agent('smoke@localhost', 'local-smoke-password')
    try:
        await asyncio.wait_for(agent.start(auto_register=True), timeout=30)
        if not agent.is_alive():
            raise RuntimeError('Agent did not become alive')
        await agent.stop()
        print('SPADE agent started and stopped successfully', flush=True)
    finally:
        if agent.is_alive():
            await agent.stop()


if __name__ == '__main__':
    spade.run(main(), embedded_xmpp_server=True)
