"""
Smart Home ADK Agent - MQTT pametna kuća

Upravljanje svjetlima, utičnicama i dimmerom preko ESP32-IO MQTT brokera.
"""

import os
import logging

from agents.adk_agents.adk_agent_factory import create_adk_agent

logger = logging.getLogger(__name__)


def create_smart_home_agent(
    model: str = "gemini-3.5-flash",
    user_timezone: str = "Europe/Zagreb"
):
    """Create Smart Home ADK agent for MQTT control"""

    # Load instructions
    instruction_path = os.path.join(
        os.path.dirname(__file__), "..", "smart_home", "instructions.md"
    )
    with open(instruction_path, "r", encoding="utf-8") as f:
        instruction = f.read()

    # Datum/vrijeme se NE ubacuje ovdje: to bi zamrznulo sat na trenutak
    # kad je agent stvoren. Predaje se predložak, a tvornica ga omota u
    # ADK instruction provider koji ga renderira pri svakom pozivu.

    # Import tools
    from tools.adk_tools.mqtt_adk_tools import get_mqtt_adk_tools
    tools = get_mqtt_adk_tools()

    # TV / media tools preko Home Assistant REST API-ja (opt-in: aktivno samo
    # kad su HA_URL i HA_TOKEN postavljeni u .env).
    if os.getenv("HA_URL", "").strip() and os.getenv("HA_TOKEN", "").strip():
        from tools.adk_tools.ha_adk_tools import get_ha_adk_tools
        from tools.adk_tools.ha_sensor_tools import get_ha_sensor_tools
        from tools.adk_tools.ha_shopping_tools import get_shopping_list_tools
        # Sklopke idu preko MQTT-a, ali mjerenja (temperatura, vlaga, tlak,
        # kvaliteta zraka, potrošnja) postoje samo u HA — bez ovih read-only
        # alata agent ih nema odakle pročitati.
        #
        # Lista za kupovinu je isti slučaj: živi kao todo entitet u HA, a
        # ovdje je zato što je svi kanali (web, Telegram, glas) dosežu kroz
        # ovog agenta — jedan skup alata, tri ulaza.
        tools = (
            tools
            + get_ha_adk_tools()
            + get_ha_sensor_tools()
            + get_shopping_list_tools()
        )
        logger.info(
            "Smart Home agent: Home Assistant TV + sensor + shopping list tools enabled"
        )

    agent = create_adk_agent(
        name="smart_home",
        model=model,
        # The orchestrator routes on this string. When it said only "lights,
        # outlets, dimmer, scenes", the orchestrator answered a TV request with
        # "nemam agenta koji može upravljati TV-om" — the tools were there, the
        # description was not. Anything this agent can do belongs here.
        description=(
            "Smart home, TV and house sensors. MQTT (ESP32-IO): lights, outlets, "
            "dimmer, scenes. Television via Home Assistant: power, volume, launch "
            "apps (YouTube, Netflix, and learned ones such as A1 Xplore TV), "
            "switch channels by name or number, play from YouTube, remote keys. "
            "Reads sensors: temperature, humidity, pressure per room, air quality, "
            "power consumption, and their history (daily min/max/average). "
            "Also owns the household SHOPPING LIST (lista za kupovinu, popis za "
            "ducan): add items, show it, mark them bought, put one back, remove "
            "them, and mark everything bought except named items."
        ),
        tools=tools,
        instruction=instruction,
        load_instruction_from_file=False,
        config={
            "temperature": 0.3,
            "max_tokens": 1536,
        }
    )

    logger.info("Smart Home ADK agent created")
    return agent
