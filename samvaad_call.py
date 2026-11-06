"""A live call answered by the hosted Sarvam (Samvaad) agent instead of our own pipeline (CALL_ENGINE=sarvam_agent).

Used by the web page's 📞 (/ws/call in server.py) and by WhatsApp calls (wa_call.py), so both sound the same. The agent
does its own listening, turn-taking and barge-in; its answers come from its own instructions on Sarvam's platform, not
from hospital.json. We pass audio both ways, show the captions, save the caller's name and hospital from the transcript
(the agent never sees the patient record), and show our own map and written way for each place the caller asks about.

    ws: the browser's WebSocket, or wa_call.Bridge on a WhatsApp call (same receive() / send_json()).
.env (never printed): SARVAM_AGENT_API_KEY (indus.sarvam.ai platform key), SARVAM_AGENT_ORG_ID, SARVAM_AGENT_WORKSPACE_ID,
SARVAM_AGENT_APP_ID, SARVAM_AGENT_VERSION (empty = newest committed).
"""
import asyncio
import os

import httpx
from fastapi import WebSocketDisconnect

import brain
import core
import patient_store as store  # noqa: F401  (loads .env)

AGENT = {k: os.environ.get(f"SARVAM_AGENT_{k.upper()}", "").strip() for k in ("org_id", "workspace_id", "app_id")}
AGENT_KEY = os.environ.get("SARVAM_AGENT_API_KEY", "").strip()
AGENT_VERSION = int(os.environ["SARVAM_AGENT_VERSION"]) if os.environ.get("SARVAM_AGENT_VERSION", "").strip().isdigit() else None
HOTWORDS = list(dict.fromkeys(core.KEYTERMS + ["LFT", "KFT", "histopathology", "USG", "X-ray", "RT card", "Day Care", "OncoSaathi"]))
_latest_version = 1


def ready():
    return all(AGENT.values()) and bool(AGENT_KEY)


async def latest_version():
    """Newest committed agent version (SARVAM_AGENT_VERSION pins one instead). Without this the SDK calls the deployed one."""
    global _latest_version
    if AGENT_VERSION:
        return AGENT_VERSION
    url = (f"https://apps.sarvam.ai/api/app-runtime/orgs/{AGENT['org_id']}/workspaces/{AGENT['workspace_id']}"
           f"/apps/{AGENT['app_id']}/url")
    async with httpx.AsyncClient(timeout=10) as c:
        v = _latest_version
        while True:
            r = await c.get(url, params={"interaction_type": "call", "version": v + 1}, headers={"X-API-Key": AGENT_KEY})
            if r.status_code != 200:
                break
            v += 1
    _latest_version = v
    return v


async def routes_in(text, state):
    """Our chat answers (with a map) for the places in one thing the caller said; both places when two are named
    ("MRI kahan hai aur radiology kahan hai"). state is a chat state kept across the call."""
    outs = [await brain.respond(text[:500], state)]
    first = (outs[0].get("map") or {}).get("to", "").split("@")[0]
    for uid in brain.two_places(text.lower()) or []:
        u = core.UNIT.get(uid)
        if u and uid != first:
            hi = outs[0].get("lang") == "hi"
            outs.append(await brain.respond(f"{u.get('name_hi') or u['name']} कहाँ है" if hi else f"where is the {u['name']}", state))
    return [o for o in outs if o.get("map")]


async def run(ws, call_token, st, live_maps=True):
    """One call. call_token is a fresh id the agent may pass back to /tools/*; it reveals nothing about the person.

    live_maps (the web page): each place the caller asks about is worked out by our brain from hospital.json and shown
    on screen while the agent talks — the map with arrows and the written way — since the agent can't send pictures and
    its own instructions can miss a department (6 Nov 2026). WhatsApp calls send them into the chat after the call
    instead (wa_call.Session.send_routes)."""
    from pydantic import SecretStr
    from sarvam_conv_ai_sdk import AsyncSamvaadAgent, InteractionConfig, InteractionType
    from sarvam_conv_ai_sdk.messages.types import UserIdentifierType

    greeting = brain.opening(st)
    route_state = {"pid": None, "mode": "chat", "awaiting": None, "where": None, "place": None, "hospital": "tata-parel"}
    route_lock, shown, jobs = asyncio.Lock(), set(), set()

    async def send(obj):
        try:
            await ws.send_json(obj)
        except Exception:
            pass

    async def show_route(text):
        async with route_lock:                                 # in order, so "wahan kaise jaun" follows the place before it
            try:
                outs = await routes_in(text, route_state)
            except Exception as e:
                print(f"  [map] could not work out a route: {e!r}", flush=True)
                return
        for out in outs:
            m = out["map"]
            if m["url"] in shown:
                continue
            shown.add(m["url"])
            print(f"  [map] {m['to']} shown on screen", flush=True)
            await send({"type": "map", "url": m["url"], "caption": m.get("caption", ""), "to": m["to"],
                        "text": out.get("text", "")})

    async def on_audio(msg):
        await send({"type": "audio", "b64": msg.audio_base64, "sr": msg.sample_rate or 16000})

    async def on_transcript(msg):
        role = str(getattr(msg.role, "value", msg.role))
        print(f"  [{role}] {msg.content}", flush=True)
        await send({"type": "transcript", "role": role, "text": msg.content})
        # Memory without needing a Sarvam tool: while the opening questions are open (name, hospital, in/coming),
        # read the caller's answer from the transcript and save it ourselves. The agent never sees the record.
        if "user" in role.lower() and msg.content:
            try:
                got = brain.capture_facts(msg.content, st)
                if got:
                    print(f"  [memory] saved {', '.join(got)} from the call", flush=True)
            except Exception as e:
                print(f"  [memory] could not save: {e!r}", flush=True)
            if live_maps:
                job = asyncio.create_task(show_route(msg.content))
                jobs.add(job)
                job.add_done_callback(jobs.discard)

    async def on_event(ev):
        await send({"type": "event", "name": str(getattr(ev, "type", "") or type(ev).__name__)})

    agent = AsyncSamvaadAgent(
        api_key=SecretStr(AGENT_KEY),
        config=InteractionConfig(user_identifier_type=UserIdentifierType.CUSTOM, user_identifier=call_token,
                                 interaction_type=InteractionType.CALL, sample_rate=16000, speech_hotwords=HOTWORDS,
                                 version=await latest_version(), initial_bot_message=greeting,
                                 agent_variables={"session": call_token, "opening_question": st.get("awaiting") or ""}, **AGENT),
        audio_callback=on_audio, transcript_callback=on_transcript, event_callback=on_event)
    try:
        await agent.start()
        if not await agent.wait_for_connect(10):
            await send({"type": "error", "message": "Could not connect to the Sarvam agent."})
            return
        await send({"type": "connected"})
        print(f"Call connected: patient={st.get('pid') or '-'} agent v{agent.config.version}", flush=True)
        disconnected = asyncio.create_task(agent.wait_for_disconnect())
        while not disconnected.done():
            recv = asyncio.create_task(ws.receive())
            done, _ = await asyncio.wait({recv, disconnected}, return_when=asyncio.FIRST_COMPLETED)
            if recv not in done:
                recv.cancel()
                print("Call ended by the Sarvam agent side", flush=True)
                break
            m = recv.result()
            if m.get("type") == "websocket.disconnect":
                print(f"Call ended by the caller (code {m.get('code')})", flush=True)
                break
            if m.get("bytes"):
                await agent.send_audio(m["bytes"])
        await send({"type": "ended"})
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print("Agent call error:", repr(e))
        await send({"type": "error", "message": f"Agent error: {e}"})
    finally:
        for job in list(jobs):
            job.cancel()
        try:
            await agent.stop()
        except Exception:
            pass
