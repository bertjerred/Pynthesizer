# Pynthesizer
**v1.0.0** | **© 2026 Bert Jerred Music Publishing**

![Pynthesizer plugin screenshot](https://github.com/bertjerred/Pynthesizer/blob/main/Pynthesizer-screenshot.png)

Pynthesizer is a custom polyphonic virtual instrument featuring bandlimited oscillators, state-variable filters, and dynamic pedalboard effects. It is designed to be lightweight, standalone, and easily customizable.

## Getting Started

Pynthesizer requires no installation. It runs completely standalone.

1. Extract the entire `.zip` folder to your computer.
2. Double-click `pynthesizer.exe` to launch the synthesizer.
3. In the top-left corner of the window, select your MIDI keyboard or controller from the **MIDI In** dropdown menu.
4. Turn up your volume and play!

## Presets & File Management

Pynthesizer uses a fast, dropdown-based preset system. 

* **Where are they?** All patches are saved as lightweight `.json` files in the `presets/` folder located right next to your `pynthesizer.exe` file.
* **Saving:** Type a name into the "New Preset" box in the top bar and click **SAVE**. It will instantly appear in the dropdown.
* **Sharing:** You can easily back up, share, or trade your sounds by copying the `.json` files out of the `presets/` folder.
* **Manual Editing:** Want to build a preset by hand? Open `preset_template.json` in any text editor to see a fully commented breakdown of every parameter.

## Modifying the Synth (For Developers)

Pynthesizer is built in Python using `sounddevice`, `numpy`, and `tkinter`. If you are interested in diving into the source code to add your own oscillators, map new MIDI CC controls, or add new DSP effects, please read the included `HACKING.md` guide for a complete architectural overview and code examples.
