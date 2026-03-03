---
title: Pynthesizer Hacking Guide
author: Bert Jerred
date: 2026-02-23
version: 1.0.0
---

# Hacking Guide for Pynthesizer

Welcome! This guide is for you if you want to dive into the `pynthesizer.py` source code to add or modify its features.

## Code Overview

The program is split into two main classes:

1.  `SynthEngine`: This is the brain. It handles all audio generation, from the oscillators to the filters and effects. It knows nothing about GUIs or MIDI ports; it just makes sound. The most important method here is `audio_callback`, which is called continuously by the audio driver to generate small chunks of audio.

2.  `SynthGui`: This is the face and the ears. It creates the user interface with `tkinter` and manages MIDI input/output using `mido`. When you move a slider, it tells the `SynthEngine` to change a parameter. When a MIDI note comes in, it tells the `SynthEngine` to play it.

## How to Tinker: Ideas and Examples

Here are some common things you might want to try, with pointers on where to look in the code.

**Important:** Real-time audio processing is tricky. A small bug in the `audio_callback` can cause audio glitches, loud noises, or even crash the program. Save your work often!

### 1. Add a New Oscillator (e.g., Sine Wave)

A sine wave is a great starting point.

1.  **Add a Parameter:** In `SynthEngine.__init__`, add a level parameter for your new oscillator.
    ```python
    # In SynthEngine.__init__
    self.osc_sine_level = 0.0
    ```

2.  **Generate the Wave:** In `SynthEngine.audio_callback`, find the `// --- OSCILLATORS ---` section. Add your logic there.
    ```python
    # In audio_callback, after the other oscillators
    if self.osc_sine_level > 0.01:
        sine_wave = np.sin(2 * np.pi * phase_norm)
        osc_mix += sine_wave * self.osc_sine_level
    ```

3.  **Add a GUI Control:** In `SynthGui.__init__`, find the `osc_frame` and add a new slider for your sine wave, copying the pattern of the others.
    ```python
    # In SynthGui.__init__
    ttk.Label(osc_frame, text="Sine Level").pack(anchor="w", padx=10)
    self.scale_sine = ttk.Scale(osc_frame, from_=0.0, to=1.0, orient="horizontal", command=lambda v: setattr(self.synth, 'osc_sine_level', float(v)))
    self.scale_sine.pack(fill="x", padx=10, pady=5)
    ```

4.  **Update Preset/GUI Functions:** Remember to add `osc_sine_level` to `_apply_state_to_synth`, `_update_gui_from_synth`, and the `state` dictionary in `save_preset` so it can be saved and loaded correctly.

### 2. Add a New LFO Modulation Target

Let's try modulating the overall volume (tremolo).

1.  **Add a Parameter:** In `SynthEngine.__init__`, add a depth parameter for the new modulation.
    ```python
    # In SynthEngine.__init__
    self.lfo_to_amp_depth = 0.0
    ```

2.  **Apply the Modulation:** In `SynthEngine.audio_callback`, find where `lfo_value` is calculated. Then, at the end of the per-note processing loop, apply it to the `final_wave`.
    ```python
    # In audio_callback, just before mixed_audio += final_wave
    if self.lfo_to_amp_depth > 0.0:
        # We use (lfo_value + 1) / 2 to map it from [-1, 1] to
        lfo_mod = 1.0 - (((lfo_value + 1) / 2.0) * self.lfo_to_amp_depth)
        final_wave *= lfo_mod

    mixed_audio += final_wave
    ```

3.  **Add a GUI Control:** In `SynthGui.__init__`, add a slider to the `mod_frame`.
    ```python
    # In SynthGui.__init__ (in the MODULATION frame)
    ttk.Label(mod_frame, text="LFO -> Amp").pack(anchor="w", padx=10)
    self.scale_lfo_amp = ttk.Scale(mod_frame, from_=0.0, to=1.0, orient="horizontal", command=lambda v: setattr(self.synth, 'lfo_to_amp_depth', float(v)))
    self.scale_lfo_amp.pack(fill="x", padx=10, pady=5)
    ```

4.  **Update Preset/GUI Functions:** As before, add `lfo_to_amp_depth` to the preset and GUI update functions.

### 3. Add a New Effect (e.g., Chorus)

The `pedalboard` library makes this easy.

1.  **Import It:** At the top of the file, import the `Chorus` effect.
    ```python
    from pedalboard import Pedalboard, Reverb, Chorus
    ```

2.  **Add to Pedalboard:** In `SynthEngine.__init__`, add the `Chorus` to your `self.pb` list. Also add a parameter to control its level.
    ```python
    # In SynthEngine.__init__
    self.chorus_level = 0.0
    self.pb = Pedalboard([
        Reverb(room_size=0.95, damping=0.2, width=1.0, dry_level=1.0),
        Chorus(rate_hz=1.0, depth=0.25, mix=0.0) # Start with mix at 0
    ])
    ```

3.  **Control the Effect:** In `SynthEngine.audio_callback`, find the `// --- Reverb FX ---` section. Add logic to control your new chorus. The Reverb is at index 0, so the Chorus will be at index 1.
    ```python
    # In audio_callback, before the pedalboard is processed
    self.pb[1].mix = self.chorus_level # pedalboard's Chorus uses 'mix'
    ```

4.  **Add a GUI Control & Update Functions:** Add a slider to the `fx_frame` and remember to update the preset and GUI functions with the new `chorus_level` parameter, just like in the previous examples.

### 4. Expand MIDI Control

You can map any MIDI Control Change (CC) message to a parameter.

1.  **Find the Worker:** Go to the `_midi_worker` method in the `SynthGui` class.

2.  **Add a CC Mapping:** Find the `elif msg.type == 'control_change'` block. Add a new condition for the CC number you want to use (e.g., CC 75 for LFO Speed).
    ```python
    # In _midi_worker
    elif msg.control == 75: # CC 75 for LFO Speed
        normalized = msg.value / 127.0
        new_lfo_speed = normalized * 20.0 # The slider's max is 20.0
        self.root.after(0, lambda val=new_lfo_speed: self.scale_lfo_spd.set(val))
    ```
    Note that we use `self.root.after` to safely update the GUI from this background thread. Setting the slider's value will automatically trigger its command and update the synth engine.

Happy Hacking!