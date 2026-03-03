from setuptools import setup

APP = ['pynthesizer.py']
DATA_FILES = [] # Add things like a .icns icon file here later if you want one

OPTIONS = {
    'argv_emulation': False,
    # Force py2app to include these entire packages (vital for binary wheels)
    'packages': ['numpy', 'sounddevice', '_sounddevice_data', 'mido', 'pedalboard'],
    # Explicitly include the tkinter framework and the rtmidi backend
    'includes': ['tkinter', 'mido.backends.rtmidi'],
    # The plist configures how macOS sees your app
    'plist': {
        'CFBundleName': 'Pynthesizer',
        'CFBundleDisplayName': 'Pynthesizer',
        'CFBundleGetInfoString': "Polyphonic Synthesizer",
        'CFBundleIdentifier': "com.bertjerred.pynthesizer",
        'CFBundleVersion': "1.0.0",
        'CFBundleShortVersionString': "1.0",
        # CoreAudio sometimes triggers macOS permissions even for output, so this prevents silent crashes
        'NSMicrophoneUsageDescription': 'Pynthesizer requires audio access to initialize the sound engine.', 
    }
}

setup(
    app=APP,
    name='Pynthesizer',
    data_files=DATA_FILES,
    options={'py2app': OPTIONS},
    setup_requires=['py2app'],
)