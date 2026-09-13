"""Read-only MQTT connectivity/auth probe. Subscribes to esp32-io/# and
homeassistant/# for a few seconds and prints what arrives. Changes nothing."""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv

load_dotenv()

try:
    import paho.mqtt.client as mqtt
except Exception as e:  # pragma: no cover
    print("PAHO_MISSING:", e)
    raise SystemExit(1)

broker = os.getenv("MQTT_BROKER")
port = int(os.getenv("MQTT_PORT", "1883"))
user = os.getenv("MQTT_USER")
password = os.getenv("MQTT_PASS")
print("broker=%s:%s user=%r pass=%s" % (broker, port, user, "<set>" if password else "<empty>"))

msgs = {}


def on_conn(client, userdata, flags, reason_code, properties=None):
    print("CONNECT reason_code=", reason_code)


def on_msg(client, userdata, msg):
    msgs[msg.topic] = msg.payload.decode("utf-8", "replace")


client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
if user or password:
    client.username_pw_set(user, password)
client.on_connect = on_conn
client.on_message = on_msg
client.connect(broker, port, keepalive=10)
client.subscribe("esp32-io/#")
client.subscribe("homeassistant/#")
client.loop_start()
time.sleep(4)
client.loop_stop()
client.disconnect()

print("topics_received=", len(msgs))
esp = [t for t in msgs if t.startswith("esp32-io/")]
print("esp32-io topics=", len(esp))
for t in sorted(msgs)[:30]:
    print("  ", t, "=", msgs[t][:60])
