# Lights Test
# 2024 (c) ralfoide at gmail
# License: MIT
#
# Target Platform: CircuitPython 9.x on AdaFruit QT PY ESP32-S2
#
# Hardware:
# - AdaFruit QT PY ESP32-S2
# - 2x 100 LEDs WS2812B addressable fairy light strings (source https://amzn.to/40UGURS)
# - LED string connected to GND and 5V on the QT PY.
# - LED string 1 and 2 connected in parallel to A1 pin on QT PY.
#
# Example effects:
# xmas colors:      "Fill #000000 1 ; SlowFill 0.1 #00FF00 10 #FF0000 10 ; Slide 0.1 80 "
# halloween colors: "Fill #000000 1 ; SlowFill 0.1 #FF2800 10 #000000 1 #FF7000 88 #000000 1 ; Slide 0.1 100"


import adafruit_connection_manager
import adafruit_logging as logging
import adafruit_minimqtt.adafruit_minimqtt as MQTT
import board
import digitalio
import neopixel
import os
import sequencer
from script_exec import InitScriptExec, EventScriptExec
import time
import wifi

_led = None
_neo = None
_mqtt = None
_boot_btn = None
_neo_wrapper: sequencer.NeoWrapper= None
_init_script: InitScriptExec = None
_event_script: EventScriptExec = None
_logger = logging.getLogger("Ambiance")
_logger.setLevel(logging.INFO)      # INFO or DEBUG

NEO_LEN = 100
NEO_STRIP_PIN = "A1"
NEO_DONT_BLINK = False

COL_OFF = (0, 0, 0)
COL_RED = (255, 0, 0)
COL_GREEN = (0, 255, 0)
COL_BLUE = (0, 0, 255)
COL_PURPLE = (255, 0, 255)      # FF00FF
COL_ORANGE = (255, 40, 0)       # FF2800
COL_YELLOW = (255, 112, 0)      # FF7000

# We use the LED color to get init status
CODE_OK = "ok"
CODE_WIFI_FAILED = "wifi_failed"
CODE_MQTT_FAILED = "mqtt_failed"
CODE_MQTT_RETRY  = "mqtt_retry"
COL_LED_ERROR = {
    CODE_OK: COL_GREEN,
    CODE_WIFI_FAILED: COL_PURPLE,
    CODE_MQTT_FAILED: COL_BLUE,
    CODE_MQTT_RETRY: COL_ORANGE,
}

MQTT_TOPIC_ROOT          = "ambiance"
MQTT_TOPIC_SUBSCRIPTION  = "/#"
MQTT_TOPIC_LENGTH        = "/length"
MQTT_TOPIC_BRIGHTNESS    = "/brightness"
MQTT_TOPIC_SCRIPT_INIT   = "/script/init"
MQTT_TOPIC_SCRIPT_EVENT  = "/script/event"
MQTT_TOPIC_EVENT_TRIGGER = "/event/trigger"


class OnboardNeoWrapper(sequencer.NeoWrapper):
    def __init__(self, target, max_len):
        super().__init__(target, max_len)
        self.val_brightness = 1

    def copy(self):
        self._target.brightness = self.val_brightness
        self._target.fill(self.data[0])

    def show(self):
        pass

    def sleep(self, seconds: float):
        super().sleep(seconds)

    def brightness(self, value: float):
        self.val_brightness = value
        self._target.brightness = value


def init() -> None:
    print("@@ init")
    global _led, _neo, _boot_btn, _init_script, _event_script, _neo_wrapper

    try:
        neo_len = int(os.getenv("NEO_LEN"))
        if neo_len > 0:
            global NEO_LEN
            NEO_LEN = neo_len
            print("@@ Settings.toml: neo len set to", NEO_LEN)
    except Exception as e:
        print("@@ Settings.toml: Invalid NEO_LEN variable ", e)

    try:
        mqtt_topic_root = os.getenv("MQTT_TOPIC_ROOT").strip()
        if mqtt_topic_root:
            global MQTT_TOPIC_ROOT
            MQTT_TOPIC_ROOT = mqtt_topic_root
            print("@@ Settings.toml: MQTT_TOPIC_ROOT set to", MQTT_TOPIC_ROOT)
    except Exception as e:
        print("@@ Settings.toml: Invalid MQTT_TOPIC_ROOT variable ", e)

    try:
        neo_strip_pin = os.getenv("NEO_STRIP_PIN").strip()
        if neo_strip_pin:
            global NEO_STRIP_PIN
            NEO_STRIP_PIN = neo_strip_pin
            print("@@ Settings.toml: NEO_STRIP_PIN set to", NEO_STRIP_PIN)
    except Exception as e:
        print("@@ Settings.toml: Invalid NEO_STRIP_PIN variable ", e)

    _led = neopixel.NeoPixel(board.NEOPIXEL, 1)
    _led.brightness = 0.1

    if NEO_STRIP_PIN == "ONBOARD":
        _neo = _led
        _neo_wrapper = OnboardNeoWrapper(_neo, NEO_LEN)
        _neo_wrapper.brightness(0.75)
    else:
        _neo = neopixel.NeoPixel(getattr(board, NEO_STRIP_PIN), NEO_LEN, auto_write = False, pixel_order=(0, 1, 2))
        _neo_wrapper = sequencer.NeoWrapper(_neo, NEO_LEN)
        _neo_wrapper.brightness(1)

    _init_script = InitScriptExec(sequencer.Sequencer(_neo_wrapper), blink)
    _event_script = EventScriptExec(sequencer.Sequencer(_neo_wrapper), blink)
    _boot_btn = digitalio.DigitalInOut(board.D0)
    _boot_btn.switch_to_input(pull = digitalio.Pull.UP)


def init_wifi() -> None:
    print("@@ WiFI setup")
    # Get wifi AP credentials from onboard settings.toml file
    wifi_ssid = os.getenv("CIRCUITPY_WIFI_SSID")
    wifi_password = os.getenv("CIRCUITPY_WIFI_PASSWORD")
    print("@@ WiFI SSID:", wifi_ssid)
    if wifi_ssid is None:
        print("@@ WiFI credentials are kept in settings.toml, please add them there!")
        raise ValueError("WiFI SSID not found in environment variables")

    try:
        wifi.radio.connect(wifi_ssid, wifi_password)
    except ConnectionError:
        print("@@ WiFI Failed to connect to WiFi with provided credentials")
        blink_error(CODE_WIFI_FAILED)
        raise
    print("@@ WiFI OK for", wifi_ssid)

def init_mqtt() -> None:
    global _mqtt
    host = os.getenv("MQTT_BROKER_IP")
    if not host:
        print("@@ MQTT: disabled")
        return
    port = int(os.getenv("MQTT_BROKER_PORT"))
    user = os.getenv("MQTT_USERNAME")
    pasw = os.getenv("MQTT_PASSWORD")
    print("@@ MQTT: connect to", host, ", port", port, ", user", user)

    # Source: https://adafruit-playground.com/u/justmobilize/pages/adafruit-connection-manager
    pool = adafruit_connection_manager.get_radio_socketpool(wifi.radio)

    # Source: https://learn.adafruit.com/mqtt-in-circuitpython/advanced-minimqtt-usage
    _mqtt = MQTT.MQTT(
        broker=host,
        port=port,
        username=user,
        password=pasw,
        is_ssl=False,
        socket_pool=pool,
    )
    _mqtt.logger = _logger

    _mqtt.on_connect = _mqtt_on_connected
    _mqtt.on_disconnect = _mqtt_on_disconnected
    _mqtt.on_message = _mqtt_on_message

    try:
        print("@@ MQTT: connecting...")
        _mqtt.connect()
    except Exception as e:
        print("@@ MQTT: Failed Connecting with ", e)
        blink_error(CODE_MQTT_FAILED, num_loop=3)
        _mqtt = "retry"

def _mqtt_on_connected(client, userdata, flags, rc):
    # This function will be called when the client is connected successfully to the broker.
    print("@Q MQTT: Connected")
    # Subscribe to all changes.
    client.subscribe(MQTT_TOPIC_ROOT + MQTT_TOPIC_SUBSCRIPTION)
    blink_error(CODE_OK, num_loop=0)

def _mqtt_on_disconnected(client, userdata, rc):
    # This method is called when the client is disconnected
    print("@Q MQTT: Disconnected")

_last_trigger = None
def _mqtt_on_message(client, topic, message):
    """Method callled when a client's subscribed feed has a new
    value.
    :param str topic: The topic of the feed with a new value.
    :param str message: The new value
    """
    print(f"@Q MQTT: New message on topic {topic}: {message}")
    try:
        if topic == MQTT_TOPIC_ROOT + MQTT_TOPIC_LENGTH:
            value = int(message)
            if value >= 1 and value <= _neo_wrapper.max_len:
                _neo_wrapper.len = int(message)
        elif topic == MQTT_TOPIC_ROOT + MQTT_TOPIC_BRIGHTNESS:
            valie = float(message)
            if value >= 0 and value <= 1:
                _neo_wrapper.brightness(value)
        elif topic == MQTT_TOPIC_ROOT + MQTT_TOPIC_SCRIPT_INIT:
            _init_script.newScript(message)
        elif topic == MQTT_TOPIC_ROOT + MQTT_TOPIC_SCRIPT_EVENT:
            _event_script.newScript(message)
        elif topic == MQTT_TOPIC_ROOT + MQTT_TOPIC_EVENT_TRIGGER:
            global _last_trigger
            if _last_trigger != message:
                _event_script.trigger()
            _last_trigger = message
    except Exception as e:
        print(f"@@ MQTT: Failed to process {topic}: {message}", e)

_mqtt_retry_ts = 0
def _mqtt_loop():
    if not _mqtt:
        return
    if isinstance(_mqtt, str) and _mqtt == "retry":
        global _mqtt_retry_ts
        if time.time() - _mqtt_retry_ts > 5:
            init_mqtt()
            _mqtt_retry_ts = time.time()
        return
    try:
        _mqtt.loop()
    except Exception as e:
        print("@@ MQTT: Failed with ", e)
        blink_error(CODE_MQTT_RETRY, num_loop=1)
        try:
            _mqtt.reconnect()
            blink_error(CODE_OK, num_loop=0)
        except Exception as e:
            print("@@ MQTT: Reconnect failed with ", e)
            blink_error(CODE_MQTT_FAILED, num_loop=2)

def blink_error(error_code, num_loop=-1):
    _led.fill(COL_LED_ERROR[error_code])
    _led.brightness = 0.1
    time.sleep(0.5)
    # For debugging purposes, we can exit the loop by using the boot button to continue
    while num_loop != 0 and _boot_btn.value:
        _led.brightness = 0
        time.sleep(0.25)
        _led.brightness = 0.1
        time.sleep(1)
        num_loop -= 1

_last_blink_ts = 0
_next_blink = 1
def blink() -> None:
    if NEO_DONT_BLINK:
        return
    global _last_blink_ts, _next_blink
    _led.brightness = 0.1 if _next_blink else 0
    now = time.time()
    if now - _last_blink_ts > 1:
        _last_blink_ts = now
        _next_blink = 1 - _next_blink

def loop() -> None:
    print("@@ loop")

    # # Sleep a few seconds at boot
    _led.fill(COL_LED_ERROR[CODE_OK])
    for i in range(0, 3):
        print(i)
        blink()
        time.sleep(1)

    _neo.fill(COL_OFF)
    _neo.show()

    global NEO_DONT_BLINK
    NEO_DONT_BLINK = NEO_STRIP_PIN == "ONBOARD"

    _init_script.loadFromNVM()
    blink()

    while True:
        start_ts = time.monotonic()
        blink()
        _mqtt_loop()    # This takes 1~2 seconds
        _init_script.loop()
        _event_script.loop()
        end_ts = time.monotonic()
        delta_ts = end_ts - start_ts
        if delta_ts < 1: time.sleep(0.25)  # prevent busy loop
        print("@@ loop: ", delta_ts)


if __name__ == "__main__":
    init()
    init_wifi()
    init_mqtt()
    loop()

#~~
