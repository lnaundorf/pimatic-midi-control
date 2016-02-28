from __future__ import division

import rtmidi_python
from threading import Thread, Event
from socketIO_client import SocketIO, LoggingNamespace
import time
import json
import os


class Update:
    PIMATIC = 0
    MIDI = 1


class PimaticMidiController:
    def __init__(self, settings, debug=False):
        self._debug = debug
        self._load_config(settings)
        if self._debug:
            print("Configuration: %s" % json.dumps(self._config, sort_keys=True, indent=4))

        self._connect_socket_io()
        self._setup_midi_ports()

    def _load_config(self, settings):
        if isinstance(settings, dict):
            self._config = settings
        elif isinstance(settings, str):
            # Load settings from a JSON file
            self._config = {}

            with open(settings) as settings_file:
                settings = json.load(settings_file)
                pimatic_settings = settings['pimatic']
                self._config['pimatic_host'] = pimatic_settings['host']
                self._config['pimatic_port'] = pimatic_settings['port']
                self._config['pimatic_username'] = pimatic_settings.get('username', None)
                self._config['pimatic_password'] = pimatic_settings.get('password', None)

                self._config['pimatic_midi_mapping'] = []

                midi_config = settings['midi']
                self._config['midi_port_in'] = midi_config.get('port_in', 0)
                self._config['midi_port_out'] = midi_config.get('port_out', 1)

                # Pads
                pad_config = midi_config.get('pads', None)

                if pad_config:
                    self._config['midi_pad_on'] = pad_config.get('type_on', 144)
                    self._config['midi_pad_off'] = pad_config.get('type_off', 128)

                    pad_devices = pad_config.get('devices', None)

                    if pad_devices:
                        for dev in pad_devices:
                            self._add_device_to_config(dev, "pad")

                # Knobs
                knob_config = midi_config.get('knobs', None)

                if knob_config:
                    self._config['knob_type'] = knob_config.get('type', 176)
                    self._config['knob_min_value'] = knob_config.get('min_value', 0)
                    self._config['knob_max_value'] = knob_config.get('max_value', 0)
                    self._config['knob_accept_time_ms'] = knob_config.get('accept_time_ms', 500)

                    knob_devices = knob_config.get('devices', None)

                    if knob_devices:
                        for dev in knob_devices:
                            self._add_device_to_config(dev, "knob")
        else:
            print("Unknown settings type: %s" % type(settings))

    def _add_device_to_config(self, dev, type):
        device_id = dev.get('device_id', None)
        midi_port = dev.get('midi_port', None)
        attribute = dev.get('attribute', 'state')
        step = dev.get('step', 0.5)
        action = dev.get('action', 'changeStateTo')

        if device_id and midi_port:
            print("Add %s device %s on midi port %d" % (type, device_id, midi_port))
            self._config['pimatic_midi_mapping'].append({
                'type': type,
                'device_id': device_id,
                'midi_port': midi_port,
                'attribute': attribute,
                'step': step,
                'action': action
            })

    def _connect_socket_io(self):
        kwargs = {}

        if self._config['pimatic_username'] and self._config['pimatic_password']:
            kwargs['params'] = {
                'username': self._config['pimatic_username'],
                'password': self._config['pimatic_password']
            }

        self._socket_io = SocketIO(
            self._config['pimatic_host'],
            self._config['pimatic_port'],
            **kwargs
        )

        self._socket_io.on('connect', self._on_connect)
        self._socket_io.on('disconnect', self._on_disconnect)
        self._socket_io.on('devices', self._on_devices)
        self._socket_io.on('deviceAttributeChanged', self._on_device_attribute_changed)
        self._socket_io.on('callResult', self._on_call_result)

    def _setup_midi_ports(self):
        self._stop_midi_in = Event()

        def midi_read(socket_io, stop):
            midi_in = rtmidi_python.MidiIn(b'in')
            if self._debug:
                for port in midi_in.ports:
                    print("In Port: %s" % port)

            midi_in.open_port(self._config['midi_port_in'])

            knob_temp_values = {}

            while not self._stop_midi_in.is_set():
                now = time.time()
                keys_to_delete = []
                for knob_key, knob_value in knob_temp_values.items():
                    if now - knob_value['time'] >= self._config['knob_accept_time_ms'] / 1000:
                        # Fire the knob change event
                        new_value = self.compute_knob_value(knob_key, knob_value['value'])
                        device_id, value_change = self._set_value_by_key(
                            key='midi_port',
                            value=knob_key,
                            new_value=new_value,
                            update_types=[Update.PIMATIC]
                        )

                        # We cannot delete the key while iterating, so we do this after the iteration
                        keys_to_delete.append(knob_key)
                for key in keys_to_delete:
                    del knob_temp_values[key]

                message, delta_time = midi_in.get_message()
                if message:
                    if message[0] == self._config['midi_pad_on']:
                        pass
                    elif message[0] == self._config['midi_pad_off']:
                        # A pad button was released
                        device_id, value_change = self._set_value_by_key(
                            key='midi_port',
                            value=message[1],
                            update_types=[Update.PIMATIC, Update.MIDI]
                        )
                    elif message[0] == self._config['knob_type']:
                        # A knob was changed
                        # print("Knob was changed: %s" % message)
                        knob_temp_values[message[1]] = {
                            'value': message[2],
                            'time': time.time()
                        }

                    elif self._debug:
                        print("Unrecognized midi input: %s" % message)

                time.sleep(0.01)

        midi_read_thread = Thread(target=midi_read, args=(self._socket_io, self._stop_midi_in))
        midi_read_thread.start()

        # There seems to be a timing issue if we dont sleep here
        time.sleep(0.1)

        self._midi_out = rtmidi_python.MidiOut(b'out')
        if self._debug:
            for port in self._midi_out.ports:
                print("Out Port: %s" % port)
        self._midi_out.open_port(self._config['midi_port_out'])


    def compute_knob_value(self, midi_port, value):
        dev = self._search_device_by_key('midi_port', midi_port)

        if not dev:
            return None

        knob_value = dev['min_value'] + ((value - self._config['knob_min_value']) / self._config['knob_max_value']) * (dev['max_value'] - dev['min_value'])

        # print("Knob value: %s" % knob_value)
        # Round with respect to 'step'
        return round(knob_value / dev['step']) * dev['step']

    def _search_device_by_key(self, key, value, attribute=None):
        for dev in self._config['pimatic_midi_mapping']:
            if dev[key] == value and (attribute is None or dev['attribute'] == attribute):
                return dev

    def _set_value_by_key(self, key, value, update_types=None, new_value=None, attribute=None, force_update=False):
        dev = self._search_device_by_key(key, value, attribute=attribute)
        if not dev:
            return None, False

        if new_value is not None:
            if dev['type'] == 'pad':
                new_value = 1 if new_value else 0
            else:
                new_value = new_value if new_value else 0
        else:
            # Toggle the state if no new_state is given
            new_value = 1 if not dev.get('value', None) else 0

        value_change = dev.get('value', None) != new_value
        dev['value'] = new_value

        if force_update or value_change:
            if Update.MIDI in update_types:
                # Update the midi light state
                if self._debug:
                    print("Send midi message, Port: %s, State: %s" % (dev['midi_port'], new_value))
                self._midi_out.send_message([self._config['midi_pad_on'], dev['midi_port'], new_value])

            if Update.PIMATIC in update_types:
                # Update the value in pimatic
                call_dict = {
                    'id': dev['device_id'],
                    'action': 'callDeviceAction',
                    'params': {
                        'deviceId': dev['device_id'],
                        'actionName': dev['action'],
                        dev['attribute']: new_value
                    }
                }
                if self._debug:
                    print("Call Pimatic: %s" % json.dumps(call_dict, indent=4))
                self._socket_io.emit('call', call_dict)

        if value_change:
            print("Changed %s of %s to %s" % (dev['attribute'], dev['device_id'], new_value))

        return dev['device_id'], value_change

    def _update_device_config_by_key(self, key, value, config_to_add, type=None):
        for dev in self._config['pimatic_midi_mapping']:
            if dev[key] == value and (type is None or dev['type'] == type):
                dev.update(config_to_add)
                return True

        return False

    def _on_connect(self):
        print("Connected to SocketIO on %s:%s" % (self._config['pimatic_host'], self._config['pimatic_port']))

    def _on_disconnect(self):
        print("Disconnected from SocketIO on %s:%s" % (self._config['pimatic_host'], self._config['pimatic_port']))

    def _on_call_result(self, data):
        if self._debug:
            print("CallResult: %s" % json.dumps(data, indent=4))

    def _on_devices(self, data):
        for device in data:
            config = device['config']

            device_id = config['id']
            min_value = config.get('ecoTemp', 12)
            max_value = config.get('comfyTemp', 18)

            self._update_device_config_by_key(
                key="device_id",
                value=device_id,
                config_to_add={
                    "min_value": min_value,
                    "max_value": max_value,
                },
                type="knob"
            )

            if self._debug:
                print("Update device %s: %s - %s" % (device_id, min_value, max_value))

        if self._debug:
            print("Config after devices: %s" % json.dumps(self._config, indent=4, sort_keys=True))

    def _on_device_attribute_changed(self, data):
        device_id, value_change = self._set_value_by_key(
            key='device_id',
            value=data['deviceId'],
            update_types=[Update.MIDI],
            new_value=data['value'],
            attribute=data['attributeName']
        )

        if device_id and value_change:
            print("Device attribute changed: %s" % json.dumps(data, indent=4))

    def run_forever(self):
        try:
            self._socket_io.wait()
        except KeyboardInterrupt:
            self.stop()

    def stop(self):
        self._stop_midi_in.set()


if __name__ == "__main__":
    debug = False

    __location__ = os.path.realpath(os.path.join(os.getcwd(), os.path.dirname(__file__)))
    settings_filename = os.path.join(__location__, "settings.json")

    pimatic_midi_controller = PimaticMidiController(
        settings=settings_filename,
        debug=debug
    )

    pimatic_midi_controller.run_forever()
