import os
from pimatic_midi import PimaticMidiController

debug = False

__location__ = os.path.realpath(os.path.join(os.getcwd(), os.path.dirname(__file__)))
settings_filename = os.path.join(__location__, "settings.json")

pimatic_midi_controller = PimaticMidiController(
    settings=settings_filename,
    debug=debug
)

pimatic_midi_controller.run_forever()


